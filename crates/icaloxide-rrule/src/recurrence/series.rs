use std::cmp::Reverse;
use std::collections::BinaryHeap;

use icaloxide_core::datetime::{CalDate, CalDateTime, CalTime, TimeZoneRef};
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

/// Resolve a [`CalDateTime`] to `(utc_timestamp, CalendarTimeZone)`, falling back to `default_tz`
/// or UTC if a referenced `TZID` is unknown or floating.
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

/// Extract timing (`DTSTART`/`DUE`/`DTEND`/`DURATION`) from a component (`VEVENT`, `VTODO`, `VJOURNAL`, `AVAILABLE`).
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
    pub rdates: Vec<CalDateTime>,
    pub exdates: ExclusionSet,
    pub min_start_utc: i64,
    pub max_end_utc: Option<i64>,
}

/// Compiled recurrence series for one `(ComponentKind, UID)` group.
#[derive(Debug, Clone)]
pub struct CompiledSeries {
    pub master: Option<MasterSeries>,
    pub overrides: Vec<OverrideEntry>,
    pub this_and_future_cutoff: Option<i64>,
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
        let mut overrides = Vec::with_capacity(override_comps.len());
        let mut override_exclusions = ExclusionSet::default();
        let mut this_and_future_cutoff: Option<i64> = None;

        for &ov in override_comps {
            if let Some(entry) = build_override(ov, reg, default_tz) {
                override_exclusions
                    .utc_instants
                    .insert(entry.recurrence_id_utc);
                if entry.is_this_and_future {
                    this_and_future_cutoff = Some(match this_and_future_cutoff {
                        Some(cur) => cur.min(entry.recurrence_id_utc),
                        None => entry.recurrence_id_utc,
                    });
                }
                overrides.push(entry);
            }
        }

        // Deduplicate overrides targeting the same RECURRENCE-ID by keeping highest SEQUENCE (or later).
        overrides.sort_by_key(|o| (o.recurrence_id_utc, o.sequence, o.component_id.0));
        overrides.dedup_by(|later, earlier| {
            if later.recurrence_id_utc == earlier.recurrence_id_utc {
                *earlier = later.clone();
                true
            } else {
                false
            }
        });

        let master = if let Some(mc) = master_comp {
            build_master(mc, reg, default_tz, override_exclusions)?
        } else {
            None
        };

        if master.is_none() && overrides.is_empty() {
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
        for o in &overrides {
            min_start_utc = min_start_utc.min(o.start_utc);
            if let Some(cur_max) = max_end_utc {
                max_end_utc = Some(cur_max.max(o.end_utc));
            }
        }

        Ok(Some(Self {
            master,
            overrides,
            this_and_future_cutoff,
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
        if start_utc < self.min_start_utc && end_utc <= self.min_start_utc {
            return Ok(());
        }
        if let Some(max_end) = self.max_end_utc {
            if start_utc > max_end {
                return Ok(());
            }
        }

        // Emit matching overrides
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

        let Some(master) = &self.master else {
            return Ok(());
        };
        if skip_cancelled && master.cancelled {
            return Ok(());
        }

        expand_master_window(
            master,
            self.this_and_future_cutoff,
            start_utc,
            end_utc,
            budget,
            out,
        )
    }

    /// Create a streaming chronological cursor yielding occurrences with `end_utc >= earliest_end_utc`
    /// (or `start_utc >= earliest_end_utc` for zero-duration events).
    pub fn into_stream(
        &self,
        earliest_end_utc: i64,
        skip_cancelled: bool,
        budget: IterationBudget,
    ) -> SeriesStream<'_> {
        SeriesStream::new(self, earliest_end_utc, skip_cancelled, budget)
    }
}

fn expand_master_window(
    master: &MasterSeries,
    cutoff: Option<i64>,
    start_utc: i64,
    end_utc: i64,
    budget: IterationBudget,
    out: &mut Vec<Occurrence>,
) -> Result<(), RRuleError> {
    let max_span_secs = match master.span {
        SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
        SpanDuration::Seconds(s) => s,
    };
    let ff_utc = start_utc
        .saturating_sub(max_span_secs)
        .max(master.min_start_utc);
    let ff_local = utc_to_local_approx(ff_utc, &master.tz);
    let win_end_local = utc_to_local_approx(end_utc + 86_400, &master.tz);

    let mut seen_instants: FxHashSet<i64> = FxHashSet::default();

    let mut emit_candidate = |cand: &CalDateTime| {
        let s_utc = resolve_local_instant(cand, &master.tz);
        if s_utc >= end_utc {
            return;
        }
        if is_excluded(master, cutoff, s_utc, cand.date, cand.time) {
            return;
        }
        if !seen_instants.insert(s_utc) {
            return;
        }
        let (e_utc, e_local) = compute_end_local(cand, s_utc, master.span, &master.tz);
        if overlaps_window(s_utc, e_utc, start_utc, end_utc) {
            out.push(make_master_occ(master, s_utc, e_utc, cand.clone(), e_local));
        }
    };

    emit_candidate(&master.start_local);

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
            if until_utc.is_some_and(|u| s_utc > u) || s_utc >= end_utc {
                break;
            }
            emit_candidate(&cand);
        }
    }

