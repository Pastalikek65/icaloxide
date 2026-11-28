#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use compact_str::CompactString;
use icaloxide_core::tz::{build_vtimezone_for_tzid, strip_vendor_prefix};
use icaloxide_core::writer::write_folded_line;
use icaloxide_core::{
    CalDate, CalDateTime, CalDuration, CalPeriod, CalTime, Calendar as CoreCalendar,
    CalendarTimeZone, CompiledVTimezone, ComponentId, ComponentKind, DstDisambiguation, ParamName,
    Parameter, ParseOptions, Property, PropertyName, PropertyValue, TimeZoneRef, TzRegistry,
    UtcOffset,
};
use icaloxide_rrule::{
    AfterIter, CalendarQuery, IterationBudget, Occurrence, QueryOptions, RRuleError,
};
use pyo3::exceptions::{PyKeyError, PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTuple,
};
use smallvec::SmallVec;

struct PyDateTimeTypes {
    date_cls: Py<PyAny>,
    datetime_cls: Py<PyAny>,
    time_cls: Py<PyAny>,
    timedelta_cls: Py<PyAny>,
    timezone_cls: Py<PyAny>,
    utc_tz: Py<PyAny>,
    zoneinfo_cls: Option<Py<PyAny>>,
    zoneinfo_cache: Py<PyDict>,
    fixed_tz_cache: Py<PyDict>,
}

static DATETIME_TYPES: GILOnceCell<PyDateTimeTypes> = GILOnceCell::new();

fn dt_types(py: Python<'_>) -> PyResult<&PyDateTimeTypes> {
    DATETIME_TYPES.get_or_try_init(py, || {
        let dt_mod = py.import("datetime")?;
        let tz_cls = dt_mod.getattr("timezone")?;
        let utc_tz = tz_cls.getattr("utc")?.unbind();
        let zoneinfo_cls = py
            .import("zoneinfo")
            .and_then(|m| m.getattr("ZoneInfo"))
            .map(Bound::unbind)
            .ok();
        Ok(PyDateTimeTypes {
            date_cls: dt_mod.getattr("date")?.unbind(),
            datetime_cls: dt_mod.getattr("datetime")?.unbind(),
            time_cls: dt_mod.getattr("time")?.unbind(),
            timedelta_cls: dt_mod.getattr("timedelta")?.unbind(),
            timezone_cls: tz_cls.unbind(),
            utc_tz,
            zoneinfo_cls,
            zoneinfo_cache: PyDict::new(py).unbind(),
            fixed_tz_cache: PyDict::new(py).unbind(),
        })
    })
}

fn parse_err_to_py(err: &icaloxide_core::ParseError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

fn rrule_err_to_py(err: &RRuleError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

fn cal_date_to_py(py: Python<'_>, d: CalDate) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    types
        .date_cls
        .bind(py)
        .call1((i32::from(d.year), d.month, d.day))
}

fn cal_time_to_py(py: Python<'_>, t: CalTime, is_utc: bool) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let sec = t.second.min(59);
    if is_utc {
        let tz = types.utc_tz.bind(py);
        types
            .time_cls
            .bind(py)
            .call1((t.hour, t.minute, sec, 0_u8, tz))
    } else {
        types.time_cls.bind(py).call1((t.hour, t.minute, sec))
    }
}

fn fixed_tz_to_py<'py>(
    py: Python<'py>,
    offset_seconds: i32,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let types = dt_types(py)?;
    let cache = types.fixed_tz_cache.bind(py);
    if let Some(cached) = cache.get_item((offset_seconds, name))? {
        return Ok(cached);
    }
    let delta = types
        .timedelta_cls
        .bind(py)
        .call1((0_i32, offset_seconds))?;
    let tz = types.timezone_cls.bind(py).call1((delta, name))?;
    if cache.len() < 256 {
        cache.set_item((offset_seconds, name), &tz)?;
    }
    Ok(tz)
}

fn tzref_to_py<'py>(
    py: Python<'py>,
    registry: &TzRegistry,
    date: CalDate,
    time: CalTime,
    tz: &TimeZoneRef,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let types = dt_types(py)?;
    match tz {
        TimeZoneRef::Utc => Ok(Some(types.utc_tz.bind(py).clone())),
        TimeZoneRef::Floating => Ok(None),
        TimeZoneRef::TzId(id) => {
            let canonical = registry.resolve_canonical_iana(id.as_str());
            if canonical.as_deref() == Some("UTC") {
                return Ok(Some(types.utc_tz.bind(py).clone()));
            }

            if registry.has_custom_vtimezone(id.as_str()) && canonical.is_none() {
                if let Some(cal_tz) = registry.resolve(id.as_str()) {
                    if let Ok(resolved) =
                        cal_tz.resolve_local(date, time, DstDisambiguation::Compatible)
                    {
                        return Ok(Some(fixed_tz_to_py(
                            py,
                            resolved.offset_seconds,
                            id.as_str(),
                        )?));
                    }
                }
            }

            let lookup_key = canonical.as_deref().unwrap_or(id.as_str());
            let zi_cache = types.zoneinfo_cache.bind(py);
            if let Some(cached) = zi_cache.get_item(lookup_key)? {
                return Ok(Some(cached));
            }
            if let Some(zi_cls) = &types.zoneinfo_cls {
                if let Ok(zi) = zi_cls.bind(py).call1((lookup_key,)) {
                    if zi_cache.len() < 1024 {
                        zi_cache.set_item(lookup_key, &zi)?;
                    }
                    return Ok(Some(zi));
                }
            }

            if let Some(cal_tz) = registry.resolve(id.as_str()) {
                if let Ok(resolved) =
                    cal_tz.resolve_local(date, time, DstDisambiguation::Compatible)
                {
                    return Ok(Some(fixed_tz_to_py(
                        py,
                        resolved.offset_seconds,
                        lookup_key,
                    )?));
                }
            }

            Ok(None)
        }
    }
}

fn cal_datetime_to_py<'py>(
    py: Python<'py>,
    registry: &TzRegistry,
    dt: &CalDateTime,
) -> PyResult<Bound<'py, PyAny>> {
    let types = dt_types(py)?;
    let tzinfo = tzref_to_py(py, registry, dt.date, dt.time, &dt.tz)?;
    let sec = dt.time.second.min(59);
    types.datetime_cls.bind(py).call1((
        i32::from(dt.date.year),
        dt.date.month,
        dt.date.day,
        dt.time.hour,
        dt.time.minute,
        sec,
        0_u8,
        tzinfo,
    ))
}

fn cal_datetime_to_py_with_utc<'py>(
    py: Python<'py>,
    registry: &TzRegistry,
    dt: &CalDateTime,
    exact_utc: i64,
) -> PyResult<Bound<'py, PyAny>> {
    let TimeZoneRef::TzId(id) = &dt.tz else {
        return cal_datetime_to_py(py, registry, dt);
    };
    let canonical = registry.resolve_canonical_iana(id.as_str());
    if canonical.as_deref() == Some("UTC") {
        return cal_datetime_to_py(py, registry, dt);
    }

    let types = dt_types(py)?;
    let cal_tz = registry.resolve(id.as_str());
    let mut out_date = dt.date;
    let mut out_time = dt.time;
    let mut fold = 0_u8;

    let tzinfo = if registry.has_custom_vtimezone(id.as_str()) && canonical.is_none() {
        if let Some(tz) = &cal_tz {
            if let Ok(off) = tz.offset_at_utc(exact_utc) {
                let local_naive = exact_utc + i64::from(off);
                out_date = CalDate::from_days_since_epoch(local_naive.div_euclid(86_400) as i32);
                let rem = local_naive.rem_euclid(86_400) as u32;
                out_time = CalTime {
                    hour: (rem / 3600) as u8,
                    minute: ((rem % 3600) / 60) as u8,
                    second: (rem % 60) as u8,
                };
                Some(fixed_tz_to_py(py, off, id.as_str())?)
            } else {
                tzref_to_py(py, registry, dt.date, dt.time, &dt.tz)?
            }
        } else {
            tzref_to_py(py, registry, dt.date, dt.time, &dt.tz)?
        }
    } else {
        if let Some(tz) = &cal_tz {
            if let Ok(earlier) = tz.resolve_local(dt.date, dt.time, DstDisambiguation::Earlier) {
                if earlier.was_fold {
                    if let Ok(later) = tz.resolve_local(dt.date, dt.time, DstDisambiguation::Later)
                    {
                        if later.utc_timestamp == exact_utc && earlier.utc_timestamp != exact_utc {
                            fold = 1;
                        }
                    }
                }
            }
        }
        tzref_to_py(py, registry, out_date, out_time, &dt.tz)?
    };

    let sec = out_time.second.min(59);
    let args = (
        i32::from(out_date.year),
        out_date.month,
        out_date.day,
        out_time.hour,
        out_time.minute,
        sec,
        0_u8,
        tzinfo,
    );
    if fold == 0 {
        types.datetime_cls.bind(py).call1(args)
    } else {
        let kwargs = PyDict::new(py);
        kwargs.set_item("fold", 1_u8)?;
        types.datetime_cls.bind(py).call(args, Some(&kwargs))
    }
}

