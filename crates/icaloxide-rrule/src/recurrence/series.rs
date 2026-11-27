use icaloxide_core::datetime::{CalDate, CalDateTime, CalPeriod, CalTime, TimeZoneRef};
use icaloxide_core::model::{
    ComponentId, ComponentKind, ComponentRef, ParamName, PropertyName, PropertyValue,
};
use icaloxide_core::tz::{CalendarTimeZone, DstDisambiguation, TzRegistry};
use rustc_hash::FxHashSet;

use crate::error::{IterationBudget, RRuleError};
use crate::query::Occurrence;
use crate::rrule::{LocalDateTime, RRule, RRuleIter, UntilValue};

/// Duration of an occurrence preserving all-day calendar semantics across DST.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpanDuration {
    Days(i32),
    Seconds(i64),
}

/// An `RDATE` entry with optional custom duration (`RDATE;VALUE=PERIOD`).
#[derive(Debug, Clone)]
pub struct RDateEntry {
    pub start_local: CalDateTime,
    pub custom_span: Option<SpanDuration>,
}

/// Dual-key exclusion set matching `EXDATE` and `RECURRENCE-ID` across UTC instants,
/// local `DATE` values, and floating wall-clock times.
#[derive(Debug, Clone, Default)]
pub struct ExclusionSet {
    pub utc_instants: FxHashSet<i64>,
    pub local_dates: FxHashSet<CalDate>,
    pub floating_wall: FxHashSet<(CalDate, CalTime)>,
}

impl ExclusionSet {
    pub fn insert_exdate_value(
        &mut self,
        val: &PropertyValue,
        reg: &TzRegistry,
        default_tz: &CalendarTimeZone,
    ) {
        match val {
            PropertyValue::Date(d) => {
                self.local_dates.insert(*d);
            }
            PropertyValue::DateList(list) => {
                for &d in list {
                    self.local_dates.insert(d);
                }
            }
            PropertyValue::DateTime(dt) => self.insert_datetime(dt, reg, default_tz),
            PropertyValue::DateTimeList(list) => {
                for dt in list {
                    self.insert_datetime(dt, reg, default_tz);
                }
            }
            _ => {}
        }
    }

    pub fn insert_datetime(
        &mut self,
        dt: &CalDateTime,
        reg: &TzRegistry,
        default_tz: &CalendarTimeZone,
    ) {
        self.floating_wall.insert((dt.date, dt.time));
        let (utc_ts, _) = resolve_datetime_with_fallback(dt, reg, default_tz);
        self.utc_instants.insert(utc_ts);
    }

    #[inline]
    pub fn matches(&self, utc_ts: i64, local_date: CalDate, local_time: CalTime) -> bool {
        self.utc_instants.contains(&utc_ts)
            || self.local_dates.contains(&local_date)
            || self.floating_wall.contains(&(local_date, local_time))
    }
}

/// Resolve a [`CalDateTime`] to `(utc_timestamp, CalendarTimeZone)`.
pub fn resolve_datetime_with_fallback(
    dt: &CalDateTime,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> (i64, CalendarTimeZone) {
    let tz = match &dt.tz {
        TimeZoneRef::Utc => CalendarTimeZone::Utc,
        TimeZoneRef::TzId(id) => reg
            .resolve(id.as_str())
            .unwrap_or_else(|| default_tz.clone()),
        TimeZoneRef::Floating => default_tz.clone(),
    };
    let utc = match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
        Ok(res) => res.utc_timestamp,
        Err(_) => dt.to_naive_timestamp(),
    };
    (utc, tz)
}

