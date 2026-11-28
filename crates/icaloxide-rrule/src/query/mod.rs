pub mod components;

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::sync::Arc;

use compact_str::CompactString;
use icaloxide_core::datetime::{CalDate, CalDateTime, CalTime};
use icaloxide_core::model::{
    Calendar, ComponentId, ComponentKind, ComponentRef, PropertyName, PropertyValue,
};
use icaloxide_core::tz::{CalendarTimeZone, DstDisambiguation, TzRegistry};
use rustc_hash::FxHashMap;
use smallvec::SmallVec;

use crate::error::{IterationBudget, RRuleError};
use crate::query::components::{
    expand_alarms_for_parent, extract_absolute_alarms, extract_freebusy_occurrences,
};
use crate::recurrence::series::{
    apply_this_and_future_shift, compute_end_local, compute_end_local_with_tz,
    extract_component_timing, overlaps_window, resolve_datetime_with_fallback,
    resolve_local_instant, resolve_until_utc, should_emit_master_dtstart, span_from_duration,
    utc_to_local_approx, CompiledSeries, OverrideEntry,
};
use crate::recurrence::SpanDuration;
use crate::rrule::{LocalDateTime, RRuleIter};

/// Merged opaque busy or free UTC time span `[start_utc, end_utc)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BusyInterval {
    pub start_utc: i64,
    pub end_utc: i64,
}

/// A resolved calendar occurrence emitted by [`CalendarQuery`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Occurrence {
    pub component_id: ComponentId,
    pub kind: ComponentKind,
    pub start_utc: i64,
    pub end_utc: i64,
    pub start_local: CalDateTime,
    pub end_local: CalDateTime,
    pub is_all_day: bool,
    pub is_undated: bool,
    pub recurrence_id_utc: Option<i64>,
    pub parent_id: Option<ComponentId>,
}

/// Configuration for building a [`CalendarQuery`].
#[derive(Debug, Clone)]
pub struct QueryOptions {
    pub components: SmallVec<[ComponentKind; 4]>,
    pub skip_cancelled: bool,
    pub skip_bad_series: bool,
    pub include_undated: bool,
    pub default_tz: Option<CalendarTimeZone>,
    pub budget: IterationBudget,
}

impl Default for QueryOptions {
    fn default() -> Self {
        let mut components = SmallVec::new();
        components.push(ComponentKind::VEvent);
        Self {
            components,
            skip_cancelled: false,
            skip_bad_series: false,
            include_undated: false,
            default_tz: None,
            budget: IterationBudget::default(),
        }
    }
}

/// Interval-indexed calendar query engine supporting `at`, `between`, `after`, `count`, `first`, and `all`.
#[derive(Debug)]
pub struct CalendarQuery {
    cal: Arc<Calendar>,
    reg: TzRegistry,
    default_tz: CalendarTimeZone,
    has_explicit_default_tz: bool,
    options: QueryOptions,
    singles: Vec<Occurrence>,
    undated: Vec<Occurrence>,
    singles_prefix_max_end: Vec<i64>,
    series: Vec<CompiledSeries>,
    alarm_parent_singles: Vec<Occurrence>,
    alarm_parent_series: Vec<CompiledSeries>,
    want_alarms: bool,
    max_alarm_pad_secs: i64,
    suppressed_errors: Vec<CompactString>,
}

impl CalendarQuery {
    /// Build an indexed [`CalendarQuery`] over `cal` using `options`.
    pub fn new(cal: Arc<Calendar>, options: QueryOptions) -> Result<Self, RRuleError> {
        let reg = cal.tz_registry();
        let reg_default_tz = reg.default_timezone();
        let has_explicit_default_tz = options.default_tz.is_some() || reg_default_tz.is_some();
        let default_tz = options
            .default_tz
            .clone()
            .or(reg_default_tz)
            .unwrap_or(CalendarTimeZone::Utc);

        let want_alarms = options.components.contains(&ComponentKind::VAlarm);
        let want_availability = options.components.contains(&ComponentKind::VAvailability)
            || options.components.contains(&ComponentKind::Available);
        let want_non_alarm: SmallVec<[ComponentKind; 4]> = options
            .components
            .iter()
            .filter(|k| *k != &ComponentKind::VAlarm)
            .cloned()
            .collect();

        let mut singles = Vec::new();
        let mut undated = Vec::new();
        let mut series = Vec::new();
        let mut alarm_parent_singles = Vec::new();
        let mut alarm_parent_series = Vec::new();
        let mut suppressed_errors = Vec::new();
        let mut max_alarm_pad_secs: i64 = 30 * 86_400;

        if want_non_alarm.contains(&ComponentKind::VFreeBusy) {
            for fb in cal.freebusies() {
                singles.extend(extract_freebusy_occurrences(fb, &reg, &default_tz));
            }
        }

        let (grouped, ungrouped) = partition_query_components(
            &cal,
            &want_non_alarm,
            want_alarms,
            want_availability,
            &mut max_alarm_pad_secs,
        );

        let mut push_group = |comps: &[ComponentRef<'_>], is_direct: bool, has_alarm: bool| {
            let res = classify_component_group(
                comps,
                &reg,
                &default_tz,
                &options,
                is_direct,
                has_alarm,
                &mut singles,
                &mut undated,
                &mut series,
                &mut alarm_parent_singles,
                &mut alarm_parent_series,
            );
            if let Err(err) = res {
                if options.skip_bad_series {
                    suppressed_errors.push(CompactString::from(err.to_string()));
                } else {
                    return Err(err);
                }
            }
            Ok(())
        };

        for comp in ungrouped {
            let is_direct = want_non_alarm.contains(comp.kind())
                || (want_availability && comp.kind() == &ComponentKind::Available);
            let has_alarm = want_alarms && has_valarm_child(comp);
            push_group(&[comp], is_direct, has_alarm)?;
        }

        for ((kind, _), comps) in grouped {
            let is_direct = want_non_alarm.contains(&kind)
                || (want_availability && kind == ComponentKind::Available);
            let has_alarm = want_alarms && comps.iter().any(|c| has_valarm_child(*c));
            push_group(&comps, is_direct, has_alarm)?;
        }

        singles.sort_by_key(|o| (o.start_utc, o.end_utc, o.component_id.0));
        let mut prefix_max = Vec::with_capacity(singles.len());
        let mut cur_max = i64::MIN;
        for s in &singles {
            cur_max = cur_max.max(s.end_utc);
            prefix_max.push(cur_max);
        }

        alarm_parent_singles.sort_by_key(|o| (o.start_utc, o.end_utc, o.component_id.0));

        Ok(Self {
            cal,
            reg,
            default_tz,
            has_explicit_default_tz,
            options,
            singles,
            undated,
            singles_prefix_max_end: prefix_max,
            series,
            alarm_parent_singles,
            alarm_parent_series,
            want_alarms,
            max_alarm_pad_secs: max_alarm_pad_secs.min(365 * 86_400),
            suppressed_errors,
        })
    }

    /// Borrow the underlying [`Calendar`].
    pub fn calendar(&self) -> &Calendar {
        &self.cal
    }

    /// Borrow the [`TzRegistry`] used by this query.
    pub fn tz_registry(&self) -> &TzRegistry {
        &self.reg
    }