fn cal_duration_to_py(py: Python<'_>, dur: CalDuration) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let total = dur.total_seconds();
    let days = total.div_euclid(86_400) as i32;
    let secs = total.rem_euclid(86_400) as i32;
    types.timedelta_cls.bind(py).call1((days, secs))
}

fn cal_period_to_py<'py>(
    py: Python<'py>,
    registry: &TzRegistry,
    period: &CalPeriod,
) -> PyResult<Bound<'py, PyTuple>> {
    match period {
        CalPeriod::Explicit { start, end } => {
            let s = cal_datetime_to_py(py, registry, start)?;
            let e = cal_datetime_to_py(py, registry, end)?;
            PyTuple::new(py, [s, e])
        }
        CalPeriod::Duration { start, duration } => {
            let s = cal_datetime_to_py(py, registry, start)?;
            let d = cal_duration_to_py(py, *duration)?;
            PyTuple::new(py, [s, d])
        }
    }
}

fn utc_offset_to_py(py: Python<'_>, offset: UtcOffset) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let total = i64::from(offset.seconds);
    let days = total.div_euclid(86_400) as i32;
    let secs = total.rem_euclid(86_400) as i32;
    types.timedelta_cls.bind(py).call1((days, secs))
}

fn prop_params_to_py_dict<'py>(
    py: Python<'py>,
    params: &[Parameter],
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for param in params {
        let vals = PyList::new(py, param.values.iter().map(CompactString::as_str))?;
        dict.set_item(param.name.as_str(), vals)?;
    }
    Ok(dict)
}

fn property_value_to_py<'py>(
    py: Python<'py>,
    registry: &TzRegistry,
    val: &PropertyValue,
) -> PyResult<Bound<'py, PyAny>> {
    match val {
        PropertyValue::Text(s)
        | PropertyValue::Uri(s)
        | PropertyValue::CalAddress(s)
        | PropertyValue::Binary(s)
        | PropertyValue::Recur(s)
        | PropertyValue::Raw(s) => Ok(PyString::new(py, s.as_str()).into_any()),
        PropertyValue::TextList(items) => {
            let list = PyList::new(py, items.iter().map(CompactString::as_str))?;
            Ok(list.into_any())
        }
        PropertyValue::Date(d) => cal_date_to_py(py, *d),
        PropertyValue::DateTime(dt) => cal_datetime_to_py(py, registry, dt),
        PropertyValue::DateList(items) => {
            let py_items: Vec<Bound<'py, PyAny>> = items
                .iter()
                .map(|&d| cal_date_to_py(py, d))
                .collect::<PyResult<_>>()?;
            Ok(PyList::new(py, py_items)?.into_any())
        }
        PropertyValue::DateTimeList(items) => {
            let py_items: Vec<Bound<'py, PyAny>> = items
                .iter()
                .map(|dt| cal_datetime_to_py(py, registry, dt))
                .collect::<PyResult<_>>()?;
            Ok(PyList::new(py, py_items)?.into_any())
        }
        PropertyValue::Duration(dur) => cal_duration_to_py(py, *dur),
        PropertyValue::Period(p) => Ok(cal_period_to_py(py, registry, p)?.into_any()),
        PropertyValue::PeriodList(items) => {
            let py_items: Vec<Bound<'py, PyTuple>> = items
                .iter()
                .map(|p| cal_period_to_py(py, registry, p))
                .collect::<PyResult<_>>()?;
            Ok(PyList::new(py, py_items)?.into_any())
        }
        PropertyValue::Integer(v) => Ok(PyInt::new(py, *v).into_any()),
        PropertyValue::Float(v) => Ok(PyFloat::new(py, *v).into_any()),
        PropertyValue::Geo { lat, lon } => Ok(PyTuple::new(py, [*lat, *lon])?.into_any()),
        PropertyValue::Boolean(b) => Ok(PyBool::new(py, *b).to_owned().into_any()),
        PropertyValue::UtcOffset(off) => utc_offset_to_py(py, *off),
        PropertyValue::Time { time, is_utc } => cal_time_to_py(py, *time, *is_utc),
        PropertyValue::RequestStatus(parts) => Ok(PyString::new(py, &parts.join(";")).into_any()),
    }
}

fn utc_ts_to_py(py: Python<'_>, utc_ts: i64) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let days = utc_ts.div_euclid(86_400) as i32;
    let rem = utc_ts.rem_euclid(86_400) as u32;
    let date = CalDate::from_days_since_epoch(days);
    let hour = (rem / 3_600) as u8;
    let minute = ((rem % 3_600) / 60) as u8;
    let second = (rem % 60) as u8;
    let tz = types.utc_tz.bind(py);
    types.datetime_cls.bind(py).call1((
        i32::from(date.year),
        date.month,
        date.day,
        hour,
        minute,
        second,
        0_u32,
        tz,
    ))
}

enum PyInputTemporal {
    Date(CalDate),
    DateTime(CalDateTime),
}

fn py_to_temporal(
    obj: &Bound<'_, PyAny>,
    tzid_override: Option<&str>,
) -> PyResult<PyInputTemporal> {
    let year = obj.getattr("year")?.extract::<i16>()?;
    let month = obj.getattr("month")?.extract::<u8>()?;
    let day = obj.getattr("day")?.extract::<u8>()?;
    let cal_date =
        CalDate::new(year, month, day).map_err(|e| PyValueError::new_err(e.to_string()))?;

    if !obj.hasattr("hour")? {
        return Ok(PyInputTemporal::Date(cal_date));
    }

    let hour = obj.getattr("hour")?.extract::<u8>()?;
    let minute = obj.getattr("minute")?.extract::<u8>()?;
    let second = obj.getattr("second")?.extract::<u8>()?;
    let cal_time =
        CalTime::new(hour, minute, second).map_err(|e| PyValueError::new_err(e.to_string()))?;

    let tz = if let Some(override_id) = tzid_override {
        if override_id.eq_ignore_ascii_case("UTC") {
            TimeZoneRef::Utc
        } else {
            TimeZoneRef::TzId(CompactString::from(override_id))
        }
    } else {
        let tzinfo = obj.getattr("tzinfo")?;
        if tzinfo.is_none() {
            TimeZoneRef::Floating
        } else {
            let offset_obj = obj.call_method0("utcoffset")?;
            let off_secs = offset_obj
                .call_method0("total_seconds")?
                .extract::<f64>()?
                .round() as i64;
            if let Ok(key) = tzinfo.getattr("key").and_then(|k| k.extract::<String>()) {
                if key.eq_ignore_ascii_case("UTC") || key.eq_ignore_ascii_case("Etc/UTC") {
                    TimeZoneRef::Utc
                } else {
                    TimeZoneRef::TzId(CompactString::from(key))
                }
            } else if off_secs == 0 {
                TimeZoneRef::Utc
            } else {
                let sign = if off_secs < 0 { '-' } else { '+' };
                let abs = off_secs.abs();
                let h = abs / 3600;
                let m = (abs % 3600) / 60;
                TimeZoneRef::TzId(CompactString::from(format!("UTC{sign}{h:02}:{m:02}")))
            }
        }
    };

    Ok(PyInputTemporal::DateTime(CalDateTime {
        date: cal_date,
        time: cal_time,
        tz,
    }))
}

fn py_duration_to_cal(obj: &Bound<'_, PyAny>) -> PyResult<CalDuration> {
    let total = obj.call_method0("total_seconds")?.extract::<f64>()?.round() as i64;
    let negative = total < 0;
    let abs = total.unsigned_abs();
    let days = (abs / 86_400) as u32;
    let rem = abs % 86_400;
    let hours = (rem / 3_600) as u32;
    let minutes = ((rem % 3_600) / 60) as u32;
    let seconds = (rem % 60) as u32;
    Ok(CalDuration {
        negative,
        weeks: 0,
        days,
        hours,
        minutes,
        seconds,
    })
}