/// Compute local wall-clock end [`CalDateTime`] from `start_local`, `start_utc`, `span`, and `tz`.
pub fn compute_end_local(
    start_local: &CalDateTime,
    start_utc: i64,
    span: SpanDuration,
    tz: &CalendarTimeZone,
) -> (i64, CalDateTime) {
    match span {
        SpanDuration::Days(days) => {
            let end_date = start_local.date.add_days(days);
            let end_dt = CalDateTime {
                date: end_date,
                time: start_local.time,
                tz: start_local.tz.clone(),
            };
            let end_utc =
                match tz.resolve_local(end_date, start_local.time, DstDisambiguation::Compatible) {
                    Ok(res) => res.utc_timestamp,
                    Err(_) => end_dt.to_naive_timestamp(),
                };
            (end_utc, end_dt)
        }
        SpanDuration::Seconds(secs) => {
            let end_utc = start_utc + secs;
            let offset = tz.offset_at_utc(end_utc).unwrap_or(0);
            let local_naive = end_utc + i64::from(offset);
            let ldt = LocalDateTime::from_naive_timestamp(local_naive);
            (
                end_utc,
                CalDateTime {
                    date: ldt.date,
                    time: ldt.time,
                    tz: start_local.tz.clone(),
                },
            )
        }
    }
}

/// Extracted start, duration, and flags for a single component instance.
pub struct ExtractedTiming {
    pub start_local: CalDateTime,
    pub start_utc: i64,
    pub tz: CalendarTimeZone,
    pub span: SpanDuration,
    pub is_all_day: bool,
    pub cancelled: bool,
}

