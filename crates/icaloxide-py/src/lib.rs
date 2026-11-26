#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use compact_str::CompactString;
use icaloxide_core::{
    CalDate, CalDateTime, CalDuration, CalPeriod, CalTime, Calendar as CoreCalendar, ComponentId,
    ComponentKind, DstDisambiguation, ParseOptions, PropertyName, PropertyValue, TimeZoneRef,
    TzRegistry, UtcOffset,
};
use icaloxide_rrule::{AfterIter, CalendarQuery, Occurrence, QueryOptions, RRuleError};
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
        })
    })
}

fn parse_err_to_py(err: &icaloxide_core::ParseError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

fn rrule_err_to_py(err: &RRuleError) -> PyErr {
    PyValueError::new_err(err.to_string())
}

/// Convert a `CalDate` into a Python `datetime.date`.
fn cal_date_to_py(py: Python<'_>, d: CalDate) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    types
        .date_cls
        .bind(py)
        .call1((i32::from(d.year), d.month, d.day))
}

/// Convert a `CalTime` into a Python `datetime.time`.
fn cal_time_to_py(py: Python<'_>, t: CalTime) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    types.time_cls.bind(py).call1((t.hour, t.minute, t.second))
}

fn fixed_tz_to_py<'py>(
    py: Python<'py>,
    offset_seconds: i32,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let types = dt_types(py)?;
    let delta = types
        .timedelta_cls
        .bind(py)
        .call1((0_i32, offset_seconds))?;
    types.timezone_cls.bind(py).call1((delta, name))
}

/// Resolve an optional `TimeZoneRef` into a Python `tzinfo` object using `TzRegistry` + `zoneinfo`.
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

            // Prefer custom compiled VTIMEZONE when TZID is not a standard IANA zone.
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

            // Try Python's ZoneInfo using canonical IANA name (handles Windows/Outlook CLDR aliases too).
            let lookup_key = canonical.as_deref().unwrap_or(id.as_str());
            if let Some(zi_cls) = &types.zoneinfo_cls {
                if let Ok(zi) = zi_cls.bind(py).call1((lookup_key,)) {
                    return Ok(Some(zi));
                }
            }

            // Fallback to bundled Rust IANA TZDB / VTIMEZONE / fixed-offset engine.
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

/// Convert a `CalDateTime` into a Python `datetime.datetime`.
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

/// Convert a `CalDuration` into a Python `datetime.timedelta`.
fn cal_duration_to_py(py: Python<'_>, dur: CalDuration) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let total = dur.total_seconds();
    let days = total.div_euclid(86_400) as i32;
    let secs = total.rem_euclid(86_400) as i32;
    types.timedelta_cls.bind(py).call1((days, secs))
}

/// Convert a `CalPeriod` into a Python tuple `(start_datetime, end_datetime_or_timedelta)`.
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

/// Convert a `UtcOffset` into a Python `datetime.timedelta`.
fn utc_offset_to_py(py: Python<'_>, offset: UtcOffset) -> PyResult<Bound<'_, PyAny>> {
    let types = dt_types(py)?;
    let total = i64::from(offset.seconds);
    let days = total.div_euclid(86_400) as i32;
    let secs = total.rem_euclid(86_400) as i32;
    types.timedelta_cls.bind(py).call1((days, secs))
}

/// Convert a Rust `PropertyValue` lazily into a native Python object on demand.
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
        PropertyValue::Time(t) => cal_time_to_py(py, *t),
    }
}

/// Extract a query boundary from Python `datetime.datetime` or `datetime.date` into `(utc_seconds, is_date_only)`.
fn py_time_bound_to_utc(
    obj: &Bound<'_, PyAny>,
    default_tz: &icaloxide_core::CalendarTimeZone,
    is_end_bound: bool,
) -> PyResult<i64> {
    let year = obj.getattr("year")?.extract::<i16>()?;
    let month = obj.getattr("month")?.extract::<u8>()?;
    let day = obj.getattr("day")?.extract::<u8>()?;
    let mut cal_date =
        CalDate::new(year, month, day).map_err(|e| PyValueError::new_err(e.to_string()))?;

    // Must check datetime attributes BEFORE date because datetime subclasses date in Python.
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

/// Read raw bytes from `bytes`, `bytearray`, `memoryview`, `str`, or `os.PathLike`.
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

/// Lazy proxy handle for a single iCalendar component (`VEVENT`, `VTODO`, `VALARM`, etc.).
#[pyclass(name = "Component", frozen)]
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
        let dict = PyDict::new(py);
        if let Some(prop) = comp.property(&pname) {
            for param in &prop.params {
                let vals = PyList::new(py, param.values.iter().map(CompactString::as_str))?;
                dict.set_item(param.name.as_str(), vals)?;
            }
        }
        Ok(dict)
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

    fn to_ical<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let s = self
            .cal
            .component_to_ical(self.id)
            .ok_or_else(|| PyKeyError::new_err("invalid component id"))?;
        Ok(PyBytes::new(py, s.as_bytes()))
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

/// Expanded occurrence of a recurring or single calendar component.
#[pyclass(name = "Occurrence", frozen)]
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
}