    /// Default timezone used when resolving floating dates/times and `at_date` boundaries.
    pub fn default_timezone(&self) -> &CalendarTimeZone {
        &self.default_tz
    }

    /// Errors suppressed during query construction when `skip_bad_series` is enabled.
    pub fn suppressed_errors(&self) -> &[CompactString] {
        &self.suppressed_errors
    }

    /// Return all occurrences intersecting `[start_utc, end_utc)`.
    pub fn between(&self, start_utc: i64, end_utc: i64) -> Result<Vec<Occurrence>, RRuleError> {
        if end_utc < start_utc {
            return Ok(Vec::new());
        }
        let mut out = Vec::new();
        self.collect_singles_in_window(start_utc, end_utc, &mut out);
        out.extend(self.undated.iter().cloned());

        for s in &self.series {
            let mut buf = Vec::new();
            match s.expand_window(
                start_utc,
                end_utc,
                self.options.skip_cancelled,
                self.options.budget,
                &mut buf,
            ) {
                Ok(()) => out.extend(buf),
                Err(err) if self.options.skip_bad_series => {
                    let _ = err;
                }
                Err(err) => return Err(err),
            }
        }

        if self.want_alarms {
            self.collect_alarms_in_window(start_utc, end_utc, &mut out)?;
        }

        out.sort_by_key(|o| (o.start_utc, o.end_utc, o.component_id.0));
        Ok(out)
    }

    /// Return all occurrences falling on calendar date `date` (`[00:00, next day 00:00)` in `default_tz`).
    pub fn at_date(&self, date: CalDate) -> Result<Vec<Occurrence>, RRuleError> {
        if !self.has_explicit_default_tz {
            let base_utc = i64::from(date.to_days_since_epoch()) * 86_400;
            let next_date = date.add_days(1);
            let next_utc = i64::from(next_date.to_days_since_epoch()) * 86_400;
            let mut occs = self.between(base_utc - 54_000, next_utc + 54_000)?;
            occs.retain(|occ| {
                if occ.is_undated {
                    return true;
                }
                if occ.is_all_day {
                    return occ.start_local.date < next_date
                        && (occ.end_local.date > date
                            || (occ.end_local.date == occ.start_local.date
                                && occ.start_local.date == date));
                }
                let s_local = occ.start_local.to_naive_timestamp();
                let e_local = if occ.start_local.tz == occ.end_local.tz {
                    occ.end_local.to_naive_timestamp()
                } else {
                    s_local + (occ.end_utc - occ.start_utc).max(0)
                };
                overlaps_window(s_local, e_local, base_utc, next_utc)
            });
            return Ok(occs);
        }
        let midnight = CalTime {
            hour: 0,
            minute: 0,
            second: 0,
        };
        let start_utc = self
            .default_tz
            .resolve_local(date, midnight, DstDisambiguation::Compatible)
            .map_or_else(
                |_| i64::from(date.to_days_since_epoch()) * 86_400,
                |r| r.utc_timestamp,
            );
        let next_date = date.add_days(1);
        let end_utc = self
            .default_tz
            .resolve_local(next_date, midnight, DstDisambiguation::Compatible)
            .map_or_else(
                |_| i64::from(next_date.to_days_since_epoch()) * 86_400,
                |r| r.utc_timestamp,
            );
        self.between(start_utc, end_utc)
    }

    /// Return all occurrences active at exact instant `utc_ts`.
    pub fn at_instant(&self, utc_ts: i64) -> Result<Vec<Occurrence>, RRuleError> {
        self.between(utc_ts, utc_ts + 1)
    }

    /// Return the earliest occurrence in the calendar, if any.
    pub fn first(self: &Arc<Self>) -> Result<Option<Occurrence>, RRuleError> {
        let floor_utc = self.min_start_floor();
        match self.after(floor_utc).next() {
            Some(Ok(occ)) => Ok(Some(occ)),
            Some(Err(e)) => Err(e),
            None => Ok(None),
        }
    }

    /// Return the first occurrence ending after `ts_utc`, if any.
    pub fn next_after(self: &Arc<Self>, ts_utc: i64) -> Result<Option<Occurrence>, RRuleError> {
        match self.after(ts_utc).next() {
            Some(Ok(occ)) => Ok(Some(occ)),
            Some(Err(e)) => Err(e),
            None => Ok(None),
        }
    }

    /// Return all occurrences across the calendar's bounded/finite span (or `[min_start, 2100-12-31]` when infinite).
    pub fn all(&self) -> Result<Vec<Occurrence>, RRuleError> {
        if let Some((min_s, max_e)) = self.bounded_extents() {
            return self.between(min_s.saturating_sub(1), max_e.saturating_add(1));
        }
        let all_finite = self.series.iter().all(CompiledSeries::is_finite)
            && (!self.want_alarms
                || self
                    .alarm_parent_series
                    .iter()
                    .all(CompiledSeries::is_finite));
        if all_finite {
            let start_utc = self.min_start_floor();
            let y9999_utc =
                i64::from(CalDate::new(9999, 12, 31).unwrap().to_days_since_epoch()) * 86_400;
            return self.between(start_utc, y9999_utc);
        }
        let y1900_utc = i64::from(CalDate::new(1900, 1, 1).unwrap().to_days_since_epoch()) * 86_400;
        let start_utc = self.min_start_floor().min(y1900_utc);
        let end_utc = i64::from(CalDate::new(2100, 12, 31).unwrap().to_days_since_epoch()) * 86_400;
        self.between(start_utc, end_utc)
    }

    /// Count all finite occurrences across any year range, bounded by [`IterationBudget`].
    pub fn count_all(self: &Arc<Self>) -> Result<usize, RRuleError> {
        if self.series.iter().all(|s| s.max_end_utc.is_some())
            && (!self.want_alarms
                || self
                    .alarm_parent_series
                    .iter()
                    .all(|s| s.max_end_utc.is_some()))
        {
            let Some((min_s, max_e)) = self.bounded_extents() else {
                return Ok(0);
            };
            return self.count_between(min_s.saturating_sub(1), max_e.saturating_add(1));
        }

        let floor_utc = self.min_start_floor();
        let mut count = self.undated.len();
        let max_budget = self.options.budget.max_periods;
        for item in self.after(floor_utc) {
            item?;
            count += 1;
            if count > max_budget {
                return Err(RRuleError::BudgetExceeded);
            }
        }
        Ok(count)
    }

    /// Compute sorted, merged opaque busy intervals overlapping `[start_utc, end_utc)`.
    pub fn busy_intervals(
        &self,
        start_utc: i64,
        end_utc: i64,
    ) -> Result<Vec<BusyInterval>, RRuleError> {
        if end_utc <= start_utc {
            return Ok(Vec::new());
        }
        let occs = self.between(start_utc, end_utc)?;
        let mut spans: Vec<BusyInterval> = Vec::with_capacity(occs.len());
        for occ in occs {
            if occ.is_undated || occ.kind == ComponentKind::VFreeBusy {
                continue;
            }
            if let Some(comp) = self.cal.component(occ.component_id) {
                if comp
                    .property_text(&PropertyName::Transp)
                    .is_some_and(|t| t.trim().eq_ignore_ascii_case("TRANSPARENT"))
                {
                    continue;
                }
                if comp
                    .status()
                    .is_some_and(|s| s.trim().eq_ignore_ascii_case("CANCELLED"))
                {
                    continue;
                }
            }
            let s = occ.start_utc.max(start_utc);
            let e = occ.end_utc.min(end_utc);
            if e > s {
                spans.push(BusyInterval {
                    start_utc: s,
                    end_utc: e,
                });
            }
        }

        if self.options.components.contains(&ComponentKind::VFreeBusy) {
            self.collect_busy_freebusy_spans(start_utc, end_utc, &mut spans);
        }

        Ok(merge_intervals(spans))
    }