/// Extract timing (`DTSTART`/`DUE`/`DTEND`/`DURATION`) from a component.
pub fn extract_component_timing(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Option<ExtractedTiming> {
    let start_prop = comp.property(&PropertyName::DtStart).or_else(|| {
        if comp.kind() == &ComponentKind::VTodo {
            comp.property(&PropertyName::Due)
        } else {
            None
        }
    })?;

    let (start_local, is_all_day) = match &start_prop.value {
        PropertyValue::Date(d) => (
            CalDateTime {
                date: *d,
                time: CalTime {
                    hour: 0,
                    minute: 0,
                    second: 0,
                },
                tz: TimeZoneRef::Floating,
            },
            true,
        ),
        PropertyValue::DateTime(dt) => (dt.clone(), false),
        _ => return None,
    };

    let (start_utc, tz) = resolve_datetime_with_fallback(&start_local, reg, default_tz);

    let end_prop = comp
        .property(&PropertyName::DtEnd)
        .or_else(|| comp.property(&PropertyName::Due));

    let span = if let Some(ep) = end_prop {
        match &ep.value {
            PropertyValue::Date(end_d) => {
                let days =
                    (end_d.to_days_since_epoch() - start_local.date.to_days_since_epoch()).max(0);
                SpanDuration::Days(if is_all_day && days == 0 { 1 } else { days })
            }
            PropertyValue::DateTime(end_dt) => {
                let (end_utc, _) = resolve_datetime_with_fallback(end_dt, reg, default_tz);
                SpanDuration::Seconds((end_utc - start_utc).max(0))
            }
            _ => default_span(comp, is_all_day),
        }
    } else {
        default_span(comp, is_all_day)
    };

    let cancelled = comp
        .status()
        .is_some_and(|s| s.trim().eq_ignore_ascii_case("CANCELLED"));

    Some(ExtractedTiming {
        start_local,
        start_utc,
        tz,
        span,
        is_all_day,
        cancelled,
    })
}

fn default_span(comp: ComponentRef<'_>, is_all_day: bool) -> SpanDuration {
    if let Some(PropertyValue::Duration(dur)) =
        comp.property(&PropertyName::Duration).map(|p| &p.value)
    {
        let secs = dur.total_seconds().max(0);
        if is_all_day {
            let days = (secs / 86_400).max(1) as i32;
            SpanDuration::Days(days)
        } else {
            SpanDuration::Seconds(secs)
        }
    } else if is_all_day {
        SpanDuration::Days(1)
    } else {
        SpanDuration::Seconds(0)
    }
}

/// An override component carrying `RECURRENCE-ID`.
#[derive(Debug, Clone)]
pub struct OverrideEntry {
    pub component_id: ComponentId,
    pub kind: ComponentKind,
    pub recurrence_id_utc: i64,
    pub recurrence_id_date: CalDate,
    pub is_this_and_future: bool,
    pub start_utc: i64,
    pub end_utc: i64,
    pub start_local: CalDateTime,
    pub end_local: CalDateTime,
    pub span: SpanDuration,
    pub tz: CalendarTimeZone,
    pub time_shift_secs: i64,
    pub override_rrules: Vec<RRule>,
    pub is_all_day: bool,
    pub cancelled: bool,
    pub sequence: i64,
}

/// Master component with `RRULE`s, `RDATE`s, `EXDATE`s, and `RECURRENCE-ID` overrides.
#[derive(Debug, Clone)]
pub struct MasterSeries {
    pub component_id: ComponentId,
    pub kind: ComponentKind,
    pub start_local: CalDateTime,
    pub tz: CalendarTimeZone,
    pub span: SpanDuration,
    pub is_all_day: bool,
    pub cancelled: bool,
    pub rrules: Vec<RRule>,
    pub rdates: Vec<RDateEntry>,
    pub exdates: ExclusionSet,
    pub min_start_utc: i64,
    pub max_end_utc: Option<i64>,
}

/// Compiled recurrence series for one `(ComponentKind, UID)` group.
#[derive(Debug, Clone)]
pub struct CompiledSeries {
    pub master: Option<MasterSeries>,
    /// Single-instance `RECURRENCE-ID` overrides (without `RANGE=THISANDFUTURE`).
    pub overrides: Vec<OverrideEntry>,
    /// Chronologically sorted `RANGE=THISANDFUTURE` overrides.
    pub this_and_future: Vec<OverrideEntry>,
    /// Cutoff UTC timestamp where a `THISANDFUTURE` override carrying its own `RRULE` takes over.
    pub rrule_split_cutoff: Option<i64>,
    pub min_start_utc: i64,
    pub max_end_utc: Option<i64>,
}

impl CompiledSeries {
    /// Build a [`CompiledSeries`] from a master component and/or `RECURRENCE-ID` overrides.
    pub fn build(
        master_comp: Option<ComponentRef<'_>>,
        override_comps: &[ComponentRef<'_>],
        reg: &TzRegistry,
        default_tz: &CalendarTimeZone,
    ) -> Result<Option<Self>, RRuleError> {
        let mut all_overrides = Vec::with_capacity(override_comps.len());
        for &ov in override_comps {
            if let Some(entry) = build_override(ov, reg, default_tz)? {
                all_overrides.push(entry);
            }
        }

        // Deduplicate overrides targeting the same RECURRENCE-ID by highest SEQUENCE (or later).
        all_overrides.sort_by_key(|o| (o.recurrence_id_utc, o.sequence, o.component_id.0));
        all_overrides.dedup_by(|later, earlier| {
            if later.recurrence_id_utc == earlier.recurrence_id_utc {
                *earlier = later.clone();
                true
            } else {
                false
            }
        });

        let mut single_overrides = Vec::new();
        let mut this_and_future = Vec::new();
        let mut override_exclusions = ExclusionSet::default();
        let mut rrule_split_cutoff: Option<i64> = None;

        for ov in all_overrides {
            if ov.is_this_and_future {
                if !ov.override_rrules.is_empty() {
                    rrule_split_cutoff = Some(match rrule_split_cutoff {
                        Some(cur) => cur.min(ov.recurrence_id_utc),
                        None => ov.recurrence_id_utc,
                    });
                }
                this_and_future.push(ov);
            } else {
                override_exclusions
                    .utc_instants
                    .insert(ov.recurrence_id_utc);
                single_overrides.push(ov);
            }
        }

        let master = if let Some(mc) = master_comp {
            build_master(mc, reg, default_tz, override_exclusions)?
        } else {
            None
        };

        if master.is_none() && single_overrides.is_empty() && this_and_future.is_empty() {
            return Ok(None);
        }

        let mut min_start_utc = i64::MAX;
        let mut max_end_utc = Some(i64::MIN);

        if let Some(m) = &master {
            min_start_utc = min_start_utc.min(m.min_start_utc);
            max_end_utc = match (max_end_utc, m.max_end_utc) {
                (Some(a), Some(b)) => Some(a.max(b)),
                _ => None,
            };
        }
        for o in &single_overrides {
            min_start_utc = min_start_utc.min(o.start_utc);
            if let Some(cur_max) = max_end_utc {
                max_end_utc = Some(cur_max.max(o.end_utc));
            }
        }
        for tf in &this_and_future {
            min_start_utc = min_start_utc.min(tf.start_utc);
            if !tf.override_rrules.is_empty() {
                // Bounded only if all override RRULEs have UNTIL; conservative unbounded if any lacks UNTIL.
                if tf.override_rrules.iter().any(|r| r.until.is_none()) {
                    max_end_utc = None;
                }
            } else if let Some(cur_max) = max_end_utc {
                let shifted_max = cur_max + tf.time_shift_secs.max(0);
                max_end_utc = Some(shifted_max.max(tf.end_utc));
            }
        }

        Ok(Some(Self {
            master,
            overrides: single_overrides,
            this_and_future,
            rrule_split_cutoff,
            min_start_utc,
            max_end_utc,
        }))
    }

    /// Expand all occurrences intersecting `[start_utc, end_utc)`.
    pub fn expand_window(
        &self,
        start_utc: i64,
        end_utc: i64,
        skip_cancelled: bool,
        budget: IterationBudget,
        out: &mut Vec<Occurrence>,
    ) -> Result<(), RRuleError> {
        if end_utc <= self.min_start_utc {
            return Ok(());
        }
        if let Some(max_end) = self.max_end_utc {
            if start_utc > max_end {
                return Ok(());
            }
        }

        for ov in &self.overrides {
            if skip_cancelled && ov.cancelled {
                continue;
            }
            if overlaps_window(ov.start_utc, ov.end_utc, start_utc, end_utc) {
                out.push(Occurrence {
                    component_id: ov.component_id,
                    kind: ov.kind.clone(),
                    start_utc: ov.start_utc,
                    end_utc: ov.end_utc,
                    start_local: ov.start_local.clone(),
                    end_local: ov.end_local.clone(),
                    is_all_day: ov.is_all_day,
                    recurrence_id_utc: Some(ov.recurrence_id_utc),
                    parent_id: None,
                });
            }
        }

        if let Some(master) = &self.master {
            if !(skip_cancelled && master.cancelled) {
                expand_master_window(
                    master,
                    &self.this_and_future,
                    self.rrule_split_cutoff,
                    skip_cancelled,
                    start_utc,
                    end_utc,
                    budget,
                    out,
                )?;
            }
        }

        // Expand any THISANDFUTURE overrides that define their own replacement RRULE (or have no master).
        for (idx, tf) in self.this_and_future.iter().enumerate() {
            if skip_cancelled && tf.cancelled {
                continue;
            }
            if tf.override_rrules.is_empty() {
                if self.master.is_none()
                    && overlaps_window(tf.start_utc, tf.end_utc, start_utc, end_utc)
                {
                    out.push(Occurrence {
                        component_id: tf.component_id,
                        kind: tf.kind.clone(),
                        start_utc: tf.start_utc,
                        end_utc: tf.end_utc,
                        start_local: tf.start_local.clone(),
                        end_local: tf.end_local.clone(),
                        is_all_day: tf.is_all_day,
                        recurrence_id_utc: Some(tf.recurrence_id_utc),
                        parent_id: None,
                    });
                }
                continue;
            }
            let next_split = self
                .this_and_future
                .get(idx + 1)
                .map(|n| n.recurrence_id_utc);
            expand_this_and_future_rrules(
                tf,
                next_split,
                &self.overrides,
                self.master.as_ref().map(|m| &m.exdates),
                start_utc,
                end_utc,
                budget,
                out,
            )?;
        }

        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
fn expand_master_window(
    master: &MasterSeries,
    this_and_future: &[OverrideEntry],
    rrule_split_cutoff: Option<i64>,
    skip_cancelled: bool,
    start_utc: i64,
    end_utc: i64,
    budget: IterationBudget,
    out: &mut Vec<Occurrence>,
) -> Result<(), RRuleError> {
    let max_span_secs = match master.span {
        SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
        SpanDuration::Seconds(s) => s,
    };
    let max_back_shift = this_and_future
        .iter()
        .map(|tf| (-tf.time_shift_secs).max(0))
        .max()
        .unwrap_or(0);
    let ff_utc = start_utc
        .saturating_sub(max_span_secs + max_back_shift)
        .max(master.min_start_utc);
    let ff_local = utc_to_local_approx(ff_utc, &master.tz);
    let win_end_local = utc_to_local_approx(end_utc + max_back_shift + 86_400, &master.tz);

    let mut seen_instants: FxHashSet<i64> = FxHashSet::default();

    let mut emit_candidate = |cand: &CalDateTime, custom_span: Option<SpanDuration>| {
        let s_utc = resolve_local_instant(cand, &master.tz);
        if rrule_split_cutoff.is_some_and(|cut| s_utc >= cut) {
            return;
        }
        if master.exdates.matches(s_utc, cand.date, cand.time) {
            return;
        }
        if !seen_instants.insert(s_utc) {
            return;
        }
        if let Some(occ) = apply_this_and_future_shift(
            master,
            this_and_future,
            s_utc,
            cand,
            custom_span.unwrap_or(master.span),
            skip_cancelled,
        ) {
            if overlaps_window(occ.start_utc, occ.end_utc, start_utc, end_utc) {
                out.push(occ);
            }
        }
    };

    emit_candidate(&master.start_local, None);

    let dtstart_ldt = LocalDateTime::new(master.start_local.date, master.start_local.time);
    for rule in &master.rrules {
        let until_utc = resolve_until_utc(rule, &master.tz);
        let until_local = until_utc.map(|u| utc_to_local_approx(u + 86_400, &master.tz));
        let iter = RRuleIter::new(rule, dtstart_ldt, until_local, Some(ff_local), budget);
        for item in iter {
            let ldt = item?;
            if ldt > win_end_local {
                break;
            }
            let cand = CalDateTime {
                date: ldt.date,
                time: ldt.time,
                tz: master.start_local.tz.clone(),
            };
            let s_utc = resolve_local_instant(&cand, &master.tz);
            if until_utc.is_some_and(|u| s_utc > u)
                || rrule_split_cutoff.is_some_and(|cut| s_utc >= cut)
                || s_utc >= end_utc + max_back_shift
            {
                break;
            }
            emit_candidate(&cand, None);
        }
    }

    for rdt in &master.rdates {
        emit_candidate(&rdt.start_local, rdt.custom_span);
    }

    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn expand_this_and_future_rrules(
    tf: &OverrideEntry,
    next_split_utc: Option<i64>,
    single_overrides: &[OverrideEntry],
    master_exdates: Option<&ExclusionSet>,
    start_utc: i64,
    end_utc: i64,
    budget: IterationBudget,
    out: &mut Vec<Occurrence>,
) -> Result<(), RRuleError> {
    let dtstart_ldt = LocalDateTime::new(tf.start_local.date, tf.start_local.time);
    let max_span_secs = match tf.span {
        SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
        SpanDuration::Seconds(s) => s,
    };
    let ff_utc = start_utc.saturating_sub(max_span_secs).max(tf.start_utc);
    let ff_local = utc_to_local_approx(ff_utc, &tf.tz);
    let win_end_local = utc_to_local_approx(end_utc + 86_400, &tf.tz);
    let mut seen: FxHashSet<i64> = FxHashSet::default();

    for rule in &tf.override_rrules {
        let until_utc = resolve_until_utc(rule, &tf.tz);
        let until_local = until_utc.map(|u| utc_to_local_approx(u + 86_400, &tf.tz));
        for item in RRuleIter::new(rule, dtstart_ldt, until_local, Some(ff_local), budget) {
            let ldt = item?;
            if ldt > win_end_local {
                break;
            }
            let cand = CalDateTime {
                date: ldt.date,
                time: ldt.time,
                tz: tf.start_local.tz.clone(),
            };
            let s_utc = resolve_local_instant(&cand, &tf.tz);
            if until_utc.is_some_and(|u| s_utc > u)
                || next_split_utc.is_some_and(|n| s_utc >= n)
                || s_utc >= end_utc
            {
                break;
            }
            if master_exdates.is_some_and(|ex| ex.matches(s_utc, ldt.date, ldt.time))
                || single_overrides
                    .iter()
                    .any(|o| o.recurrence_id_utc == s_utc)
                || !seen.insert(s_utc)
            {
                continue;
            }
            let (e_utc, e_local) = compute_end_local(&cand, s_utc, tf.span, &tf.tz);
            if overlaps_window(s_utc, e_utc, start_utc, end_utc) {
                out.push(Occurrence {
                    component_id: tf.component_id,
                    kind: tf.kind.clone(),
                    start_utc: s_utc,
                    end_utc: e_utc,
                    start_local: cand,
                    end_local: e_local,
                    is_all_day: tf.is_all_day,
                    recurrence_id_utc: Some(s_utc),
                    parent_id: None,
                });
            }
        }
    }
    Ok(())
}

/// Apply the latest `THISANDFUTURE` override (`<= s_utc`) to a master occurrence.
pub fn apply_this_and_future_shift(
    master: &MasterSeries,
    this_and_future: &[OverrideEntry],
    s_utc: i64,
    cand_local: &CalDateTime,
    base_span: SpanDuration,
    skip_cancelled: bool,
) -> Option<Occurrence> {
    let idx = this_and_future.partition_point(|tf| tf.recurrence_id_utc <= s_utc);
    if idx == 0 {
        let (e_utc, e_local) = compute_end_local(cand_local, s_utc, base_span, &master.tz);
        return Some(Occurrence {
            component_id: master.component_id,
            kind: master.kind.clone(),
            start_utc: s_utc,
            end_utc: e_utc,
            start_local: cand_local.clone(),
            end_local: e_local,
            is_all_day: master.is_all_day,
            recurrence_id_utc: Some(s_utc),
            parent_id: None,
        });
    }

    let tf = &this_and_future[idx - 1];
    if skip_cancelled && tf.cancelled {
        return None;
    }

    let shifted_utc = s_utc + tf.time_shift_secs;
    let shifted_local = if tf.time_shift_secs == 0 && tf.is_all_day == master.is_all_day {
        cand_local.clone()
    } else {
        let ldt = utc_to_local_approx(shifted_utc, &tf.tz);
        CalDateTime {
            date: ldt.date,
            time: ldt.time,
            tz: tf.start_local.tz.clone(),
        }
    };
    let (e_utc, e_local) = compute_end_local(&shifted_local, shifted_utc, tf.span, &tf.tz);
    Some(Occurrence {
        component_id: tf.component_id,
        kind: tf.kind.clone(),
        start_utc: shifted_utc,
        end_utc: e_utc,
        start_local: shifted_local,
        end_local: e_local,
        is_all_day: tf.is_all_day,
        recurrence_id_utc: Some(s_utc),
        parent_id: None,
    })
}

#[inline]
pub const fn overlaps_window(occ_start: i64, occ_end: i64, win_start: i64, win_end: i64) -> bool {
    if occ_end > occ_start {
        occ_start < win_end && occ_end > win_start
    } else {
        occ_start >= win_start && occ_start < win_end
    }
}

pub fn resolve_local_instant(dt: &CalDateTime, tz: &CalendarTimeZone) -> i64 {
    if matches!(dt.tz, TimeZoneRef::Utc) {
        return dt.to_naive_timestamp();
    }
    match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
        Ok(res) => res.utc_timestamp,
        Err(_) => dt.to_naive_timestamp(),
    }
}

pub fn utc_to_local_approx(utc_ts: i64, tz: &CalendarTimeZone) -> LocalDateTime {
    let off = tz.offset_at_utc(utc_ts).unwrap_or(0);
    LocalDateTime::from_naive_timestamp(utc_ts + i64::from(off))
}

pub fn resolve_until_utc(rule: &RRule, tz: &CalendarTimeZone) -> Option<i64> {
    let u = rule.until.as_ref()?;
    Some(match u {
        UntilValue::Date(d) => {
            let end_time = CalTime {
                hour: 23,
                minute: 59,
                second: 59,
            };
            match tz.resolve_local(*d, end_time, DstDisambiguation::Compatible) {
                Ok(res) => res.utc_timestamp,
                Err(_) => i64::from(d.to_days_since_epoch()) * 86_400 + 86_399,
            }
        }
        UntilValue::DateTime(dt) => {
            if matches!(dt.tz, TimeZoneRef::Utc) {
                dt.to_naive_timestamp()
            } else {
                match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
                    Ok(res) => res.utc_timestamp,
                    Err(_) => dt.to_naive_timestamp(),
                }
            }
        }
    })
}

fn build_override(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Result<Option<OverrideEntry>, RRuleError> {
    let Some(rid_prop) = comp.property(&PropertyName::RecurrenceId) else {
        return Ok(None);
    };
    let (rid_dt, rid_date) = match &rid_prop.value {
        PropertyValue::Date(d) => (
            CalDateTime {
                date: *d,
                time: CalTime {
                    hour: 0,
                    minute: 0,
                    second: 0,
                },
                tz: TimeZoneRef::Floating,
            },
            *d,
        ),
        PropertyValue::DateTime(dt) => (dt.clone(), dt.date),
        _ => return Ok(None),
    };
    let (recurrence_id_utc, _) = resolve_datetime_with_fallback(&rid_dt, reg, default_tz);
    let is_this_and_future = rid_prop
        .param_first(&ParamName::Range)
        .is_some_and(|r| r.eq_ignore_ascii_case("THISANDFUTURE"));

    let Some(timing) = extract_component_timing(comp, reg, default_tz) else {
        return Ok(None);
    };
    let (end_utc, end_local) = compute_end_local(
        &timing.start_local,
        timing.start_utc,
        timing.span,
        &timing.tz,
    );

    let mut override_rrules = Vec::new();
    for prop in comp.properties_named(&PropertyName::RRule) {
        if let Some(raw) = prop.value.as_str() {
            override_rrules.push(RRule::parse(raw)?);
        }
    }

    let sequence = match comp.property(&PropertyName::Sequence).map(|p| &p.value) {
        Some(PropertyValue::Integer(v)) => *v,
        _ => 0,
    };

    Ok(Some(OverrideEntry {
        component_id: comp.id(),
        kind: comp.kind().clone(),
        recurrence_id_utc,
        recurrence_id_date: rid_date,
        is_this_and_future,
        start_utc: timing.start_utc,
        end_utc,
        start_local: timing.start_local,
        end_local,
        span: timing.span,
        tz: timing.tz,
        time_shift_secs: timing.start_utc - recurrence_id_utc,
        override_rrules,
        is_all_day: timing.is_all_day,
        cancelled: timing.cancelled,
        sequence,
    }))
}

fn push_period_rdate(
    period: &CalPeriod,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
    out: &mut Vec<RDateEntry>,
) {
    match period {
        CalPeriod::Explicit { start, end } => {
            let (s_utc, _) = resolve_datetime_with_fallback(start, reg, default_tz);
            let (e_utc, _) = resolve_datetime_with_fallback(end, reg, default_tz);
            out.push(RDateEntry {
                start_local: start.clone(),
                custom_span: Some(SpanDuration::Seconds((e_utc - s_utc).max(0))),
            });
        }
        CalPeriod::Duration { start, duration } => {
            out.push(RDateEntry {
                start_local: start.clone(),
                custom_span: Some(SpanDuration::Seconds(duration.total_seconds().max(0))),
            });
        }
    }
}

fn extract_rdates(
    comp: ComponentRef<'_>,
    default_start: &CalDateTime,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Vec<RDateEntry> {
    let mut rdates = Vec::new();
    for prop in comp.properties_named(&PropertyName::RDate) {
        match &prop.value {
            PropertyValue::DateTimeList(list) => {
                for dt in list {
                    rdates.push(RDateEntry {
                        start_local: dt.clone(),
                        custom_span: None,
                    });
                }
            }
            PropertyValue::DateList(list) => {
                for &d in list {
                    rdates.push(RDateEntry {
                        start_local: CalDateTime {
                            date: d,
                            time: default_start.time,
                            tz: default_start.tz.clone(),
                        },
                        custom_span: None,
                    });
                }
            }
            PropertyValue::DateTime(dt) => rdates.push(RDateEntry {
                start_local: dt.clone(),
                custom_span: None,
            }),
            PropertyValue::Date(d) => rdates.push(RDateEntry {
                start_local: CalDateTime {
                    date: *d,
                    time: default_start.time,
                    tz: default_start.tz.clone(),
                },
                custom_span: None,
            }),
            PropertyValue::Period(p) => push_period_rdate(p, reg, default_tz, &mut rdates),
            PropertyValue::PeriodList(list) => {
                for p in list {
                    push_period_rdate(p, reg, default_tz, &mut rdates);
                }
            }
            _ => {}
        }
    }
    rdates
}

fn build_master(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
    mut exdates: ExclusionSet,
) -> Result<Option<MasterSeries>, RRuleError> {
    let Some(timing) = extract_component_timing(comp, reg, default_tz) else {
        return Ok(None);
    };

    let mut rrules = Vec::new();
    for prop in comp.properties_named(&PropertyName::RRule) {
        if let Some(raw) = prop.value.as_str() {
            rrules.push(RRule::parse(raw)?);
        }
    }

    let rdates = extract_rdates(comp, &timing.start_local, reg, default_tz);

    for prop in comp.properties_named(&PropertyName::ExDate) {
        exdates.insert_exdate_value(&prop.value, reg, default_tz);
    }

    let mut min_start_utc = timing.start_utc;
    for rdt in &rdates {
        let (u, _) = resolve_datetime_with_fallback(&rdt.start_local, reg, default_tz);
        min_start_utc = min_start_utc.min(u);
    }

    let max_end_utc = if rrules.is_empty() {
        let mut max_e = compute_end_local(
            &timing.start_local,
            timing.start_utc,
            timing.span,
            &timing.tz,
        )
        .0;
        for rdt in &rdates {
            let s = resolve_local_instant(&rdt.start_local, &timing.tz);
            let span = rdt.custom_span.unwrap_or(timing.span);
            let e = compute_end_local(&rdt.start_local, s, span, &timing.tz).0;
            max_e = max_e.max(e);
        }
        Some(max_e)
    } else if rrules.iter().all(|r| r.until.is_some()) {
        let mut max_e = compute_end_local(
            &timing.start_local,
            timing.start_utc,
            timing.span,
            &timing.tz,
        )
        .0;
        for r in &rrules {
            if let Some(u) = resolve_until_utc(r, &timing.tz) {
                let span_secs = match timing.span {
                    SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
                    SpanDuration::Seconds(s) => s,
                };
                max_e = max_e.max(u + span_secs);
            }
        }
        for rdt in &rdates {
            let s = resolve_local_instant(&rdt.start_local, &timing.tz);
            let span = rdt.custom_span.unwrap_or(timing.span);
            let e = compute_end_local(&rdt.start_local, s, span, &timing.tz).0;
            max_e = max_e.max(e);
        }
        Some(max_e)
    } else {
        None
    };

    Ok(Some(MasterSeries {
        component_id: comp.id(),
        kind: comp.kind().clone(),
        start_local: timing.start_local,
        tz: timing.tz,
        span: timing.span,
        is_all_day: timing.is_all_day,
        cancelled: timing.cancelled,
        rrules,
        rdates,
        exdates,
        min_start_utc,
        max_end_utc,
    }))
}