fn py_time_bound_to_utc(
    obj: &Bound<'_, PyAny>,
    default_tz: &CalendarTimeZone,
    is_end_bound: bool,
) -> PyResult<i64> {
    let year = obj.getattr("year")?.extract::<i16>()?;
    let month = obj.getattr("month")?.extract::<u8>()?;
    let day = obj.getattr("day")?.extract::<u8>()?;
    let mut cal_date =
        CalDate::new(year, month, day).map_err(|e| PyValueError::new_err(e.to_string()))?;

    if obj.hasattr("hour")? {
        let hour = obj.getattr("hour")?.extract::<u8>()?;
        let minute = obj.getattr("minute")?.extract::<u8>()?;
        let second = obj.getattr("second")?.extract::<u8>()?;
        let cal_time =
            CalTime::new(hour, minute, second).map_err(|e| PyValueError::new_err(e.to_string()))?;

        let utc_offset = obj.call_method0("utcoffset")?;
        if utc_offset.is_none() {
            let utc = default_tz
                .resolve_local(cal_date, cal_time, DstDisambiguation::Compatible)
                .map_or_else(
                    |_| {
                        i64::from(cal_date.to_days_since_epoch()) * 86_400
                            + i64::from(cal_time.seconds_since_midnight())
                    },
                    |r| r.utc_timestamp,
                );
            Ok(utc)
        } else {
            let offset_secs = utc_offset.call_method0("total_seconds")?.extract::<f64>()?;
            let naive = i64::from(cal_date.to_days_since_epoch()) * 86_400
                + i64::from(cal_time.seconds_since_midnight());
            Ok(naive - offset_secs.round() as i64)
        }
    } else {
        if is_end_bound {
            cal_date = cal_date.add_days(1);
        }
        let midnight = CalTime {
            hour: 0,
            minute: 0,
            second: 0,
        };
        let utc = default_tz
            .resolve_local(cal_date, midnight, DstDisambiguation::Compatible)
            .map_or_else(
                |_| i64::from(cal_date.to_days_since_epoch()) * 86_400,
                |r| r.utc_timestamp,
            );
        Ok(utc)
    }
}

enum InputSource {
    Bytes(Vec<u8>),
    Path(PathBuf),
}

fn extract_input_source(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<InputSource> {
    if let Ok(b) = data.downcast::<PyBytes>() {
        return Ok(InputSource::Bytes(b.as_bytes().to_vec()));
    }
    if let Ok(ba) = data.downcast::<PyByteArray>() {
        return Ok(InputSource::Bytes(ba.to_vec()));
    }
    if let Ok(s) = data.downcast::<PyString>() {
        return Ok(InputSource::Bytes(s.to_cow()?.as_bytes().to_vec()));
    }
    if data.is_instance_of::<PyMemoryView>() {
        let b = data.call_method0("tobytes")?.downcast_into::<PyBytes>()?;
        return Ok(InputSource::Bytes(b.as_bytes().to_vec()));
    }
    let os_mod = py.import("os")?;
    if let Ok(fspath) = os_mod.call_method1("fspath", (data,)) {
        if let Ok(p) = fspath.extract::<PathBuf>() {
            return Ok(InputSource::Path(p));
        }
    }
    Err(PyTypeError::new_err(
        "expected bytes, bytearray, memoryview, str, or os.PathLike",
    ))
}

#[pyclass(name = "Component", module = "icaloxide._icaloxide", frozen)]
#[derive(Clone)]
pub struct PyComponent {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    id: ComponentId,
}

impl PyComponent {
    fn node_ref(&self) -> PyResult<icaloxide_core::ComponentRef<'_>> {
        self.cal
            .component(self.id)
            .ok_or_else(|| PyKeyError::new_err("invalid component id"))
    }

    fn registry(&self) -> &TzRegistry {
        self.tz_registry.get_or_init(|| self.cal.tz_registry())
    }
}

#[pymethods]
impl PyComponent {
    #[getter]
    fn name(&self) -> PyResult<String> {
        Ok(self.node_ref()?.kind().as_str().to_owned())
    }

    #[getter]
    fn uid(&self) -> PyResult<Option<String>> {
        Ok(self.node_ref()?.uid().map(str::to_owned))
    }

    #[getter]
    fn summary(&self) -> PyResult<Option<String>> {
        Ok(self.node_ref()?.summary().map(str::to_owned))
    }

    #[getter]
    fn description(&self) -> PyResult<Option<String>> {
        Ok(self.node_ref()?.description().map(str::to_owned))
    }

    #[getter]
    fn location(&self) -> PyResult<Option<String>> {
        Ok(self.node_ref()?.location().map(str::to_owned))
    }

    #[getter]
    fn status(&self) -> PyResult<Option<String>> {
        Ok(self.node_ref()?.status().map(str::to_owned))
    }

    #[pyo3(signature = (key, default=None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<PyObject>) -> PyResult<PyObject> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    fn get_all<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyList>> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        let reg = self.registry();
        let mut items = Vec::new();
        for prop in comp.properties_named(&pname) {
            items.push(property_value_to_py(py, reg, &prop.value)?);
        }
        PyList::new(py, items)
    }

    fn params<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyDict>> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        match comp.property(&pname) {
            Some(prop) => prop_params_to_py_dict(py, &prop.params),
            None => Ok(PyDict::new(py)),
        }
    }

    fn raw_properties<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<Vec<(String, PyObject, Bound<'py, PyDict>)>> {
        let comp = self.node_ref()?;
        let reg = self.registry();
        comp.properties()
            .iter()
            .map(|prop| {
                let val = property_value_to_py(py, reg, &prop.value)?.unbind();
                let params = prop_params_to_py_dict(py, &prop.params)?;
                Ok((prop.name.as_str().to_owned(), val, params))
            })
            .collect()
    }

    #[allow(clippy::type_complexity)]
    fn raw_properties_compact<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<Vec<(String, PyObject, Option<Bound<'py, PyDict>>)>> {
        let comp = self.node_ref()?;
        let reg = self.registry();
        comp.properties()
            .iter()
            .map(|prop| {
                let val = property_value_to_py(py, reg, &prop.value)?.unbind();
                let params = if prop.params.is_empty() {
                    None
                } else {
                    Some(prop_params_to_py_dict(py, &prop.params)?)
                };
                Ok((prop.name.as_str().to_owned(), val, params))
            })
            .collect()
    }

    fn get_all_with_params<'py>(
        &self,
        py: Python<'py>,
        key: &str,
    ) -> PyResult<Vec<(PyObject, Bound<'py, PyDict>)>> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        let reg = self.registry();
        comp.properties_named(&pname)
            .map(|prop| {
                let val = property_value_to_py(py, reg, &prop.value)?.unbind();
                let params = prop_params_to_py_dict(py, &prop.params)?;
                Ok((val, params))
            })
            .collect()
    }

    fn property_names(&self) -> PyResult<Vec<String>> {
        let comp = self.node_ref()?;
        Ok(comp
            .properties()
            .iter()
            .map(|p| p.name.as_str().to_owned())
            .collect())
    }

    #[getter]
    fn subcomponents(&self) -> PyResult<Vec<PyComponent>> {
        let comp = self.node_ref()?;
        Ok(comp
            .children()
            .map(|c| PyComponent {
                cal: Arc::clone(&self.cal),
                tz_registry: Arc::clone(&self.tz_registry),
                id: c.id(),
            })
            .collect())
    }

    #[pyo3(signature = (sorted=false))]
    fn to_ical<'py>(&self, py: Python<'py>, sorted: bool) -> PyResult<Bound<'py, PyBytes>> {
        let s = self
            .cal
            .component_to_ical_ordered(self.id, sorted)
            .ok_or_else(|| PyKeyError::new_err("invalid component id"))?;
        Ok(PyBytes::new(py, s.as_bytes()))
    }

    fn __reduce__<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<(Bound<'py, PyAny>, (Bound<'py, PyBytes>,))> {
        let parse_fn = py.import("icaloxide")?.getattr("parse_component")?;
        let raw = self.to_ical(py, false)?;
        Ok((parse_fn, (raw,)))
    }

    fn __getitem__(&self, py: Python<'_>, key: &str) -> PyResult<PyObject> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Err(PyKeyError::new_err(key.to_owned())),
        }
    }

    fn __contains__(&self, key: &str) -> PyResult<bool> {
        let comp = self.node_ref()?;
        let pname = PropertyName::parse(key);
        Ok(comp.property(&pname).is_some())
    }

    fn __repr__(&self) -> PyResult<String> {
        let comp = self.node_ref()?;
        let kind = comp.kind().as_str();
        match comp.summary() {
            Some(s) => Ok(format!("Component({kind:?}, summary={s:?})")),
            None => Ok(format!("Component({kind:?})")),
        }
    }
}