    for rdt in &master.rdates {
        emit_candidate(rdt);
    }

    Ok(())
}

/// Streaming chronological occurrence cursor for one [`CompiledSeries`].
pub struct SeriesStream<'a> {
    series: &'a CompiledSeries,
    earliest_end_utc: i64,
    skip_cancelled: bool,
    rule_iters: Vec<RuleCursor<'a>>,
    static_items: BinaryHeap<Reverse<OccurrenceKey>>,
    seen_instants: FxHashSet<i64>,
}

#[derive(Debug, Clone)]
struct OccurrenceKey {
    occ: Occurrence,
    is_master: bool,
}

impl PartialEq for OccurrenceKey {
    fn eq(&self, other: &Self) -> bool {
        self.occ.start_utc == other.occ.start_utc
            && self.occ.end_utc == other.occ.end_utc
            && self.occ.component_id.0 == other.occ.component_id.0
    }
}
impl Eq for OccurrenceKey {}
impl PartialOrd for OccurrenceKey {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for OccurrenceKey {
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

struct RuleCursor<'a> {
    iter: RRuleIter<'a>,
    until_utc: Option<i64>,
    next_occ: Option<Occurrence>,
}

impl<'a> SeriesStream<'a> {
    fn new(
        series: &'a CompiledSeries,
        earliest_end_utc: i64,
        skip_cancelled: bool,
        budget: IterationBudget,
    ) -> Self {
        let mut static_items = BinaryHeap::new();
        for ov in &series.overrides {
            if skip_cancelled && ov.cancelled {
                continue;
            }
            if keeps_for_after(ov.start_utc, ov.end_utc, earliest_end_utc) {
                static_items.push(Reverse(OccurrenceKey {
                    occ: Occurrence {
                        component_id: ov.component_id,
                        kind: ov.kind.clone(),
                        start_utc: ov.start_utc,
                        end_utc: ov.end_utc,
                        start_local: ov.start_local.clone(),
                        end_local: ov.end_local.clone(),
                        is_all_day: ov.is_all_day,
                        recurrence_id_utc: Some(ov.recurrence_id_utc),
                        parent_id: None,
                    },
                    is_master: false,
                }));
            }
        }

        let mut rule_iters = Vec::new();
        if let Some(master) = &series.master {
            if !(skip_cancelled && master.cancelled) {
                let max_span_secs = match master.span {
                    SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
                    SpanDuration::Seconds(s) => s,
                };
                let ff_utc = earliest_end_utc
                    .saturating_sub(max_span_secs)
                    .max(master.min_start_utc);
                let ff_local = utc_to_local_approx(ff_utc, &master.tz);
                let dtstart_ldt =
                    LocalDateTime::new(master.start_local.date, master.start_local.time);

                if master.rrules.is_empty() {
                    let s_utc = resolve_local_instant(&master.start_local, &master.tz);
                    let (e_utc, e_loc) =
                        compute_end_local(&master.start_local, s_utc, master.span, &master.tz);
                    if keeps_for_after(s_utc, e_utc, earliest_end_utc)
                        && !is_excluded(
                            master,
                            series.this_and_future_cutoff,
                            s_utc,
                            master.start_local.date,
                            master.start_local.time,
                        )
                    {
                        static_items.push(Reverse(OccurrenceKey {
                            occ: make_master_occ(
                                master,
                                s_utc,
                                e_utc,
                                master.start_local.clone(),
                                e_loc,
                            ),
                            is_master: true,
                        }));
                    }
                }

                for rdt in &master.rdates {
                    let s_utc = resolve_local_instant(rdt, &master.tz);
                    let (e_utc, e_loc) = compute_end_local(rdt, s_utc, master.span, &master.tz);
                    if keeps_for_after(s_utc, e_utc, earliest_end_utc)
                        && !is_excluded(
                            master,
                            series.this_and_future_cutoff,
                            s_utc,
                            rdt.date,
                            rdt.time,
                        )
                    {
                        static_items.push(Reverse(OccurrenceKey {
                            occ: make_master_occ(master, s_utc, e_utc, rdt.clone(), e_loc),
                            is_master: true,
                        }));
                    }
                }

                for rule in &master.rrules {
                    let until_utc = resolve_until_utc(rule, &master.tz);
                    let until_local =
                        until_utc.map(|u| utc_to_local_approx(u + 86_400, &master.tz));
                    let iter =
                        RRuleIter::new(rule, dtstart_ldt, until_local, Some(ff_local), budget);
                    rule_iters.push(RuleCursor {
                        iter,
                        until_utc,
                        next_occ: None,
                    });
                }
            }
        }

        Self {
            series,
            earliest_end_utc,
            skip_cancelled,
            rule_iters,
            static_items,
            seen_instants: FxHashSet::default(),
        }
    }