    /// Compute sorted non-busy (free) intervals inside `[start_utc, end_utc)`.
    pub fn free_intervals(
        &self,
        start_utc: i64,
        end_utc: i64,
    ) -> Result<Vec<BusyInterval>, RRuleError> {
        if end_utc <= start_utc {
            return Ok(Vec::new());
        }
        let busy = self.busy_intervals(start_utc, end_utc)?;
        let mut free = Vec::with_capacity(busy.len() + 1);
        let mut cursor = start_utc;
        for b in busy {
            if b.start_utc > cursor {
                free.push(BusyInterval {
                    start_utc: cursor,
                    end_utc: b.start_utc,
                });
            }
            cursor = cursor.max(b.end_utc);
        }
        if cursor < end_utc {
            free.push(BusyInterval {
                start_utc: cursor,
                end_utc,
            });
        }
        Ok(free)
    }

    pub fn min_start_floor(&self) -> i64 {
        let mut floor = i64::MAX;
        if let Some(s) = self.singles.first() {
            floor = floor.min(s.start_utc);
        }
        for ser in &self.series {
            floor = floor.min(ser.min_start_utc);
        }
        if self.want_alarms {
            if let Some(s) = self.alarm_parent_singles.first() {
                floor = floor.min(s.start_utc.saturating_sub(self.max_alarm_pad_secs));
            }
            for ser in &self.alarm_parent_series {
                floor = floor.min(ser.min_start_utc.saturating_sub(self.max_alarm_pad_secs));
            }
        }
        if floor == i64::MAX {
            0
        } else {
            floor.saturating_sub(86_400)
        }
    }

    pub fn bounded_extents(&self) -> Option<(i64, i64)> {
        let mut min_s = i64::MAX;
        let mut max_e = i64::MIN;
        let mut any = false;
        for s in &self.singles {
            any = true;
            min_s = min_s.min(s.start_utc);
            max_e = max_e.max(s.end_utc.max(s.start_utc));
        }
        for ser in &self.series {
            any = true;
            min_s = min_s.min(ser.min_start_utc);
            max_e = max_e.max(ser.max_end_utc?);
        }
        if self.want_alarms {
            for s in &self.alarm_parent_singles {
                any = true;
                min_s = min_s.min(s.start_utc.saturating_sub(self.max_alarm_pad_secs));
                max_e = max_e.max(s.end_utc.saturating_add(self.max_alarm_pad_secs));
            }
            for ser in &self.alarm_parent_series {
                any = true;
                min_s = min_s.min(ser.min_start_utc.saturating_sub(self.max_alarm_pad_secs));
                max_e = max_e.max(ser.max_end_utc?.saturating_add(self.max_alarm_pad_secs));
            }
        }
        if any {
            Some((min_s, max_e))
        } else {
            None
        }
    }