#[pyclass(name = "Occurrence", module = "icaloxide._icaloxide", frozen)]
#[derive(Clone)]
pub struct PyOccurrence {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    occ: Occurrence,
}

impl PyOccurrence {
    fn registry(&self) -> &TzRegistry {
        self.tz_registry.get_or_init(|| self.cal.tz_registry())
    }

    fn comp(&self) -> PyResult<icaloxide_core::ComponentRef<'_>> {
        self.cal
            .component(self.occ.component_id)
            .ok_or_else(|| PyKeyError::new_err("invalid component id"))
    }
    fn is_synthetic_end(&self, pname: &PropertyName) -> bool {
        (self.occ.kind == ComponentKind::VTodo && *pname == PropertyName::Due)
            || (!matches!(
                self.occ.kind,
                ComponentKind::VTodo | ComponentKind::VJournal | ComponentKind::VAlarm
            ) && *pname == PropertyName::DtEnd)
    }
}

#[pymethods]
impl PyOccurrence {
    #[getter]
    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.occ.is_undated {
            Ok(py.None().into_bound(py))
        } else if self.occ.is_all_day {
            cal_date_to_py(py, self.occ.start_local.date)
        } else {
            cal_datetime_to_py_with_utc(
                py,
                self.registry(),
                &self.occ.start_local,
                self.occ.start_utc,
            )
        }
    }

    #[getter]
    fn end<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.occ.is_undated {
            Ok(py.None().into_bound(py))
        } else if self.occ.is_all_day {
            cal_date_to_py(py, self.occ.end_local.date)
        } else {
            cal_datetime_to_py_with_utc(py, self.registry(), &self.occ.end_local, self.occ.end_utc)
        }
    }

    #[getter]
    fn is_all_day(&self) -> bool {
        self.occ.is_all_day
    }

    #[getter]
    fn name(&self) -> String {
        self.occ.kind.as_str().to_owned()
    }

    #[getter]
    fn uid(&self) -> PyResult<Option<String>> {
        Ok(self.comp()?.uid().map(str::to_owned))
    }

    #[getter]
    fn summary(&self) -> PyResult<Option<String>> {
        Ok(self.comp()?.summary().map(str::to_owned))
    }

    #[getter]
    fn description(&self) -> PyResult<Option<String>> {
        Ok(self.comp()?.description().map(str::to_owned))
    }

    #[getter]
    fn location(&self) -> PyResult<Option<String>> {
        Ok(self.comp()?.location().map(str::to_owned))
    }

    #[getter]
    fn status(&self) -> PyResult<Option<String>> {
        Ok(self.comp()?.status().map(str::to_owned))
    }

    #[getter]
    fn component(&self) -> PyComponent {
        PyComponent {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            id: self.occ.component_id,
        }
    }

    fn params<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyDict>> {
        let comp = self.comp()?;
        let pname = PropertyName::parse(key);
        let dict = PyDict::new(py);
        if let Some(prop) = comp.property(&pname) {
            for param in &prop.params {
                let vals = PyList::new(py, param.values.iter().map(CompactString::as_str))?;
                dict.set_item(param.name.as_str(), vals)?;
            }
        }
        Ok(dict)
    }

    #[pyo3(signature = (key, default=None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<PyObject>) -> PyResult<PyObject> {
        let pname = PropertyName::parse(key);
        if !self.occ.is_undated {
            if pname == PropertyName::DtStart {
                return Ok(self.start(py)?.unbind());
            }
            if self.is_synthetic_end(&pname) {
                return Ok(self.end(py)?.unbind());
            }
        }
        let comp = self.comp()?;
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    fn __getitem__(&self, py: Python<'_>, key: &str) -> PyResult<PyObject> {
        let pname = PropertyName::parse(key);
        if !self.occ.is_undated {
            if pname == PropertyName::DtStart {
                return Ok(self.start(py)?.unbind());
            }
            if self.is_synthetic_end(&pname) {
                return Ok(self.end(py)?.unbind());
            }
        }
        let comp = self.comp()?;
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Err(PyKeyError::new_err(key.to_owned())),
        }
    }

    fn __contains__(&self, key: &str) -> PyResult<bool> {
        let pname = PropertyName::parse(key);
        if !self.occ.is_undated && (pname == PropertyName::DtStart || self.is_synthetic_end(&pname))
        {
            return Ok(true);
        }
        Ok(self.comp()?.property(&pname).is_some())
    }

    #[pyo3(signature = (*, keep_recurrence_attributes=false))]
    fn to_ical<'py>(
        &self,
        py: Python<'py>,
        keep_recurrence_attributes: bool,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let comp = self.comp()?;
        let mut temp_cal = CoreCalendar::new();
        let copy_id = temp_cal.add_component(comp.kind().clone());
        let end_prop_name = if self.occ.kind == ComponentKind::VTodo {
            PropertyName::Due
        } else {
            PropertyName::DtEnd
        };
        if let Some(mut dst) = temp_cal.component_mut(copy_id) {
            for prop in comp.properties() {
                let is_span = matches!(
                    prop.name,
                    PropertyName::DtStart
                        | PropertyName::DtEnd
                        | PropertyName::Due
                        | PropertyName::Duration
                );
                let is_recur = matches!(
                    prop.name,
                    PropertyName::RRule
                        | PropertyName::RDate
                        | PropertyName::ExDate
                        | PropertyName::RecurrenceId
                );
                if is_span || (!keep_recurrence_attributes && is_recur) {
                    continue;
                }
                dst.add_property(prop.clone());
            }
            if !self.occ.is_undated {
                if self.occ.is_all_day {
                    dst.add_property(Property::new(
                        PropertyName::DtStart,
                        PropertyValue::Date(self.occ.start_local.date),
                    ));
                    if self.occ.kind != ComponentKind::VJournal {
                        dst.add_property(Property::new(
                            end_prop_name,
                            PropertyValue::Date(self.occ.end_local.date),
                        ));
                    }
                } else {
                    dst.add_property(Property::new(
                        PropertyName::DtStart,
                        PropertyValue::DateTime(self.occ.start_local.clone()),
                    ));
                    if self.occ.kind != ComponentKind::VJournal {
                        dst.add_property(Property::new(
                            end_prop_name,
                            PropertyValue::DateTime(self.occ.end_local.clone()),
                        ));
                    }
                }
            }
        }
        let serialized = temp_cal
            .component_to_ical(copy_id)
            .ok_or_else(|| PyKeyError::new_err("failed to serialize occurrence"))?;
        Ok(PyBytes::new(py, serialized.as_bytes()))
    }

    fn __repr__(&self) -> String {
        let kind = self.occ.kind.as_str();
        let summary = self
            .cal
            .component(self.occ.component_id)
            .and_then(icaloxide_core::ComponentRef::summary);
        match summary {
            Some(s) => format!(
                "Occurrence({kind:?}, start={}, summary={s:?})",
                self.occ.start_local
            ),
            None => format!("Occurrence({kind:?}, start={})", self.occ.start_local),
        }
    }
}

#[pyclass(name = "OccurrenceIterator", module = "icaloxide._icaloxide")]
pub struct PyOccurrenceIterator {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    iter: AfterIter,
    remaining: Option<usize>,
}

#[pymethods]
impl PyOccurrenceIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>) -> PyResult<Option<PyOccurrence>> {
        if let Some(rem) = slf.remaining {
            if rem == 0 {
                return Ok(None);
            }
            slf.remaining = Some(rem - 1);
        }
        match slf.iter.next() {
            Some(Ok(occ)) => Ok(Some(PyOccurrence {
                cal: Arc::clone(&slf.cal),
                tz_registry: Arc::clone(&slf.tz_registry),
                occ,
            })),
            Some(Err(e)) => Err(rrule_err_to_py(&e)),
            None => Ok(None),
        }
    }
}