    pub fn next_occurrence(&mut self) -> Result<Option<Occurrence>, RRuleError> {
        loop {
            if let Some(master) = &self.series.master {
                if !(self.skip_cancelled && master.cancelled) {
                    for cursor in &mut self.rule_iters {
                        if cursor.next_occ.is_none() {
                            cursor.next_occ = advance_cursor(
                                cursor,
                                master,
                                self.series.this_and_future_cutoff,
                                self.earliest_end_utc,
                            )?;
                        }
                    }
                }
            }

            let best_rule = self
                .rule_iters
                .iter()
                .enumerate()
                .filter_map(|(idx, c)| c.next_occ.as_ref().map(|o| (idx, o.start_utc, o.end_utc)))
                .min_by_key(|&(_, s, e)| (s, e));

            let best_static = self.static_items.peek().map(|r| &r.0);

            let chosen = match (best_rule, best_static) {
                (None, None) => return Ok(None),
                (Some((idx, _, _)), None) => {
                    let occ = self.rule_iters[idx].next_occ.take().unwrap();
                    (occ, true)
                }
                (None, Some(_)) => {
                    let item = self.static_items.pop().unwrap().0;
                    (item.occ, item.is_master)
                }
                (Some((idx, rs, re)), Some(st)) => {
                    if (rs, re) < (st.occ.start_utc, st.occ.end_utc) {
                        let occ = self.rule_iters[idx].next_occ.take().unwrap();
                        (occ, true)
                    } else {
                        let item = self.static_items.pop().unwrap().0;
                        (item.occ, item.is_master)
                    }
                }
            };

            let (occ, is_master) = chosen;
            if is_master && !self.seen_instants.insert(occ.start_utc) {
                continue;
            }
            return Ok(Some(occ));
        }
    }
}

fn advance_cursor(
    cursor: &mut RuleCursor<'_>,
    master: &MasterSeries,
    cutoff: Option<i64>,
    earliest_end_utc: i64,
) -> Result<Option<Occurrence>, RRuleError> {
    for item in &mut cursor.iter {
        let ldt = item?;
        let cand_local = CalDateTime {
            date: ldt.date,
            time: ldt.time,
            tz: master.start_local.tz.clone(),
        };
        let occ_start_utc = resolve_local_instant(&cand_local, &master.tz);
        if cursor.until_utc.is_some_and(|u| occ_start_utc > u) {
            return Ok(None);
        }
        if is_excluded(master, cutoff, occ_start_utc, ldt.date, ldt.time) {
            continue;
        }
        let (occ_end_utc, end_local) =
            compute_end_local(&cand_local, occ_start_utc, master.span, &master.tz);
        if !keeps_for_after(occ_start_utc, occ_end_utc, earliest_end_utc) {
            continue;
        }
        return Ok(Some(make_master_occ(
            master,
            occ_start_utc,
            occ_end_utc,
            cand_local,
            end_local,
        )));
    }
    Ok(None)
}

#[inline]
const fn keeps_for_after(start_utc: i64, end_utc: i64, earliest_end_utc: i64) -> bool {
    if end_utc > start_utc {
        end_utc > earliest_end_utc
    } else {
        start_utc >= earliest_end_utc
    }
}

#[inline]
pub const fn overlaps_window(occ_start: i64, occ_end: i64, win_start: i64, win_end: i64) -> bool {
    if occ_end > occ_start {
        occ_start < win_end && occ_end > win_start
    } else {
        occ_start >= win_start && occ_start < win_end
    }
}

#[inline]
fn is_excluded(
    master: &MasterSeries,
    this_and_future_cutoff: Option<i64>,
    utc_ts: i64,
    date: CalDate,
    time: CalTime,
) -> bool {
    if this_and_future_cutoff.is_some_and(|cut| utc_ts >= cut) {
        return true;
    }
    master.exdates.matches(utc_ts, date, time)
}

fn resolve_local_instant(dt: &CalDateTime, tz: &CalendarTimeZone) -> i64 {
    if matches!(dt.tz, TimeZoneRef::Utc) {
        return dt.to_naive_timestamp();
    }
    match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
        Ok(res) => res.utc_timestamp,
        Err(_) => dt.to_naive_timestamp(),
    }
}