    fn collect_busy_freebusy_spans(
        &self,
        start_utc: i64,
        end_utc: i64,
        out: &mut Vec<BusyInterval>,
    ) {
        use icaloxide_core::datetime::CalPeriod;
        use icaloxide_core::model::ParamName;

        for fb in self.cal.freebusies() {
            for prop in fb.properties_named(&PropertyName::FreeBusy) {
                let is_free = prop
                    .param_first(&ParamName::FbType)
                    .is_some_and(|v| v.trim().eq_ignore_ascii_case("FREE"));
                if is_free {
                    continue;
                }
                let mut push_period = |p: &CalPeriod| {
                    let (ps, pe) = match p {
                        CalPeriod::Explicit { start, end } => {
                            let (s, _) =
                                resolve_datetime_with_fallback(start, &self.reg, &self.default_tz);
                            let (e, _) =
                                resolve_datetime_with_fallback(end, &self.reg, &self.default_tz);
                            (s, e)
                        }
                        CalPeriod::Duration { start, duration } => {
                            let (s, tz) =
                                resolve_datetime_with_fallback(start, &self.reg, &self.default_tz);
                            let span = span_from_duration(*duration, false);
                            let (e, _) = compute_end_local(start, s, span, &tz);
                            (s, e)
                        }
                    };
                    let cs = ps.max(start_utc);
                    let ce = pe.min(end_utc);
                    if ce > cs {
                        out.push(BusyInterval {
                            start_utc: cs,
                            end_utc: ce,
                        });
                    }
                };
                match &prop.value {
                    PropertyValue::Period(p) => push_period(p),
                    PropertyValue::PeriodList(list) => {
                        for p in list {
                            push_period(p);
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    /// Count occurrences intersecting `[start_utc, end_utc)`.
    pub fn count_between(&self, start_utc: i64, end_utc: i64) -> Result<usize, RRuleError> {
        if end_utc < start_utc {
            return Ok(0);
        }
        let mut count = self.undated.len();
        let upper = if end_utc == start_utc {
            self.singles.partition_point(|s| s.start_utc <= end_utc)
        } else {
            self.singles.partition_point(|s| s.start_utc < end_utc)
        };
        for idx in (0..upper).rev() {
            if self.singles_prefix_max_end[idx] < start_utc {
                break;
            }
            let s = &self.singles[idx];
            if overlaps_window(s.start_utc, s.end_utc, start_utc, end_utc) {
                count += 1;
            }
        }

        let mut buf = Vec::new();
        for ser in &self.series {
            buf.clear();
            match ser.expand_window(
                start_utc,
                end_utc,
                self.options.skip_cancelled,
                self.options.budget,
                &mut buf,
            ) {
                Ok(()) => count += buf.len(),
                Err(err) if self.options.skip_bad_series => {
                    let _ = err;
                }
                Err(err) => return Err(err),
            }
        }
        if self.want_alarms {
            buf.clear();
            self.collect_alarms_in_window(start_utc, end_utc, &mut buf)?;
            count += buf.len();
        }
        Ok(count)
    }

    /// Return a streaming lazy iterator of occurrences ending after `earliest_end_utc`
    /// in chronological order.
    pub fn after(self: &Arc<Self>, earliest_end_utc: i64) -> AfterIter {
        AfterIter::new(Arc::clone(self), earliest_end_utc)
    }

    fn collect_singles_in_window(&self, start_utc: i64, end_utc: i64, out: &mut Vec<Occurrence>) {
        let upper = if end_utc == start_utc {
            self.singles.partition_point(|s| s.start_utc <= end_utc)
        } else {
            self.singles.partition_point(|s| s.start_utc < end_utc)
        };
        for idx in (0..upper).rev() {
            if self.singles_prefix_max_end[idx] < start_utc {
                break;
            }
            let s = &self.singles[idx];
            if overlaps_window(s.start_utc, s.end_utc, start_utc, end_utc) {
                out.push(s.clone());
            }
        }
    }

    fn collect_alarms_in_window(
        &self,
        start_utc: i64,
        end_utc: i64,
        out: &mut Vec<Occurrence>,
    ) -> Result<(), RRuleError> {
        let p_start = start_utc.saturating_sub(self.max_alarm_pad_secs);
        let p_end = end_utc.saturating_add(self.max_alarm_pad_secs);

        let mut parent_occs = Vec::new();
        for p in &self.alarm_parent_singles {
            if overlaps_window(p.start_utc, p.end_utc, p_start, p_end) {
                parent_occs.push(p.clone());
            }
        }
        for ser in &self.alarm_parent_series {
            let mut buf = Vec::new();
            match ser.expand_window(
                p_start,
                p_end,
                self.options.skip_cancelled,
                self.options.budget,
                &mut buf,
            ) {
                Ok(()) => parent_occs.extend(buf),
                Err(err) if self.options.skip_bad_series => {
                    let _ = err;
                }
                Err(err) => return Err(err),
            }
        }

        for p_occ in parent_occs {
            if let Some(p_comp) = self.cal.component(p_occ.component_id) {
                for alarm_occ in
                    expand_alarms_for_parent(p_comp, &p_occ, &self.reg, &self.default_tz)
                {
                    if overlaps_window(alarm_occ.start_utc, alarm_occ.end_utc, start_utc, end_utc) {
                        out.push(alarm_occ);
                    }
                }
            }
        }
        Ok(())
    }
}

/// Streaming chronological occurrence iterator using $O(S)$ memory across $S$ recurring series.
pub struct AfterIter {
    query: Arc<CalendarQuery>,
    earliest_end_utc: i64,
    singles_idx: usize,
    series_streams: Vec<OwnedSeriesStream>,
    alarm_parent_streams: Vec<OwnedSeriesStream>,
    alarm_parent_singles_idx: usize,
    heap: BinaryHeap<Reverse<HeapEntry>>,
    initialized: bool,
}

#[derive(Debug, Clone)]
struct HeapEntry {
    occ: Occurrence,
    source: HeapSource,
    is_master: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HeapSource {
    Single,
    Series(usize),
    AlarmFromParentSeries(usize),
    AlarmFromParentSingle,
}

impl PartialEq for HeapEntry {
    fn eq(&self, other: &Self) -> bool {
        self.occ.start_utc == other.occ.start_utc
            && self.occ.end_utc == other.occ.end_utc
            && self.occ.component_id.0 == other.occ.component_id.0
    }
}
impl Eq for HeapEntry {}
impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for HeapEntry {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        (
            self.occ.start_utc,
            self.occ.end_utc,
            self.occ.component_id.0,
        )
            .cmp(&(
                other.occ.start_utc,
                other.occ.end_utc,
                other.occ.component_id.0,
            ))
    }
}

struct OwnedSeriesStream {
    series_idx: usize,
    is_alarm_parent: bool,
    rule_cursors: Vec<OwnedRuleCursor>,
    split_cursors: Vec<OwnedSplitCursor>,
    static_items: BinaryHeap<Reverse<HeapEntry>>,
    seen_instants: rustc_hash::FxHashSet<(i64, CalDate, CalTime)>,
}

struct OwnedRuleCursor {
    rule_idx: usize,
    next_period_idx: u64,
    periods_checked: usize,
    consecutive_empty: usize,
    emitted: u32,
    pending: std::collections::VecDeque<LocalDateTime>,
    iter_finished: bool,
    finished: bool,
    until_utc: Option<i64>,
    next_occ: Option<Occurrence>,
}

struct OwnedSplitCursor {
    tf_idx: usize,
    rule_idx: usize,
    next_period_idx: u64,
    periods_checked: usize,
    consecutive_empty: usize,
    emitted: u32,
    pending: std::collections::VecDeque<LocalDateTime>,
    iter_finished: bool,
    finished: bool,
    until_utc: Option<i64>,
    next_split_utc: Option<i64>,
    next_occ: Option<Occurrence>,
}

impl AfterIter {
    fn new(query: Arc<CalendarQuery>, earliest_end_utc: i64) -> Self {
        Self {
            query,
            earliest_end_utc,
            singles_idx: 0,
            series_streams: Vec::new(),
            alarm_parent_streams: Vec::new(),
            alarm_parent_singles_idx: 0,
            heap: BinaryHeap::new(),
            initialized: false,
        }
    }

    fn init(&mut self) -> Result<(), RRuleError> {
        self.initialized = true;
        self.push_next_single();

        for s_idx in 0..self.query.series.len() {
            let mut stream = build_owned_stream(
                &self.query.series[s_idx],
                s_idx,
                false,
                self.earliest_end_utc,
                self.query.options.skip_cancelled,
            );
            match self.pop_from_owned_stream(&mut stream, self.earliest_end_utc) {
                Ok(Some(first)) => self.heap.push(Reverse(HeapEntry {
                    occ: first,
                    source: HeapSource::Series(s_idx),
                    is_master: true,
                })),
                Ok(None) => {}
                Err(e) if self.query.options.skip_bad_series => {
                    let _ = e;
                }
                Err(e) => return Err(e),
            }
            self.series_streams.push(stream);
        }

        if self.query.want_alarms {
            let parent_anchor = self
                .earliest_end_utc
                .saturating_sub(self.query.max_alarm_pad_secs);
            self.push_alarms_from_next_parent_single();
            for p_idx in 0..self.query.alarm_parent_series.len() {
                let mut stream = build_owned_stream(
                    &self.query.alarm_parent_series[p_idx],
                    p_idx,
                    true,
                    parent_anchor,
                    self.query.options.skip_cancelled,
                );
                let pushed = self.pump_alarm_parent_stream(&mut stream, p_idx)?;
                let _ = pushed;
                self.alarm_parent_streams.push(stream);
            }
        }
        Ok(())
    }

    fn push_next_single(&mut self) {
        while self.singles_idx < self.query.singles.len() {
            let occ = &self.query.singles[self.singles_idx];
            self.singles_idx += 1;
            if keeps_for_after(occ.start_utc, occ.end_utc, self.earliest_end_utc) {
                self.heap.push(Reverse(HeapEntry {
                    occ: occ.clone(),
                    source: HeapSource::Single,
                    is_master: false,
                }));
                break;
            }
        }
    }

    fn push_alarms_from_next_parent_single(&mut self) {
        let parent_anchor = self
            .earliest_end_utc
            .saturating_sub(self.query.max_alarm_pad_secs);
        while self.alarm_parent_singles_idx < self.query.alarm_parent_singles.len() {
            let p_occ = &self.query.alarm_parent_singles[self.alarm_parent_singles_idx];
            self.alarm_parent_singles_idx += 1;
            if !keeps_for_after(p_occ.start_utc, p_occ.end_utc, parent_anchor) {
                continue;
            }
            if let Some(p_comp) = self.query.cal.component(p_occ.component_id) {
                let mut added_any = false;
                for alarm in
                    expand_alarms_for_parent(p_comp, p_occ, &self.query.reg, &self.query.default_tz)
                {
                    if alarm.start_utc >= self.earliest_end_utc {
                        self.heap.push(Reverse(HeapEntry {
                            occ: alarm,
                            source: HeapSource::AlarmFromParentSingle,
                            is_master: false,
                        }));
                        added_any = true;
                    }
                }
                if added_any {
                    break;
                }
            }
        }
    }

    fn pump_alarm_parent_stream(
        &mut self,
        stream: &mut OwnedSeriesStream,
        p_idx: usize,
    ) -> Result<bool, RRuleError> {
        let parent_anchor = self
            .earliest_end_utc
            .saturating_sub(self.query.max_alarm_pad_secs);
        loop {
            let next_parent = match self.pop_from_owned_stream(stream, parent_anchor) {
                Ok(v) => v,
                Err(e) if self.query.options.skip_bad_series => {
                    let _ = e;
                    return Ok(false);
                }
                Err(e) => return Err(e),
            };
            let Some(p_occ) = next_parent else {
                return Ok(false);
            };
            if let Some(p_comp) = self.query.cal.component(p_occ.component_id) {
                let mut added_any = false;
                for alarm in expand_alarms_for_parent(
                    p_comp,
                    &p_occ,
                    &self.query.reg,
                    &self.query.default_tz,
                ) {
                    if alarm.start_utc >= self.earliest_end_utc {
                        self.heap.push(Reverse(HeapEntry {
                            occ: alarm,
                            source: HeapSource::AlarmFromParentSeries(p_idx),
                            is_master: false,
                        }));
                        added_any = true;
                    }
                }
                if added_any {
                    return Ok(true);
                }
            }
        }
    }

    fn pop_from_owned_stream(
        &self,
        stream: &mut OwnedSeriesStream,
        earliest_end: i64,
    ) -> Result<Option<Occurrence>, RRuleError> {
        let ser = if stream.is_alarm_parent {
            &self.query.alarm_parent_series[stream.series_idx]
        } else {
            &self.query.series[stream.series_idx]
        };

        loop {
            if let Some(master) = &ser.master {
                if !(self.query.options.skip_cancelled && master.cancelled) {
                    for cursor in &mut stream.rule_cursors {
                        if cursor.next_occ.is_none() && !cursor.finished {
                            cursor.next_occ = advance_owned_cursor(
                                cursor,
                                master,
                                &ser.this_and_future,
                                ser.rrule_split_cutoff,
                                self.query.options.skip_cancelled,
                                earliest_end,
                                self.query.options.budget,
                            )?;
                        }
                    }
                }
            }

            for sc in &mut stream.split_cursors {
                if sc.next_occ.is_none() && !sc.finished {
                    let tf = &ser.this_and_future[sc.tf_idx];
                    sc.next_occ = advance_split_cursor(
                        sc,
                        tf,
                        &ser.overrides,
                        ser.master.as_ref().map(|m| &m.exdates),
                        earliest_end,
                        self.query.options.budget,
                    )?;
                }
            }

            let best_rule = stream
                .rule_cursors
                .iter()
                .enumerate()
                .filter_map(|(i, c)| c.next_occ.as_ref().map(|o| (i, o.start_utc, o.end_utc)))
                .min_by_key(|&(_, s, e)| (s, e));
            let best_split = stream
                .split_cursors
                .iter()
                .enumerate()
                .filter_map(|(i, c)| c.next_occ.as_ref().map(|o| (i, o.start_utc, o.end_utc)))
                .min_by_key(|&(_, s, e)| (s, e));
            let best_static = stream
                .static_items
                .peek()
                .map(|r| (r.0.occ.start_utc, r.0.occ.end_utc));

            let (occ, from_master) =
                pick_earliest_stream_item(stream, best_rule, best_split, best_static);

            let Some(occ) = occ else {
                return Ok(None);
            };
            if from_master
                && !stream.seen_instants.insert((
                    occ.start_utc,
                    occ.start_local.date,
                    occ.start_local.time,
                ))
            {
                continue;
            }
            return Ok(Some(occ));
        }
    }
}

fn pick_earliest_stream_item(
    stream: &mut OwnedSeriesStream,
    best_rule: Option<(usize, i64, i64)>,
    best_split: Option<(usize, i64, i64)>,
    best_static: Option<(i64, i64)>,
) -> (Option<Occurrence>, bool) {
    let mut best_kind: Option<u8> = None; // 0=rule, 1=split, 2=static
    let mut best_key = (i64::MAX, i64::MAX);

    if let Some((_, s, e)) = best_rule {
        best_kind = Some(0);
        best_key = (s, e);
    }
    if let Some((_, s, e)) = best_split {
        if (s, e) < best_key {
            best_kind = Some(1);
            best_key = (s, e);
        }
    }
    if let Some((s, e)) = best_static {
        if (s, e) < best_key {
            best_kind = Some(2);
        }
    }

    match best_kind {
        Some(0) => {
            let i = best_rule.unwrap().0;
            (stream.rule_cursors[i].next_occ.take(), true)
        }
        Some(1) => {
            let i = best_split.unwrap().0;
            (stream.split_cursors[i].next_occ.take(), true)
        }
        Some(2) => {
            let item = stream.static_items.pop().unwrap().0;
            (Some(item.occ), item.is_master)
        }
        _ => (None, false),
    }
}

impl Iterator for AfterIter {
    type Item = Result<Occurrence, RRuleError>;

    fn next(&mut self) -> Option<Self::Item> {
        if !self.initialized {
            if let Err(e) = self.init() {
                return Some(Err(e));
            }
        }
        let Reverse(entry) = self.heap.pop()?;
        match entry.source {
            HeapSource::Single => self.push_next_single(),
            HeapSource::Series(s_idx) => {
                let mut stream =
                    std::mem::replace(&mut self.series_streams[s_idx], empty_stream(s_idx, false));
                let next_res = self.pop_from_owned_stream(&mut stream, self.earliest_end_utc);
                self.series_streams[s_idx] = stream;
                match next_res {
                    Ok(Some(occ)) => self.heap.push(Reverse(HeapEntry {
                        occ,
                        source: HeapSource::Series(s_idx),
                        is_master: true,
                    })),
                    Ok(None) => {}
                    Err(e) if self.query.options.skip_bad_series => {
                        let _ = e;
                    }
                    Err(e) => return Some(Err(e)),
                }
            }
            HeapSource::AlarmFromParentSingle => self.push_alarms_from_next_parent_single(),
            HeapSource::AlarmFromParentSeries(p_idx) => {
                let mut stream = std::mem::replace(
                    &mut self.alarm_parent_streams[p_idx],
                    empty_stream(p_idx, true),
                );
                let res = self.pump_alarm_parent_stream(&mut stream, p_idx);
                self.alarm_parent_streams[p_idx] = stream;
                if let Err(e) = res {
                    return Some(Err(e));
                }
            }
        }
        Some(Ok(entry.occ))
    }
}

fn empty_stream(series_idx: usize, is_alarm_parent: bool) -> OwnedSeriesStream {
    OwnedSeriesStream {
        series_idx,
        is_alarm_parent,
        rule_cursors: Vec::new(),
        split_cursors: Vec::new(),
        static_items: BinaryHeap::new(),
        seen_instants: rustc_hash::FxHashSet::default(),
    }
}

fn build_owned_stream(
    ser: &CompiledSeries,
    series_idx: usize,
    is_alarm_parent: bool,
    earliest_end_utc: i64,
    skip_cancelled: bool,
) -> OwnedSeriesStream {
    let mut static_items = BinaryHeap::new();
    let source = if is_alarm_parent {
        HeapSource::AlarmFromParentSeries(series_idx)
    } else {
        HeapSource::Series(series_idx)
    };

    for ov in &ser.overrides {
        if skip_cancelled && ov.cancelled {
            continue;
        }
        if keeps_for_after(ov.start_utc, ov.end_utc, earliest_end_utc) {
            static_items.push(Reverse(HeapEntry {
                occ: Occurrence {
                    component_id: ov.component_id,
                    kind: ov.kind.clone(),
                    start_utc: ov.start_utc,
                    end_utc: ov.end_utc,
                    start_local: ov.start_local.clone(),
                    end_local: ov.end_local.clone(),
                    is_all_day: ov.is_all_day,
                    is_undated: false,
                    recurrence_id_utc: Some(ov.recurrence_id_utc),
                    parent_id: None,
                },
                source,
                is_master: false,
            }));
        }
    }

    let mut rule_cursors = Vec::new();
    if let Some(master) = &ser.master {
        if !(skip_cancelled && master.cancelled) {
            push_master_statics(
                master,
                source,
                &ser.this_and_future,
                ser.rrule_split_cutoff,
                skip_cancelled,
                earliest_end_utc,
                &mut static_items,
            );
            let master_is_floating = matches!(
                master.start_local.tz,
                icaloxide_core::datetime::TimeZoneRef::Floating
            );
            for (r_idx, rule) in master.rrules.iter().enumerate() {
                let until_utc =
                    resolve_until_utc(rule, &master.tz, master.is_all_day, master_is_floating);
                rule_cursors.push(OwnedRuleCursor {
                    rule_idx: r_idx,
                    next_period_idx: 0,
                    periods_checked: 0,
                    consecutive_empty: 0,
                    emitted: 0,
                    pending: std::collections::VecDeque::new(),
                    iter_finished: false,
                    finished: false,
                    until_utc,
                    next_occ: None,
                });
            }
        }
    }

    let mut split_cursors = Vec::new();
    push_split_cursors(
        ser,
        source,
        earliest_end_utc,
        skip_cancelled,
        &mut static_items,
        &mut split_cursors,
    );

    OwnedSeriesStream {
        series_idx,
        is_alarm_parent,
        rule_cursors,
        split_cursors,
        static_items,
        seen_instants: rustc_hash::FxHashSet::default(),
    }
}

fn push_split_cursors(
    ser: &CompiledSeries,
    source: HeapSource,
    earliest_end_utc: i64,
    skip_cancelled: bool,
    static_items: &mut BinaryHeap<Reverse<HeapEntry>>,
    split_cursors: &mut Vec<OwnedSplitCursor>,
) {
    for (tf_idx, tf) in ser.this_and_future.iter().enumerate() {
        if skip_cancelled && tf.cancelled {
            continue;
        }
        if tf.override_rrules.is_empty() {
            if ser.master.is_none() && keeps_for_after(tf.start_utc, tf.end_utc, earliest_end_utc) {
                static_items.push(Reverse(HeapEntry {
                    occ: Occurrence {
                        component_id: tf.component_id,
                        kind: tf.kind.clone(),
                        start_utc: tf.start_utc,
                        end_utc: tf.end_utc,
                        start_local: tf.start_local.clone(),
                        end_local: tf.end_local.clone(),
                        is_all_day: tf.is_all_day,
                        is_undated: false,
                        recurrence_id_utc: Some(tf.recurrence_id_utc),
                        parent_id: None,
                    },
                    source,
                    is_master: false,
                }));
            }
            continue;
        }
        let next_split_utc = ser
            .this_and_future
            .get(tf_idx + 1)
            .map(|n| n.recurrence_id_utc);
        let tf_is_floating = matches!(
            tf.start_local.tz,
            icaloxide_core::datetime::TimeZoneRef::Floating
        );
        for (rule_idx, rule) in tf.override_rrules.iter().enumerate() {
            split_cursors.push(OwnedSplitCursor {
                tf_idx,
                rule_idx,
                next_period_idx: 0,
                periods_checked: 0,
                consecutive_empty: 0,
                emitted: 0,
                pending: std::collections::VecDeque::new(),
                iter_finished: false,
                finished: false,
                until_utc: resolve_until_utc(rule, &tf.tz, tf.is_all_day, tf_is_floating),
                next_split_utc,
                next_occ: None,
            });
        }
    }
}

fn push_master_statics(
    master: &crate::recurrence::series::MasterSeries,
    source: HeapSource,
    this_and_future: &[OverrideEntry],
    rrule_split_cutoff: Option<i64>,
    skip_cancelled: bool,
    earliest_end_utc: i64,
    static_items: &mut BinaryHeap<Reverse<HeapEntry>>,
) {
    let mut try_push = |dt: &CalDateTime, cand_tz: &CalendarTimeZone, span: SpanDuration| {
        let s_utc = resolve_local_instant(dt, cand_tz);
        if rrule_split_cutoff.is_some_and(|cut| s_utc >= cut) {
            return;
        }
        if master.exdates.matches(s_utc, dt.date, dt.time) {
            return;
        }
        if let Some(occ) = apply_this_and_future_shift(
            master,
            this_and_future,
            s_utc,
            dt,
            cand_tz,
            span,
            skip_cancelled,
        ) {
            if keeps_for_after(occ.start_utc, occ.end_utc, earliest_end_utc) {
                static_items.push(Reverse(HeapEntry {
                    occ,
                    source,
                    is_master: true,
                }));
            }
        }
    };

    if should_emit_master_dtstart(master) {
        try_push(&master.start_local, &master.tz, master.span);
    }
    for rdt in &master.rdates {
        try_push(
            &rdt.start_local,
            &rdt.tz,
            rdt.custom_span.unwrap_or(master.span),
        );
    }
}

fn advance_owned_cursor(
    cursor: &mut OwnedRuleCursor,
    master: &crate::recurrence::series::MasterSeries,
    this_and_future: &[OverrideEntry],
    rrule_split_cutoff: Option<i64>,
    skip_cancelled: bool,
    earliest_end_utc: i64,
    budget: IterationBudget,
) -> Result<Option<Occurrence>, RRuleError> {
    let rule = &master.rrules[cursor.rule_idx];
    let dtstart_ldt = LocalDateTime::new(master.start_local.date, master.start_local.time);
    let max_span_secs = master.span.max_seconds();
    let max_back_shift = this_and_future
        .iter()
        .map(|tf| (-tf.time_shift_secs).max(0))
        .max()
        .unwrap_or(0);
    let ff_utc = earliest_end_utc
        .saturating_sub(max_span_secs + max_back_shift)
        .max(master.min_start_utc);
    let ff_local = utc_to_local_approx(ff_utc, &master.tz);

    let mut iter = RRuleIter::new(
        rule,
        dtstart_ldt,
        cursor.until_utc.map(|u| {
            let off = master.tz.offset_at_utc(u).unwrap_or(0);
            LocalDateTime::from_naive_timestamp(u + 86_400 + i64::from(off))
        }),
        if cursor.periods_checked == 0
            && cursor.emitted == 0
            && cursor.pending.is_empty()
            && !cursor.iter_finished
        {
            Some(ff_local)
        } else {
            None
        },
        budget,
    );

    if cursor.periods_checked > 0
        || cursor.emitted > 0
        || !cursor.pending.is_empty()
        || cursor.iter_finished
    {
        iter.restore_state(
            cursor.next_period_idx,
            cursor.periods_checked,
            cursor.consecutive_empty,
            cursor.emitted,
            std::mem::take(&mut cursor.pending),
            cursor.iter_finished,
        );
    }

    let mut found = None;
    for item in &mut iter {
        let ldt = item?;
        let cand_local = CalDateTime {
            date: ldt.date,
            time: ldt.time,
            tz: master.start_local.tz.clone(),
        };
        let s_utc = resolve_local_instant(&cand_local, &master.tz);
        if cursor.until_utc.is_some_and(|u| s_utc > u)
            || rrule_split_cutoff.is_some_and(|cut| s_utc >= cut)
        {
            cursor.finished = true;
            break;
        }
        if master.exdates.matches(s_utc, ldt.date, ldt.time) {
            continue;
        }
        let Some(occ) = apply_this_and_future_shift(
            master,
            this_and_future,
            s_utc,
            &cand_local,
            &master.tz,
            master.span,
            skip_cancelled,
        ) else {
            continue;
        };
        if !keeps_for_after(occ.start_utc, occ.end_utc, earliest_end_utc) {
            continue;
        }
        found = Some(occ);
        break;
    }

    let (p_idx, p_chk, c_empty, emitted, pending, fin) = iter.save_state();
    cursor.next_period_idx = p_idx;
    cursor.periods_checked = p_chk;
    cursor.consecutive_empty = c_empty;
    cursor.emitted = emitted;
    cursor.pending = pending;
    cursor.iter_finished = fin;
    if (fin && cursor.pending.is_empty()) || found.is_none() {
        cursor.finished = true;
    }
    Ok(found)
}

fn advance_split_cursor(
    cursor: &mut OwnedSplitCursor,
    tf: &OverrideEntry,
    single_overrides: &[OverrideEntry],
    master_exdates: Option<&crate::recurrence::ExclusionSet>,
    earliest_end_utc: i64,
    budget: IterationBudget,
) -> Result<Option<Occurrence>, RRuleError> {
    let rule = &tf.override_rrules[cursor.rule_idx];
    let dtstart_ldt = LocalDateTime::new(tf.start_local.date, tf.start_local.time);
    let max_span_secs = tf.span.max_seconds();
    let ff_utc = earliest_end_utc
        .saturating_sub(max_span_secs)
        .max(tf.start_utc);
    let ff_local = utc_to_local_approx(ff_utc, &tf.tz);

    let mut iter = RRuleIter::new(
        rule,
        dtstart_ldt,
        cursor.until_utc.map(|u| {
            let off = tf.tz.offset_at_utc(u).unwrap_or(0);
            LocalDateTime::from_naive_timestamp(u + 86_400 + i64::from(off))
        }),
        if cursor.periods_checked == 0
            && cursor.emitted == 0
            && cursor.pending.is_empty()
            && !cursor.iter_finished
        {
            Some(ff_local)
        } else {
            None
        },
        budget,
    );

    if cursor.periods_checked > 0
        || cursor.emitted > 0
        || !cursor.pending.is_empty()
        || cursor.iter_finished
    {
        iter.restore_state(
            cursor.next_period_idx,
            cursor.periods_checked,
            cursor.consecutive_empty,
            cursor.emitted,
            std::mem::take(&mut cursor.pending),
            cursor.iter_finished,
        );
    }

    let mut found = None;
    for item in &mut iter {
        let ldt = item?;
        let cand_local = CalDateTime {
            date: ldt.date,
            time: ldt.time,
            tz: tf.start_local.tz.clone(),
        };
        let s_utc = resolve_local_instant(&cand_local, &tf.tz);
        if cursor.until_utc.is_some_and(|u| s_utc > u)
            || cursor.next_split_utc.is_some_and(|n| s_utc >= n)
        {
            cursor.finished = true;
            break;
        }
        if master_exdates.is_some_and(|ex| ex.matches(s_utc, ldt.date, ldt.time))
            || single_overrides
                .iter()
                .any(|o| o.recurrence_id_utc == s_utc)
        {
            continue;
        }
        let (e_utc, e_local) =
            compute_end_local_with_tz(&cand_local, s_utc, tf.span, &tf.tz, tf.end_tz.as_ref());
        if !keeps_for_after(s_utc, e_utc, earliest_end_utc) {
            continue;
        }
        found = Some(Occurrence {
            component_id: tf.component_id,
            kind: tf.kind.clone(),
            start_utc: s_utc,
            end_utc: e_utc,
            start_local: cand_local,
            end_local: e_local,
            is_all_day: tf.is_all_day,
            is_undated: false,
            recurrence_id_utc: Some(s_utc),
            parent_id: None,
        });
        break;
    }

    let (p_idx, p_chk, c_empty, emitted, pending, fin) = iter.save_state();
    cursor.next_period_idx = p_idx;
    cursor.periods_checked = p_chk;
    cursor.consecutive_empty = c_empty;
    cursor.emitted = emitted;
    cursor.pending = pending;
    cursor.iter_finished = fin;
    if (fin && cursor.pending.is_empty()) || found.is_none() {
        cursor.finished = true;
    }
    Ok(found)
}

#[inline]
const fn keeps_for_after(start_utc: i64, end_utc: i64, earliest_end_utc: i64) -> bool {
    if end_utc > start_utc {
        end_utc > earliest_end_utc
    } else {
        start_utc >= earliest_end_utc
    }
}

fn collect_abs_alarms(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
    skip_cancelled: bool,
    out: &mut Vec<Occurrence>,
) {
    let cancelled = skip_cancelled
        && comp
            .status()
            .is_some_and(|s| s.trim().eq_ignore_ascii_case("CANCELLED"));
    if !cancelled {
        out.extend(extract_absolute_alarms(comp, reg, default_tz));
    }
}

fn split_master_and_overrides<'a>(
    comps: &[ComponentRef<'a>],
) -> (Option<ComponentRef<'a>>, Vec<ComponentRef<'a>>) {
    let mut best_master: Option<(i64, ComponentRef<'a>)> = None;
    let mut overrides: Vec<ComponentRef<'a>> = Vec::new();
    for &c in comps {
        if c.property(&PropertyName::RecurrenceId).is_some() {
            overrides.push(c);
        } else {
            let seq = match c.property(&PropertyName::Sequence).map(|p| &p.value) {
                Some(PropertyValue::Integer(n)) => *n,
                _ => 0,
            };
            if best_master
                .as_ref()
                .is_none_or(|(best_seq, _)| seq >= *best_seq)
            {
                best_master = Some((seq, c));
            }
        }
    }
    (best_master.map(|(_, c)| c), overrides)
}

#[allow(clippy::too_many_arguments)]
fn classify_component_group(
    comps: &[ComponentRef<'_>],
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
    options: &QueryOptions,
    is_direct: bool,
    has_alarm: bool,
    singles: &mut Vec<Occurrence>,
    undated: &mut Vec<Occurrence>,
    series: &mut Vec<CompiledSeries>,
    alarm_parent_singles: &mut Vec<Occurrence>,
    alarm_parent_series: &mut Vec<CompiledSeries>,
) -> Result<(), RRuleError> {
    let (master, overrides) = split_master_and_overrides(comps);

    let is_simple_single = overrides.is_empty()
        && master.is_some_and(|m| {
            m.property(&PropertyName::RRule).is_none()
                && m.property(&PropertyName::RDate).is_none()
                && m.property(&PropertyName::ExDate).is_none()
        });

    if is_simple_single {
        let mc = master.unwrap();
        if has_alarm {
            collect_abs_alarms(mc, reg, default_tz, options.skip_cancelled, singles);
        }
        if let Some(timing) = extract_component_timing(mc, reg, default_tz) {
            if options.skip_cancelled && timing.cancelled {
                return Ok(());
            }
            let (end_utc, end_local) = compute_end_local_with_tz(
                &timing.start_local,
                timing.start_utc,
                timing.span,
                &timing.tz,
                timing.end_tz.as_ref(),
            );
            let occ = Occurrence {
                component_id: mc.id(),
                kind: mc.kind().clone(),
                start_utc: timing.start_utc,
                end_utc,
                start_local: timing.start_local,
                end_local,
                is_all_day: timing.is_all_day,
                is_undated: false,
                recurrence_id_utc: None,
                parent_id: None,
            };
            if is_direct {
                singles.push(occ.clone());
            }
            if has_alarm {
                alarm_parent_singles.push(occ);
            }
        } else if options.include_undated && is_direct && mc.kind() == &ComponentKind::VTodo {
            let cancelled = mc
                .status()
                .is_some_and(|s| s.trim().eq_ignore_ascii_case("CANCELLED"));
            if !(options.skip_cancelled && cancelled) {
                let placeholder = CalDateTime {
                    date: CalDate {
                        year: 1970,
                        month: 1,
                        day: 1,
                    },
                    time: CalTime {
                        hour: 0,
                        minute: 0,
                        second: 0,
                    },
                    tz: icaloxide_core::datetime::TimeZoneRef::Floating,
                };
                undated.push(Occurrence {
                    component_id: mc.id(),
                    kind: mc.kind().clone(),
                    start_utc: i64::MIN,
                    end_utc: i64::MAX,
                    start_local: placeholder.clone(),
                    end_local: placeholder,
                    is_all_day: false,
                    is_undated: true,
                    recurrence_id_utc: None,
                    parent_id: None,
                });
            }
        }
        return Ok(());
    }

    if has_alarm {
        if let Some(mc) = master {
            collect_abs_alarms(mc, reg, default_tz, options.skip_cancelled, singles);
        }
        for &ov in &overrides {
            collect_abs_alarms(ov, reg, default_tz, options.skip_cancelled, singles);
        }
    }

    if let Some(compiled) = CompiledSeries::build(master, &overrides, reg, default_tz)? {
        if is_direct && has_alarm {
            alarm_parent_series.push(compiled.clone());
            series.push(compiled);
        } else if is_direct {
            series.push(compiled);
        } else if has_alarm {
            alarm_parent_series.push(compiled);
        }
    }
    Ok(())
}

fn has_valarm_child(comp: ComponentRef<'_>) -> bool {
    comp.children().any(|c| c.kind() == &ComponentKind::VAlarm)
}

fn scan_max_alarm_pad(comp: ComponentRef<'_>) -> i64 {
    let mut max_pad: i64 = 0;
    for child in comp.children() {
        if child.kind() != &ComponentKind::VAlarm {
            continue;
        }
        if let Some(PropertyValue::Duration(d)) =
            child.property(&PropertyName::Trigger).map(|p| &p.value)
        {
            max_pad = max_pad.max(d.total_seconds().abs() + 86_400);
        }
    }
    max_pad
}

type ComponentGrouping<'a> = (
    FxHashMap<(ComponentKind, String), Vec<ComponentRef<'a>>>,
    Vec<ComponentRef<'a>>,
);

fn push_partitioned<'a>(
    comp: ComponentRef<'a>,
    want_alarms: bool,
    has_alarm: bool,
    max_alarm_pad_secs: &mut i64,
    grouped: &mut FxHashMap<(ComponentKind, String), Vec<ComponentRef<'a>>>,
    ungrouped: &mut Vec<ComponentRef<'a>>,
) {
    if want_alarms && has_alarm {
        *max_alarm_pad_secs = (*max_alarm_pad_secs).max(scan_max_alarm_pad(comp));
    }
    if let Some(uid) = comp.uid() {
        grouped
            .entry((comp.kind().clone(), uid.to_owned()))
            .or_default()
            .push(comp);
    } else {
        ungrouped.push(comp);
    }
}

fn partition_query_components<'a>(
    cal: &'a Calendar,
    want_non_alarm: &[ComponentKind],
    want_alarms: bool,
    want_availability: bool,
    max_alarm_pad_secs: &mut i64,
) -> ComponentGrouping<'a> {
    let mut grouped: FxHashMap<(ComponentKind, String), Vec<ComponentRef<'a>>> =
        FxHashMap::default();
    let mut ungrouped: Vec<ComponentRef<'a>> = Vec::new();

    for comp in cal.root().children() {
        if matches!(
            comp.kind(),
            ComponentKind::VTimezone | ComponentKind::VFreeBusy
        ) {
            continue;
        }
        if comp.kind() == &ComponentKind::VAvailability {
            if want_availability {
                for child in comp.children() {
                    if child.kind() == &ComponentKind::Available {
                        let child_alarm = has_valarm_child(child);
                        push_partitioned(
                            child,
                            want_alarms,
                            child_alarm,
                            max_alarm_pad_secs,
                            &mut grouped,
                            &mut ungrouped,
                        );
                    }
                }
                if want_non_alarm.contains(&ComponentKind::VAvailability)
                    && comp.property(&PropertyName::DtStart).is_some()
                {
                    let has_alarm = has_valarm_child(comp);
                    push_partitioned(
                        comp,
                        want_alarms,
                        has_alarm,
                        max_alarm_pad_secs,
                        &mut grouped,
                        &mut ungrouped,
                    );
                }
            }
            continue;
        }

        let is_direct = want_non_alarm.contains(comp.kind())
            || (want_availability && comp.kind() == &ComponentKind::Available);
        let has_alarm = want_alarms && has_valarm_child(comp);
        if !is_direct && !has_alarm {
            continue;
        }
        push_partitioned(
            comp,
            want_alarms,
            has_alarm,
            max_alarm_pad_secs,
            &mut grouped,
            &mut ungrouped,
        );
    }

    (grouped, ungrouped)
}

fn merge_intervals(mut spans: Vec<BusyInterval>) -> Vec<BusyInterval> {
    if spans.is_empty() {
        return spans;
    }
    spans.sort_by_key(|s| (s.start_utc, s.end_utc));
    let mut merged: Vec<BusyInterval> = Vec::with_capacity(spans.len());
    for span in spans {
        if let Some(last) = merged.last_mut() {
            if span.start_utc <= last.end_utc {
                last.end_utc = last.end_utc.max(span.end_utc);
                continue;
            }
        }
        merged.push(span);
    }
    merged
}