#[pyclass(name = "OccurrencePageIterator", module = "icaloxide._icaloxide")]
pub struct PyOccurrencePageIterator {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    iter: AfterIter,
    page_size: usize,
    latest_start_utc: Option<i64>,
    exhausted: bool,
}

#[pymethods]
impl PyOccurrencePageIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>) -> PyResult<Option<Vec<PyOccurrence>>> {
        if slf.exhausted || slf.page_size == 0 {
            return Ok(None);
        }
        let mut page = Vec::with_capacity(slf.page_size);
        for _ in 0..slf.page_size {
            match slf.iter.next() {
                Some(Ok(occ)) => {
                    if slf.latest_start_utc.is_some_and(|cut| occ.start_utc > cut) {
                        slf.exhausted = true;
                        break;
                    }
                    page.push(PyOccurrence {
                        cal: Arc::clone(&slf.cal),
                        tz_registry: Arc::clone(&slf.tz_registry),
                        occ,
                    });
                }
                Some(Err(e)) => return Err(rrule_err_to_py(&e)),
                None => {
                    slf.exhausted = true;
                    break;
                }
            }
        }
        if page.is_empty() {
            Ok(None)
        } else {
            Ok(Some(page))
        }
    }
}

#[pyclass(name = "CalendarQuery", module = "icaloxide._icaloxide", frozen)]
pub struct PyCalendarQuery {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    query: Arc<CalendarQuery>,
}

#[pymethods]
impl PyCalendarQuery {
    #[getter]
    fn suppressed_errors(&self) -> Vec<String> {
        self.query
            .suppressed_errors()
            .iter()
            .map(ToString::to_string)
            .collect()
    }

    fn at(&self, py: Python<'_>, date_or_dt: &Bound<'_, PyAny>) -> PyResult<Vec<PyOccurrence>> {
        let occs = if date_or_dt.hasattr("hour")? {
            let instant_utc =
                py_time_bound_to_utc(date_or_dt, self.query.default_timezone(), false)?;
            py.allow_threads(|| self.query.at_instant(instant_utc))
                .map_err(|e| rrule_err_to_py(&e))?
        } else {
            let year = date_or_dt.getattr("year")?.extract::<i16>()?;
            let month = date_or_dt.getattr("month")?.extract::<u8>()?;
            let day = date_or_dt.getattr("day")?.extract::<u8>()?;
            let d =
                CalDate::new(year, month, day).map_err(|e| PyValueError::new_err(e.to_string()))?;
            py.allow_threads(|| self.query.at_date(d))
                .map_err(|e| rrule_err_to_py(&e))?
        };
        Ok(occs
            .into_iter()
            .map(|occ| PyOccurrence {
                cal: Arc::clone(&self.cal),
                tz_registry: Arc::clone(&self.tz_registry),
                occ,
            })
            .collect())
    }

    fn between(
        &self,
        py: Python<'_>,
        start: &Bound<'_, PyAny>,
        end: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<PyOccurrence>> {
        let start_utc = py_time_bound_to_utc(start, self.query.default_timezone(), false)?;
        let end_utc = py_time_bound_to_utc(end, self.query.default_timezone(), true)?;
        let occs = py
            .allow_threads(|| self.query.between(start_utc, end_utc))
            .map_err(|e| rrule_err_to_py(&e))?;
        Ok(occs
            .into_iter()
            .map(|occ| PyOccurrence {
                cal: Arc::clone(&self.cal),
                tz_registry: Arc::clone(&self.tz_registry),
                occ,
            })
            .collect())
    }

    #[pyo3(signature = (dt=None, *, limit=None))]
    fn after(
        &self,
        dt: Option<&Bound<'_, PyAny>>,
        limit: Option<usize>,
    ) -> PyResult<PyOccurrenceIterator> {
        let earliest_utc = if let Some(bound) = dt {
            py_time_bound_to_utc(bound, self.query.default_timezone(), false)?
        } else {
            self.query.min_start_floor()
        };
        Ok(PyOccurrenceIterator {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            iter: self.query.after(earliest_utc),
            remaining: limit,
        })
    }

    #[pyo3(signature = (page_size, earliest_end=None, latest_start=None))]
    fn paginate(
        &self,
        page_size: usize,
        earliest_end: Option<&Bound<'_, PyAny>>,
        latest_start: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<PyOccurrencePageIterator> {
        if page_size == 0 {
            return Err(PyValueError::new_err("page_size must be >= 1"));
        }
        let anchor_utc = if let Some(dt) = earliest_end {
            py_time_bound_to_utc(dt, self.query.default_timezone(), false)?
        } else {
            self.query.min_start_floor()
        };
        let latest_start_utc = match latest_start {
            Some(dt) => Some(py_time_bound_to_utc(
                dt,
                self.query.default_timezone(),
                true,
            )?),
            None => None,
        };
        Ok(PyOccurrencePageIterator {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            iter: self.query.after(anchor_utc),
            page_size,
            latest_start_utc,
            exhausted: false,
        })
    }

    fn first(&self, py: Python<'_>) -> PyResult<Option<PyOccurrence>> {
        let occ = py
            .allow_threads(|| self.query.first())
            .map_err(|e| rrule_err_to_py(&e))?;
        Ok(occ.map(|o| PyOccurrence {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            occ: o,
        }))
    }

    fn next_after(&self, py: Python<'_>, dt: &Bound<'_, PyAny>) -> PyResult<Option<PyOccurrence>> {
        let ts_utc = py_time_bound_to_utc(dt, self.query.default_timezone(), false)?;
        let occ = py
            .allow_threads(|| self.query.next_after(ts_utc))
            .map_err(|e| rrule_err_to_py(&e))?;
        Ok(occ.map(|o| PyOccurrence {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            occ: o,
        }))
    }

    fn all(&self, py: Python<'_>) -> PyResult<Vec<PyOccurrence>> {
        let occs = py
            .allow_threads(|| self.query.all())
            .map_err(|e| rrule_err_to_py(&e))?;
        Ok(occs
            .into_iter()
            .map(|occ| PyOccurrence {
                cal: Arc::clone(&self.cal),
                tz_registry: Arc::clone(&self.tz_registry),
                occ,
            })
            .collect())
    }

    fn count(
        &self,
        py: Python<'_>,
        start: &Bound<'_, PyAny>,
        end: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        let start_utc = py_time_bound_to_utc(start, self.query.default_timezone(), false)?;
        let end_utc = py_time_bound_to_utc(end, self.query.default_timezone(), true)?;
        py.allow_threads(|| self.query.count_between(start_utc, end_utc))
            .map_err(|e| rrule_err_to_py(&e))
    }

    fn count_all(&self, py: Python<'_>) -> PyResult<usize> {
        py.allow_threads(|| self.query.count_all())
            .map_err(|e| rrule_err_to_py(&e))
    }

    fn busy_intervals<'py>(
        &self,
        py: Python<'py>,
        start: &Bound<'py, PyAny>,
        end: &Bound<'py, PyAny>,
    ) -> PyResult<Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        let start_utc = py_time_bound_to_utc(start, self.query.default_timezone(), false)?;
        let end_utc = py_time_bound_to_utc(end, self.query.default_timezone(), true)?;
        let spans = py
            .allow_threads(|| self.query.busy_intervals(start_utc, end_utc))
            .map_err(|e| rrule_err_to_py(&e))?;
        let mut out = Vec::with_capacity(spans.len());
        for sp in spans {
            out.push((
                utc_ts_to_py(py, sp.start_utc)?,
                utc_ts_to_py(py, sp.end_utc)?,
            ));
        }
        Ok(out)
    }

    fn free_intervals<'py>(
        &self,
        py: Python<'py>,
        start: &Bound<'py, PyAny>,
        end: &Bound<'py, PyAny>,
    ) -> PyResult<Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        let start_utc = py_time_bound_to_utc(start, self.query.default_timezone(), false)?;
        let end_utc = py_time_bound_to_utc(end, self.query.default_timezone(), true)?;
        let spans = py
            .allow_threads(|| self.query.free_intervals(start_utc, end_utc))
            .map_err(|e| rrule_err_to_py(&e))?;
        let mut out = Vec::with_capacity(spans.len());
        for sp in spans {
            out.push((
                utc_ts_to_py(py, sp.start_utc)?,
                utc_ts_to_py(py, sp.end_utc)?,
            ));
        }
        Ok(out)
    }
}