fn utc_to_local_approx(utc_ts: i64, tz: &CalendarTimeZone) -> LocalDateTime {
    let off = tz.offset_at_utc(utc_ts).unwrap_or(0);
    LocalDateTime::from_naive_timestamp(utc_ts + i64::from(off))
}

fn resolve_until_utc(rule: &RRule, tz: &CalendarTimeZone) -> Option<i64> {
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

fn make_master_occ(
    master: &MasterSeries,
    start_utc: i64,
    end_utc: i64,
    start_local: CalDateTime,
    end_local: CalDateTime,
) -> Occurrence {
    Occurrence {
        component_id: master.component_id,
        kind: master.kind.clone(),
        start_utc,
        end_utc,
        start_local,
        end_local,
        is_all_day: master.is_all_day,
        recurrence_id_utc: Some(start_utc),
        parent_id: None,
    }
}

fn build_override(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Option<OverrideEntry> {
    let rid_prop = comp.property(&PropertyName::RecurrenceId)?;
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
        _ => return None,
    };
    let (recurrence_id_utc, _) = resolve_datetime_with_fallback(&rid_dt, reg, default_tz);
    let is_this_and_future = rid_prop
        .param_first(&ParamName::Range)
        .is_some_and(|r| r.eq_ignore_ascii_case("THISANDFUTURE"));

    let timing = extract_component_timing(comp, reg, default_tz)?;
    let (end_utc, end_local) = compute_end_local(
        &timing.start_local,
        timing.start_utc,
        timing.span,
        &timing.tz,
    );

    let sequence = match comp.property(&PropertyName::Sequence).map(|p| &p.value) {
        Some(PropertyValue::Integer(v)) => *v,
        _ => 0,
    };

    Some(OverrideEntry {
        component_id: comp.id(),
        kind: comp.kind().clone(),
        recurrence_id_utc,
        recurrence_id_date: rid_date,
        is_this_and_future,
        start_utc: timing.start_utc,
        end_utc,
        start_local: timing.start_local,
        end_local,
        is_all_day: timing.is_all_day,
        cancelled: timing.cancelled,
        sequence,
    })
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

    let mut rdates = Vec::new();
    for prop in comp.properties_named(&PropertyName::RDate) {
        match &prop.value {
            PropertyValue::DateTimeList(list) => rdates.extend(list.iter().cloned()),
            PropertyValue::DateList(list) => {
                for &d in list {
                    rdates.push(CalDateTime {
                        date: d,
                        time: timing.start_local.time,
                        tz: timing.start_local.tz.clone(),
                    });
                }
            }
            PropertyValue::DateTime(dt) => rdates.push(dt.clone()),
            PropertyValue::Date(d) => rdates.push(CalDateTime {
                date: *d,
                time: timing.start_local.time,
                tz: timing.start_local.tz.clone(),
            }),
            _ => {}
        }
    }

    for prop in comp.properties_named(&PropertyName::ExDate) {
        exdates.insert_exdate_value(&prop.value, reg, default_tz);
    }

    let mut min_start_utc = timing.start_utc;
    for rdt in &rdates {
        let (u, _) = resolve_datetime_with_fallback(rdt, reg, default_tz);
        min_start_utc = min_start_utc.min(u);
    }

    // Determine conservative upper bound when no RRULE is unbounded.
    let max_end_utc = if rrules.is_empty() {
        let mut max_e = compute_end_local(
            &timing.start_local,
            timing.start_utc,
            timing.span,
            &timing.tz,
        )
        .0;
        for rdt in &rdates {
            let s = resolve_local_instant(rdt, &timing.tz);
            let e = compute_end_local(rdt, s, timing.span, &timing.tz).0;
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
            let s = resolve_local_instant(rdt, &timing.tz);
            let e = compute_end_local(rdt, s, timing.span, &timing.tz).0;
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