#[pymethods]
impl PyOccurrence {
    #[getter]
    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.occ.is_all_day {
            cal_date_to_py(py, self.occ.start_local.date)
        } else {
            cal_datetime_to_py(py, self.registry(), &self.occ.start_local)
        }
    }

    #[getter]
    fn end<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.occ.is_all_day {
            cal_date_to_py(py, self.occ.end_local.date)
        } else {
            cal_datetime_to_py(py, self.registry(), &self.occ.end_local)
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

    #[pyo3(signature = (key, default=None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<PyObject>) -> PyResult<PyObject> {
        let pname = PropertyName::parse(key);
        if pname == PropertyName::DtStart {
            return Ok(self.start(py)?.unbind());
        }
        if pname == PropertyName::DtEnd {
            return Ok(self.end(py)?.unbind());
        }
        let comp = self.comp()?;
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    fn __getitem__(&self, py: Python<'_>, key: &str) -> PyResult<PyObject> {
        let pname = PropertyName::parse(key);
        if pname == PropertyName::DtStart {
            return Ok(self.start(py)?.unbind());
        }
        if pname == PropertyName::DtEnd {
            return Ok(self.end(py)?.unbind());
        }
        let comp = self.comp()?;
        match comp.property(&pname) {
            Some(prop) => Ok(property_value_to_py(py, self.registry(), &prop.value)?.unbind()),
            None => Err(PyKeyError::new_err(key.to_owned())),
        }
    }

    fn __contains__(&self, key: &str) -> PyResult<bool> {
        let pname = PropertyName::parse(key);
        if matches!(pname, PropertyName::DtStart | PropertyName::DtEnd) {
            return Ok(true);
        }
        Ok(self.comp()?.property(&pname).is_some())
    }

    fn __repr__(&self) -> String {
        let kind = self.occ.kind.as_str();
        let summary = self
            .cal
            .component(self.occ.component_id)
            .and_then(|c| c.summary().map(str::to_owned));
        match summary {
            Some(s) => format!(
                "Occurrence({kind:?}, start={}, summary={s:?})",
                self.occ.start_local
            ),
            None => format!("Occurrence({kind:?}, start={})", self.occ.start_local),
        }
    }
}

/// Streaming lazy iterator yielding [`Occurrence`] instances in chronological order.
#[pyclass(name = "OccurrenceIterator")]
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

/// Interval-indexed recurring calendar query engine.
#[pyclass(name = "CalendarQuery", frozen)]
pub struct PyCalendarQuery {
    cal: Arc<CoreCalendar>,
    tz_registry: Arc<OnceLock<TzRegistry>>,
    query: Arc<CalendarQuery>,
}

#[pymethods]
impl PyCalendarQuery {
    /// Return all occurrences falling on `date_or_dt` (`datetime.date` or `datetime.datetime`).
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

    /// Return all occurrences intersecting `[start, end)` (`datetime` or inclusive `date` range).
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

    /// Return a streaming chronological iterator of occurrences ending after `dt`.
    #[pyo3(signature = (dt, *, limit=None))]
    fn after(&self, dt: &Bound<'_, PyAny>, limit: Option<usize>) -> PyResult<PyOccurrenceIterator> {
        let earliest_utc = py_time_bound_to_utc(dt, self.query.default_timezone(), false)?;
        Ok(PyOccurrenceIterator {
            cal: Arc::clone(&self.cal),
            tz_registry: Arc::clone(&self.tz_registry),
            iter: self.query.after(earliest_utc),
            remaining: limit,
        })
    }

    /// Count occurrences intersecting `[start, end)` without allocating Python objects.
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
}

/// High-performance Rust-backed iCalendar container.
#[pyclass(name = "Calendar", frozen)]
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
    #[pyo3(signature = (*, prodid=None, name=None))]
    fn new(
        _cls: &Bound<'_, pyo3::types::PyType>,
        prodid: Option<&str>,
        name: Option<&str>,
    ) -> Self {
        let mut cal = CoreCalendar::new();
        if let Some(pid) = prodid {
            cal.root_mut().set_property(icaloxide_core::Property::new(
                PropertyName::ProdId,
                PropertyValue::Text(CompactString::from(pid)),
            ));
        }
        if let Some(cal_name) = name {
            cal.root_mut().set_property(icaloxide_core::Property::new(
                PropertyName::Name,
                PropertyValue::Text(CompactString::from(cal_name)),
            ));
        }
        Self {
            inner: Arc::new(cal),
            tz_registry: Arc::new(OnceLock::new()),
        }
    }

    /// Build an indexed [`CalendarQuery`] engine for recurring/non-recurring queries.
    #[pyo3(signature = (*, components=None, skip_cancelled=false))]
    fn query(
        &self,
        py: Python<'_>,
        components: Option<Vec<String>>,
        skip_cancelled: bool,
    ) -> PyResult<PyCalendarQuery> {
        let mut opts = QueryOptions {
            skip_cancelled,
            ..QueryOptions::default()
        };
        if let Some(list) = components {
            let mut kinds = SmallVec::new();
            for item in list {
                kinds.push(ComponentKind::parse(&item));
            }
            opts.components = kinds;
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

    fn to_ical<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let s = py.allow_threads(|| self.inner.to_ical());
        PyBytes::new(py, s.as_bytes())
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

#[pymodule]
fn _icaloxide(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<PyCalendar>()?;
    m.add_class::<PyComponent>()?;
    m.add_class::<PyCalendarQuery>()?;
    m.add_class::<PyOccurrence>()?;
    m.add_class::<PyOccurrenceIterator>()?;
    Ok(())
}