#[pyclass(name = "Calendar", module = "icaloxide._icaloxide", frozen)]
pub struct PyCalendar {
    inner: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
}

#[pymethods]
impl PyCalendar {
    #[classmethod]
    #[pyo3(signature = (data, *, strict=false))]
    fn from_ical(
        _cls: &Bound<'_, pyo3::types::PyType>,
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        strict: bool,
    ) -> PyResult<Self> {
        let source = extract_input_source(py, data)?;
        let opts = if strict {
            ParseOptions::strict()
        } else {
            ParseOptions::lenient()
        };

        let cal = py.allow_threads(|| -> PyResult<CoreCalendar> {
            match source {
                InputSource::Bytes(bytes) => {
                    CoreCalendar::parse_with_options(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
                }
                InputSource::Path(path) => {
                    let bytes = std::fs::read(&path)
                        .map_err(|e| PyOSError::new_err(format!("{}: {e}", path.display())))?;
                    CoreCalendar::parse_with_options(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
                }
            }
        })?;

        Ok(Self {
            inner: Arc::new(cal),
            tz_registry: Arc::new(OnceLock::new()),
        })
    }

    #[classmethod]
    #[pyo3(signature = (data, *, strict=false))]
    fn parse_component(
        _cls: &Bound<'_, pyo3::types::PyType>,
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        strict: bool,
    ) -> PyResult<PyComponent> {
        parse_component(py, data, strict)
    }

    #[classmethod]
    #[pyo3(signature = (data, *, strict=false))]
    fn from_ical_multiple(
        _cls: &Bound<'_, pyo3::types::PyType>,
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        strict: bool,
    ) -> PyResult<Vec<Self>> {
        let source = extract_input_source(py, data)?;
        let opts = if strict {
            ParseOptions::strict()
        } else {
            ParseOptions::lenient()
        };

        let cals = py.allow_threads(|| -> PyResult<Vec<CoreCalendar>> {
            match source {
                InputSource::Bytes(bytes) => {
                    CoreCalendar::parse_all(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
                }
                InputSource::Path(path) => {
                    let bytes = std::fs::read(&path)
                        .map_err(|e| PyOSError::new_err(format!("{}: {e}", path.display())))?;
                    CoreCalendar::parse_all(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
                }
            }
        })?;

        Ok(cals
            .into_iter()
            .map(|cal| Self {
                inner: Arc::new(cal),
                tz_registry: Arc::new(OnceLock::new()),
            })
            .collect())
    }

    #[classmethod]
    #[pyo3(signature = (*, prodid=None, name=None))]
    fn new(
        _cls: &Bound<'_, pyo3::types::PyType>,
        prodid: Option<&str>,
        name: Option<&str>,
    ) -> Self {
        let mut cal = CoreCalendar::new();
        if let Some(pid) = prodid {
            cal.root_mut().set_property(Property::new(
                PropertyName::ProdId,
                PropertyValue::Text(CompactString::from(pid)),
            ));
        }
        if let Some(cal_name) = name {
            cal.root_mut().set_property(Property::new(
                PropertyName::Name,
                PropertyValue::Text(CompactString::from(cal_name)),
            ));
        }
        Self {
            inner: Arc::new(cal),
            tz_registry: Arc::new(OnceLock::new()),
        }
    }

    #[pyo3(signature = (*, components=None, skip_cancelled=false, skip_bad_series=false, include_undated=false, default_tz=None, max_iterations=None, max_periods=None))]
    #[allow(clippy::too_many_arguments)]
    fn query(
        &self,
        py: Python<'_>,
        components: Option<Vec<String>>,
        skip_cancelled: bool,
        skip_bad_series: bool,
        include_undated: bool,
        default_tz: Option<&str>,
        max_iterations: Option<usize>,
        max_periods: Option<usize>,
    ) -> PyResult<PyCalendarQuery> {
        let mut budget = IterationBudget::default();
        if let Some(mp) = max_periods.or(max_iterations) {
            budget.max_periods = mp;
        }
        let mut opts = QueryOptions {
            skip_cancelled,
            skip_bad_series,
            include_undated,
            budget,
            ..QueryOptions::default()
        };
        if let Some(list) = components {
            let mut kinds = SmallVec::new();
            for item in list {
                kinds.push(ComponentKind::parse(&item));
            }
            opts.components = kinds;
        }
        if let Some(tz_name) = default_tz {
            if let Some(resolved) = self.registry().resolve(tz_name) {
                opts.default_tz = Some(resolved);
            }
        }
        let cal_arc = Arc::clone(&self.inner);
        let q = py
            .allow_threads(|| CalendarQuery::new(cal_arc, opts))
            .map_err(|e| rrule_err_to_py(&e))?;
        Ok(PyCalendarQuery {
            cal: Arc::clone(&self.inner),
            tz_registry: Arc::clone(&self.tz_registry),
            query: Arc::new(q),
        })
    }

    fn add_missing_timezones(&self, py: Python<'_>) -> Self {
        let updated = py.allow_threads(|| {
            let mut cloned = (*self.inner).clone();
            let _ = cloned.add_missing_timezones();
            cloned
        });
        Self {
            inner: Arc::new(updated),
            tz_registry: Arc::new(OnceLock::new()),
        }
    }

    fn used_tzids(&self) -> Vec<String> {
        self.inner
            .used_tzids()
            .into_iter()
            .map(Into::into)
            .collect()
    }

    fn missing_tzids(&self) -> Vec<String> {
        self.inner
            .missing_tzids()
            .into_iter()
            .map(Into::into)
            .collect()
    }

    fn validate(&self) -> Vec<(usize, String)> {
        self.inner
            .validate()
            .into_iter()
            .map(|w| (w.line, w.message.to_string()))
            .collect()
    }

    fn with_uid(&self, uid: &str) -> Vec<PyComponent> {
        self.inner
            .components_with_uid(uid)
            .into_iter()
            .map(|id| PyComponent {
                cal: Arc::clone(&self.inner),
                tz_registry: Arc::clone(&self.tz_registry),
                id,
            })
            .collect()
    }

    #[pyo3(signature = (tzid, dt))]
    fn resolve_utc_offset<'py>(
        &self,
        py: Python<'py>,
        tzid: &str,
        dt: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let year = dt.getattr("year")?.extract::<i16>()?;
        let month = dt.getattr("month")?.extract::<u8>()?;
        let day = dt.getattr("day")?.extract::<u8>()?;
        let hour = dt.getattr("hour")?.extract::<u8>()?;
        let minute = dt.getattr("minute")?.extract::<u8>()?;
        let second = dt.getattr("second")?.extract::<u8>()?;

        let cal_date =
            CalDate::new(year, month, day).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let cal_time =
            CalTime::new(hour, minute, second).map_err(|e| PyValueError::new_err(e.to_string()))?;

        let reg = self.registry();
        let Some(cal_tz) = reg.resolve(tzid) else {
            return Ok(None);
        };
        let Ok(resolved) = cal_tz.resolve_local(cal_date, cal_time, DstDisambiguation::Compatible)
        else {
            return Ok(None);
        };
        let types = dt_types(py)?;
        let delta = types
            .timedelta_cls
            .bind(py)
            .call1((0_i32, resolved.offset_seconds))?;
        Ok(Some(delta))
    }

    #[pyo3(signature = (sorted=false))]
    fn to_ical<'py>(&self, py: Python<'py>, sorted: bool) -> Bound<'py, PyBytes> {
        let s = py.allow_threads(|| {
            if sorted {
                self.inner.to_ical_sorted()
            } else {
                self.inner.to_ical()
            }
        });
        PyBytes::new(py, s.as_bytes())
    }

    fn __reduce__<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<(Bound<'py, PyAny>, (Bound<'py, PyBytes>,))> {
        let unpickle_fn = py
            .import("icaloxide._icaloxide")?
            .getattr("_unpickle_calendar")?;
        let raw = self.to_ical(py, false);
        Ok((unpickle_fn, (raw,)))
    }

    #[getter]
    fn name(&self) -> Option<String> {
        self.inner.name().map(str::to_owned)
    }

    #[getter]
    fn prodid(&self) -> Option<String> {
        self.inner.prodid().map(str::to_owned)
    }

    #[getter]
    fn default_timezone(&self) -> Option<String> {
        self.registry().default_tzid().map(str::to_owned)
    }

    #[getter]
    fn component_count(&self) -> usize {
        self.inner.component_count()
    }

    #[getter]
    fn warnings(&self) -> Vec<(usize, String)> {
        self.inner
            .warnings()
            .iter()
            .map(|w| (w.line, w.message.to_string()))
            .collect()
    }

    #[getter]
    fn root(&self) -> PyComponent {
        PyComponent {
            cal: Arc::clone(&self.inner),
            tz_registry: Arc::clone(&self.tz_registry),
            id: self.inner.root_id(),
        }
    }

    #[getter]
    fn events(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VEvent)
    }

    #[getter]
    fn todos(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VTodo)
    }

    #[getter]
    fn journals(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VJournal)
    }

    #[getter]
    fn timezones(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VTimezone)
    }

    #[getter]
    fn freebusies(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VFreeBusy)
    }

    #[getter]
    fn availabilities(&self) -> Vec<PyComponent> {
        self.collect_kind(ComponentKind::VAvailability)
    }

    #[getter]
    fn subcomponents(&self) -> Vec<PyComponent> {
        self.inner
            .root()
            .children()
            .map(|c| PyComponent {
                cal: Arc::clone(&self.inner),
                tz_registry: Arc::clone(&self.tz_registry),
                id: c.id(),
            })
            .collect()
    }

    fn component_stats(&self) -> HashMap<String, usize> {
        let mut counts: HashMap<String, usize> = HashMap::new();
        for child in self.inner.root().children() {
            *counts.entry(child.kind().as_str().to_owned()).or_insert(0) += 1;
        }
        counts
    }

    fn __len__(&self) -> usize {
        self.inner.root().children().count()
    }

    fn __repr__(&self) -> String {
        let total = self.inner.root().children().count();
        format!("Calendar(subcomponents={total})")
    }
}

impl PyCalendar {
    fn registry(&self) -> &TzRegistry {
        self.tz_registry.get_or_init(|| self.inner.tz_registry())
    }

    fn collect_kind(&self, kind: ComponentKind) -> Vec<PyComponent> {
        self.inner
            .components_of_kind(kind)
            .map(|c| PyComponent {
                cal: Arc::clone(&self.inner),
                tz_registry: Arc::clone(&self.tz_registry),
                id: c.id(),
            })
            .collect()
    }
}

fn temporal_to_property(name: PropertyName, val: PyInputTemporal) -> Property {
    match val {
        PyInputTemporal::Date(d) => Property::new(name, PropertyValue::Date(d))
            .with_param(Parameter::new(ParamName::Value, "DATE")),
        PyInputTemporal::DateTime(dt) => Property::new(name, PropertyValue::DateTime(dt)),
    }
}

/// Mutable Rust-backed builder for constructing high-performance calendars without Python dict overhead.
#[pyclass(name = "CalendarBuilder", module = "icaloxide._icaloxide")]
pub struct PyCalendarBuilder {
    cal: CoreCalendar,
    last_component: Option<ComponentId>,
}

#[pymethods]
impl PyCalendarBuilder {
    #[new]
    #[pyo3(signature = (*, prodid=None, name=None))]
    fn new(prodid: Option<&str>, name: Option<&str>) -> Self {
        let mut cal = CoreCalendar::new();
        if let Some(pid) = prodid {
            cal.root_mut().set_property(Property::new(
                PropertyName::ProdId,
                PropertyValue::Text(CompactString::from(pid)),
            ));
        }
        if let Some(cal_name) = name {
            cal.root_mut().set_property(Property::new(
                PropertyName::Name,
                PropertyValue::Text(CompactString::from(cal_name)),
            ));
        }
        Self {
            cal,
            last_component: None,
        }
    }

    fn set_property(&mut self, name: &str, value: &str) {
        self.cal.root_mut().set_property(Property::new(
            PropertyName::parse(name),
            PropertyValue::Text(CompactString::from(value)),
        ));
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (*, uid, summary, start, end=None, duration=None, rrule=None, tzid=None, location=None, description=None, categories=None))]
    fn add_event(
        &mut self,
        uid: &str,
        summary: &str,
        start: &Bound<'_, PyAny>,
        end: Option<&Bound<'_, PyAny>>,
        duration: Option<&Bound<'_, PyAny>>,
        rrule: Option<&str>,
        tzid: Option<&str>,
        location: Option<&str>,
        description: Option<&str>,
        categories: Option<Vec<String>>,
    ) -> PyResult<()> {
        let comp_id = self.cal.add_component(ComponentKind::VEvent);
        let start_val = py_to_temporal(start, tzid)?;
        let end_val = match end {
            Some(e) => Some(py_to_temporal(e, tzid)?),
            None => None,
        };
        let dur_val = match duration {
            Some(d) => Some(py_duration_to_cal(d)?),
            None => None,
        };

        if let Some(mut ev) = self.cal.component_mut(comp_id) {
            ev.add_property(Property::new(
                PropertyName::Uid,
                PropertyValue::Text(CompactString::from(uid)),
            ));
            ev.add_property(Property::new(
                PropertyName::Summary,
                PropertyValue::Text(CompactString::from(summary)),
            ));
            ev.add_property(temporal_to_property(PropertyName::DtStart, start_val));
            if let Some(ev_end) = end_val {
                ev.add_property(temporal_to_property(PropertyName::DtEnd, ev_end));
            }
            if let Some(dur) = dur_val {
                ev.add_property(Property::new(
                    PropertyName::Duration,
                    PropertyValue::Duration(dur),
                ));
            }
            if let Some(r) = rrule {
                ev.add_property(Property::new(
                    PropertyName::RRule,
                    PropertyValue::Recur(CompactString::from(r)),
                ));
            }
            if let Some(loc) = location {
                ev.add_property(Property::new(
                    PropertyName::Location,
                    PropertyValue::Text(CompactString::from(loc)),
                ));
            }
            if let Some(desc) = description {
                ev.add_property(Property::new(
                    PropertyName::Description,
                    PropertyValue::Text(CompactString::from(desc)),
                ));
            }
            if let Some(cats) = categories {
                ev.add_property(Property::new(
                    PropertyName::Categories,
                    PropertyValue::TextList(cats.into_iter().map(CompactString::from).collect()),
                ));
            }
        }
        self.last_component = Some(comp_id);
        Ok(())
    }

    #[pyo3(signature = (*, uid, summary, start=None, due=None, tzid=None, status=None))]
    fn add_todo(
        &mut self,
        uid: &str,
        summary: &str,
        start: Option<&Bound<'_, PyAny>>,
        due: Option<&Bound<'_, PyAny>>,
        tzid: Option<&str>,
        status: Option<&str>,
    ) -> PyResult<()> {
        let comp_id = self.cal.add_component(ComponentKind::VTodo);
        let start_val = match start {
            Some(s) => Some(py_to_temporal(s, tzid)?),
            None => None,
        };
        let due_val = match due {
            Some(d) => Some(py_to_temporal(d, tzid)?),
            None => None,
        };

        if let Some(mut td) = self.cal.component_mut(comp_id) {
            td.add_property(Property::new(
                PropertyName::Uid,
                PropertyValue::Text(CompactString::from(uid)),
            ));
            td.add_property(Property::new(
                PropertyName::Summary,
                PropertyValue::Text(CompactString::from(summary)),
            ));
            if let Some(sv) = start_val {
                td.add_property(temporal_to_property(PropertyName::DtStart, sv));
            }
            if let Some(dv) = due_val {
                td.add_property(temporal_to_property(PropertyName::Due, dv));
            }
            if let Some(st) = status {
                td.add_property(Property::new(
                    PropertyName::Status,
                    PropertyValue::Text(CompactString::from(st)),
                ));
            }
        }
        self.last_component = Some(comp_id);
        Ok(())
    }

    #[pyo3(signature = (*, uid, summary, start=None, tzid=None, description=None))]
    fn add_journal(
        &mut self,
        uid: &str,
        summary: &str,
        start: Option<&Bound<'_, PyAny>>,
        tzid: Option<&str>,
        description: Option<&str>,
    ) -> PyResult<()> {
        let comp_id = self.cal.add_component(ComponentKind::VJournal);
        let start_val = match start {
            Some(s) => Some(py_to_temporal(s, tzid)?),
            None => None,
        };
        if let Some(mut jn) = self.cal.component_mut(comp_id) {
            jn.add_property(Property::new(
                PropertyName::Uid,
                PropertyValue::Text(CompactString::from(uid)),
            ));
            jn.add_property(Property::new(
                PropertyName::Summary,
                PropertyValue::Text(CompactString::from(summary)),
            ));
            if let Some(sv) = start_val {
                jn.add_property(temporal_to_property(PropertyName::DtStart, sv));
            }
            if let Some(desc) = description {
                jn.add_property(Property::new(
                    PropertyName::Description,
                    PropertyValue::Text(CompactString::from(desc)),
                ));
            }
        }
        self.last_component = Some(comp_id);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (*, trigger, action="DISPLAY", description=None, summary=None, duration=None, repeat=None, parent_uid=None))]
    fn add_alarm(
        &mut self,
        trigger: &Bound<'_, PyAny>,
        action: &str,
        description: Option<&str>,
        summary: Option<&str>,
        duration: Option<&Bound<'_, PyAny>>,
        repeat: Option<i64>,
        parent_uid: Option<&str>,
    ) -> PyResult<()> {
        let target_id = if let Some(uid) = parent_uid {
            self.cal
                .components_with_uid(uid)
                .into_iter()
                .next()
                .ok_or_else(|| PyKeyError::new_err(format!("no component with uid {uid:?}")))?
        } else {
            self.last_component
                .ok_or_else(|| PyValueError::new_err("no parent component added yet"))?
        };

        let trigger_prop = if trigger.hasattr("total_seconds")? {
            let dur = py_duration_to_cal(trigger)?;
            Property::new(PropertyName::Trigger, PropertyValue::Duration(dur))
        } else {
            match py_to_temporal(trigger, None)? {
                PyInputTemporal::DateTime(dt) => {
                    Property::new(PropertyName::Trigger, PropertyValue::DateTime(dt))
                        .with_param(Parameter::new(ParamName::Value, "DATE-TIME"))
                }
                PyInputTemporal::Date(d) => {
                    Property::new(PropertyName::Trigger, PropertyValue::Date(d))
                        .with_param(Parameter::new(ParamName::Value, "DATE"))
                }
            }
        };
        let dur_val = match duration {
            Some(d) => Some(py_duration_to_cal(d)?),
            None => None,
        };

        let alarm_id = self
            .cal
            .add_child_component(target_id, ComponentKind::VAlarm)
            .ok_or_else(|| PyKeyError::new_err("invalid parent component"))?;
        if let Some(mut al) = self.cal.component_mut(alarm_id) {
            al.add_property(Property::new(
                PropertyName::Action,
                PropertyValue::Text(CompactString::from(action)),
            ));
            al.add_property(trigger_prop);
            if let Some(desc) = description {
                al.add_property(Property::new(
                    PropertyName::Description,
                    PropertyValue::Text(CompactString::from(desc)),
                ));
            }
            if let Some(sum) = summary {
                al.add_property(Property::new(
                    PropertyName::Summary,
                    PropertyValue::Text(CompactString::from(sum)),
                ));
            }
            if let Some(dur) = dur_val {
                al.add_property(Property::new(
                    PropertyName::Duration,
                    PropertyValue::Duration(dur),
                ));
            }
            if let Some(rep) = repeat {
                al.add_property(Property::new(
                    PropertyName::Repeat,
                    PropertyValue::Integer(rep),
                ));
            }
        }
        Ok(())
    }

    fn build(&self) -> PyCalendar {
        PyCalendar {
            inner: Arc::new(self.cal.clone()),
            tz_registry: Arc::new(OnceLock::new()),
        }
    }

    fn to_ical<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let s = py.allow_threads(|| self.cal.to_ical());
        PyBytes::new(py, s.as_bytes())
    }
}

#[pyfunction]
#[pyo3(signature = (data, *, strict=false))]
fn parse_component(py: Python<'_>, data: &Bound<'_, PyAny>, strict: bool) -> PyResult<PyComponent> {
    let source = extract_input_source(py, data)?;
    let opts = if strict {
        ParseOptions::strict()
    } else {
        ParseOptions::lenient()
    };

    let cal = py.allow_threads(|| -> PyResult<CoreCalendar> {
        match source {
            InputSource::Bytes(bytes) => {
                CoreCalendar::parse_component(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
            }
            InputSource::Path(path) => {
                let bytes = std::fs::read(&path)
                    .map_err(|e| PyOSError::new_err(format!("{}: {e}", path.display())))?;
                CoreCalendar::parse_component(&bytes, &opts).map_err(|e| parse_err_to_py(&e))
            }
        }
    })?;

    let root = cal.root_id();
    Ok(PyComponent {
        cal: Arc::new(cal),
        tz_registry: Arc::new(OnceLock::new()),
        id: root,
    })
}

#[pyfunction]
#[pyo3(signature = (tzid, start_year=1970, end_year=2038))]
fn vtimezone_for_tzid<'py>(
    py: Python<'py>,
    tzid: &str,
    start_year: i16,
    end_year: i16,
) -> Option<Bound<'py, PyBytes>> {
    let cal = build_vtimezone_for_tzid(tzid, start_year, end_year)?;
    let rendered = cal.to_ical();
    Some(PyBytes::new(py, rendered.as_bytes()))
}

#[pyfunction]
#[allow(clippy::type_complexity)]
fn compile_vtimezone_transitions(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
) -> PyResult<Vec<(i64, i32, i32, bool, Option<String>)>> {
    let source = extract_input_source(py, data)?;
    let bytes = match source {
        InputSource::Bytes(b) => b,
        InputSource::Path(p) => {
            std::fs::read(&p).map_err(|e| PyOSError::new_err(format!("{}: {e}", p.display())))?
        }
    };
    let cal = CoreCalendar::parse_component(&bytes, &ParseOptions::lenient())
        .map_err(|e| parse_err_to_py(&e))?;
    let root = cal.root();
    let vtz = if root.kind() == &ComponentKind::VTimezone {
        root
    } else {
        cal.timezones()
            .next()
            .ok_or_else(|| PyValueError::new_err("no VTIMEZONE component found"))?
    };
    let compiled =
        CompiledVTimezone::compile(vtz).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(compiled
        .transitions
        .into_iter()
        .map(|t| {
            (
                t.utc_timestamp,
                t.offset_from,
                t.offset_to,
                t.is_dst,
                t.tzname.map(|s| s.to_string()),
            )
        })
        .collect())
}

#[pyfunction]
fn fold_ical_lines<'py>(
    py: Python<'py>,
    lines: &Bound<'py, PyList>,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut out = String::with_capacity(lines.len().saturating_mul(48));
    for item in lines.iter() {
        let s = item.downcast::<PyString>()?;
        let text = s.to_cow()?;
        write_folded_line(text.as_ref(), &mut out)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
    }
    Ok(PyBytes::new(py, out.as_bytes()))
}

#[pyfunction]
fn resolve_tzid(tzid: &str) -> Option<String> {
    let reg = TzRegistry::default();
    if let Some(canon) = reg.resolve_canonical_iana(tzid) {
        return Some(canon.into_string());
    }
    let stripped = strip_vendor_prefix(tzid).unwrap_or(tzid);
    if reg.resolve(stripped).is_some() {
        return Some(stripped.to_owned());
    }
    None
}

#[pyfunction]
fn _unpickle_calendar(py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<PyCalendar> {
    let bytes = data.as_bytes().to_vec();
    let cal = py
        .allow_threads(|| CoreCalendar::parse_with_options(&bytes, &ParseOptions::lenient()))
        .map_err(|e| parse_err_to_py(&e))?;
    Ok(PyCalendar {
        inner: Arc::new(cal),
        tz_registry: Arc::new(OnceLock::new()),
    })
}

#[pymodule]
fn _icaloxide(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<PyCalendar>()?;
    m.add_class::<PyCalendarBuilder>()?;
    m.add_class::<PyComponent>()?;
    m.add_class::<PyCalendarQuery>()?;
    m.add_class::<PyOccurrence>()?;
    m.add_class::<PyOccurrenceIterator>()?;
    m.add_class::<PyOccurrencePageIterator>()?;
    m.add_function(wrap_pyfunction!(_unpickle_calendar, m)?)?;
    m.add_function(wrap_pyfunction!(parse_component, m)?)?;
    m.add_function(wrap_pyfunction!(vtimezone_for_tzid, m)?)?;
    m.add_function(wrap_pyfunction!(compile_vtimezone_transitions, m)?)?;
    m.add_function(wrap_pyfunction!(fold_ical_lines, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_tzid, m)?)?;
    Ok(())
}
