pub mod components;

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::sync::Arc;

use compact_str::CompactString;
use icaloxide_core::datetime::{CalDate, CalDateTime, CalTime};
use icaloxide_core::model::{Calendar, ComponentId, ComponentKind, ComponentRef, PropertyName};
use icaloxide_core::tz::{CalendarTimeZone, DstDisambiguation, TzRegistry};
use rustc_hash::FxHashMap;
use smallvec::SmallVec;

use crate::error::{IterationBudget, RRuleError};
use crate::query::components::{expand_alarms_for_parent, extract_freebusy_occurrences};
use crate::recurrence::series::{
    compute_end_local, extract_component_timing, overlaps_window, CompiledSeries,
};
use crate::recurrence::SpanDuration;

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
    pub recurrence_id_utc: Option<i64>,
    pub parent_id: Option<ComponentId>,
}

/// Configuration for building a [`CalendarQuery`].
#[derive(Debug, Clone)]
pub struct QueryOptions {
    pub components: SmallVec<[ComponentKind; 4]>,
    pub skip_cancelled: bool,
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
            default_tz: None,
            budget: IterationBudget::default(),
        }
    }
}

/// Interval-indexed calendar query engine supporting `at`, `between`, `after`, and `count`.
#[derive(Debug)]
pub struct CalendarQuery {
    cal: Arc<Calendar>,
    reg: TzRegistry,
    default_tz: CalendarTimeZone,
    options: QueryOptions,
    singles: Vec<Occurrence>,
    singles_prefix_max_end: Vec<i64>,
    series: Vec<CompiledSeries>,
    alarm_parent_singles: Vec<Occurrence>,
    alarm_parent_series: Vec<CompiledSeries>,
    want_alarms: bool,
}

impl CalendarQuery {
    /// Build an indexed [`CalendarQuery`] over `cal` using `options`.
    pub fn new(cal: Arc<Calendar>, options: QueryOptions) -> Result<Self, RRuleError> {
        let reg = cal.tz_registry();
        let default_tz = options
            .default_tz
            .clone()
            .or_else(|| reg.default_timezone())
            .unwrap_or(CalendarTimeZone::Utc);

        let want_alarms = options.components.contains(&ComponentKind::VAlarm);
        let want_non_alarm: SmallVec<[ComponentKind; 4]> = options
            .components
            .iter()
            .filter(|k| *k != &ComponentKind::VAlarm)
            .cloned()
            .collect();

        let mut singles = Vec::new();
        let mut series = Vec::new();
        let mut alarm_parent_singles = Vec::new();
        let mut alarm_parent_series = Vec::new();

        // Collect VFREEBUSY directly if requested.
        if want_non_alarm.contains(&ComponentKind::VFreeBusy) {
            for fb in cal.freebusies() {
                singles.extend(extract_freebusy_occurrences(fb, &reg, &default_tz));
            }
        }

        // Partition components of requested kinds (and VEVENT/VTODO if VALARM is requested).
        let mut grouped: FxHashMap<(ComponentKind, CompactString), Vec<ComponentRef<'_>>> =
            FxHashMap::default();
        let mut ungrouped: Vec<ComponentRef<'_>> = Vec::new();

        for child in cal.root().children() {
            let kind = child.kind();
            if *kind == ComponentKind::VTimezone || *kind == ComponentKind::VFreeBusy {
                continue;
            }
            let is_direct = want_non_alarm.contains(kind);
            let is_alarm_parent =
                want_alarms && matches!(kind, ComponentKind::VEvent | ComponentKind::VTodo);
            if !is_direct && !is_alarm_parent {
                continue;
            }

            if let Some(uid) = child.uid() {
                grouped
                    .entry((kind.clone(), CompactString::from(uid)))
                    .or_default()
                    .push(child);
            } else {
                ungrouped.push(child);
            }
        }

        for comp in ungrouped {
            let is_direct = want_non_alarm.contains(comp.kind());
            let has_alarm = want_alarms && has_valarm_child(comp);
            classify_component_group(
                &[comp],
                &reg,
                &default_tz,
                options.skip_cancelled,
                is_direct,
                has_alarm,
                &mut singles,
                &mut series,
                &mut alarm_parent_singles,
                &mut alarm_parent_series,
            )?;
        }

        for ((kind, _), comps) in grouped {
            let is_direct = want_non_alarm.contains(&kind);
            let has_alarm = want_alarms && comps.iter().any(|c| has_valarm_child(*c));
            classify_component_group(
                &comps,
                &reg,
                &default_tz,
                options.skip_cancelled,
                is_direct,
                has_alarm,
                &mut singles,
                &mut series,
                &mut alarm_parent_singles,
                &mut alarm_parent_series,
            )?;
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
            options,
            singles,
            singles_prefix_max_end: prefix_max,
            series,
            alarm_parent_singles,
            alarm_parent_series,
            want_alarms,
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

    /// Return all occurrences intersecting `[start_utc, end_utc)`.
    pub fn between(&self, start_utc: i64, end_utc: i64) -> Result<Vec<Occurrence>, RRuleError> {
        if end_utc <= start_utc {
            return Ok(Vec::new());
        }
        let mut out = Vec::new();
        self.collect_singles_in_window(start_utc, end_utc, &mut out);

        for s in &self.series {
            s.expand_window(
                start_utc,
                end_utc,
                self.options.skip_cancelled,
                self.options.budget,
                &mut out,
            )?;
        }

        if self.want_alarms {
            self.collect_alarms_in_window(start_utc, end_utc, &mut out)?;
        }

        out.sort_by_key(|o| (o.start_utc, o.end_utc, o.component_id.0));
        Ok(out)
    }

    /// Return all occurrences falling on calendar date `date` (`[00:00, next day 00:00)` in `default_tz`).
    pub fn at_date(&self, date: CalDate) -> Result<Vec<Occurrence>, RRuleError> {
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

    /// Count occurrences intersecting `[start_utc, end_utc)`.
    pub fn count_between(&self, start_utc: i64, end_utc: i64) -> Result<usize, RRuleError> {
        if end_utc <= start_utc {
            return Ok(0);
        }
        let mut count = 0_usize;
        let upper = self.singles.partition_point(|s| s.start_utc < end_utc);
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
            ser.expand_window(
                start_utc,
                end_utc,
                self.options.skip_cancelled,
                self.options.budget,
                &mut buf,
            )?;
            count += buf.len();
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
        let upper = self.singles.partition_point(|s| s.start_utc < end_utc);
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
        // Generous parent search padding (+/- 30 days) for negative/positive alarm triggers.
        const ALARM_PAD: i64 = 30 * 86_400;
        let p_start = start_utc.saturating_sub(ALARM_PAD);
        let p_end = end_utc.saturating_add(ALARM_PAD);

        let mut parent_occs = Vec::new();
        for p in &self.alarm_parent_singles {
            if overlaps_window(p.start_utc, p.end_utc, p_start, p_end) {
                parent_occs.push(p.clone());
            }
        }
        for ser in &self.alarm_parent_series {
            ser.expand_window(
                p_start,
                p_end,
                self.options.skip_cancelled,
                self.options.budget,
                &mut parent_occs,
            )?;
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
    heap: BinaryHeap<Reverse<HeapEntry>>,
    initialized: bool,
}

#[derive(Debug, Clone)]
struct HeapEntry {
    occ: Occurrence,
    source: HeapSource,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HeapSource {
    Single,
    Series(usize),
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

/// Self-contained streaming wrapper over one series index inside `Arc<CalendarQuery>`.
struct OwnedSeriesStream {
    series_idx: usize,
    rule_cursors: Vec<OwnedRuleCursor>,
    static_items: BinaryHeap<Reverse<HeapEntry>>,
    seen_instants: rustc_hash::FxHashSet<i64>,
}

struct OwnedRuleCursor {
    rule_idx: usize,
    next_period_idx: u64,
    periods_checked: usize,
    consecutive_empty: usize,
    emitted: u32,
    pending: std::collections::VecDeque<crate::rrule::LocalDateTime>,
    finished: bool,
    until_utc: Option<i64>,
    next_occ: Option<Occurrence>,
}

impl AfterIter {
    fn new(query: Arc<CalendarQuery>, earliest_end_utc: i64) -> Self {
        Self {
            query,
            earliest_end_utc,
            singles_idx: 0,
            series_streams: Vec::new(),
            heap: BinaryHeap::new(),
            initialized: false,
        }
    }

    fn init(&mut self) -> Result<(), RRuleError> {
        self.initialized = true;
        // Prime single occurrences
        self.push_next_single();

        // Prime each recurring series stream
        for s_idx in 0..self.query.series.len() {
            let mut stream = self.build_owned_stream(s_idx);
            if let Some(first) = self.pop_from_owned_stream(&mut stream)? {
                self.heap.push(Reverse(HeapEntry {
                    occ: first,
                    source: HeapSource::Series(s_idx),
                }));
            }
            self.series_streams.push(stream);
        }
        Ok(())
    }

    fn push_next_single(&mut self) {
        while self.singles_idx < self.query.singles.len() {
            let occ = &self.query.singles[self.singles_idx];
            self.singles_idx += 1;
            let keep = if occ.end_utc > occ.start_utc {
                occ.end_utc > self.earliest_end_utc
            } else {
                occ.start_utc >= self.earliest_end_utc
            };
            if keep {
                self.heap.push(Reverse(HeapEntry {
                    occ: occ.clone(),
                    source: HeapSource::Single,
                }));
                break;
            }
        }
    }

    fn build_owned_stream(&self, series_idx: usize) -> OwnedSeriesStream {
        let ser = &self.query.series[series_idx];
        let mut static_items = BinaryHeap::new();
        for ov in &ser.overrides {
            if self.query.options.skip_cancelled && ov.cancelled {
                continue;
            }
            let keep = if ov.end_utc > ov.start_utc {
                ov.end_utc > self.earliest_end_utc
            } else {
                ov.start_utc >= self.earliest_end_utc
            };
            if keep {
                static_items.push(Reverse(HeapEntry {
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
                    source: HeapSource::Series(series_idx),
                }));
            }
        }

        let mut rule_cursors = Vec::new();
        if let Some(master) = &ser.master {
            if !(self.query.options.skip_cancelled && master.cancelled) {
                push_master_statics(
                    master,
                    series_idx,
                    ser.this_and_future_cutoff,
                    self.earliest_end_utc,
                    &mut static_items,
                );
                for (r_idx, rule) in master.rrules.iter().enumerate() {
                    let until_utc = resolve_until_with_tz(rule, &master.tz);
                    rule_cursors.push(OwnedRuleCursor {
                        rule_idx: r_idx,
                        next_period_idx: 0,
                        periods_checked: 0,
                        consecutive_empty: 0,
                        emitted: 0,
                        pending: std::collections::VecDeque::new(),
                        finished: false,
                        until_utc,
                        next_occ: None,
                    });
                }
            }
        }

        OwnedSeriesStream {
            series_idx,
            rule_cursors,
            static_items,
            seen_instants: rustc_hash::FxHashSet::default(),
        }
    }

    fn pop_from_owned_stream(
        &self,
        stream: &mut OwnedSeriesStream,
    ) -> Result<Option<Occurrence>, RRuleError> {
        let ser = &self.query.series[stream.series_idx];
        let Some(master) = &ser.master else {
            return Ok(stream.static_items.pop().map(|r| r.0.occ));
        };

        loop {
            for cursor in &mut stream.rule_cursors {
                if cursor.next_occ.is_none() && !cursor.finished {
                    cursor.next_occ = advance_owned_cursor(
                        cursor,
                        master,
                        ser.this_and_future_cutoff,
                        self.earliest_end_utc,
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
            let best_static = stream.static_items.peek().map(|r| &r.0);

            let (occ, from_master) = match (best_rule, best_static) {
                (None, None) => return Ok(None),
                (Some((i, _, _)), None) => (stream.rule_cursors[i].next_occ.take().unwrap(), true),
                (None, Some(_)) => (stream.static_items.pop().unwrap().0.occ, false),
                (Some((i, rs, re)), Some(st)) => {
                    if (rs, re) < (st.occ.start_utc, st.occ.end_utc) {
                        (stream.rule_cursors[i].next_occ.take().unwrap(), true)
                    } else {
                        (stream.static_items.pop().unwrap().0.occ, false)
                    }
                }
            };

            if from_master && !stream.seen_instants.insert(occ.start_utc) {
                continue;
            }
            return Ok(Some(occ));
        }
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
                // Temporarily take stream out to satisfy borrow checker cleanly.
                let mut stream = std::mem::replace(
                    &mut self.series_streams[s_idx],
                    OwnedSeriesStream {
                        series_idx: s_idx,
                        rule_cursors: Vec::new(),
                        static_items: BinaryHeap::new(),
                        seen_instants: rustc_hash::FxHashSet::default(),
                    },
                );
                let next_res = self.pop_from_owned_stream(&mut stream);
                self.series_streams[s_idx] = stream;
                match next_res {
                    Ok(Some(occ)) => self.heap.push(Reverse(HeapEntry {
                        occ,
                        source: HeapSource::Series(s_idx),
                    })),
                    Ok(None) => {}
                    Err(e) => return Some(Err(e)),
                }
            }
        }
        Some(Ok(entry.occ))
    }
}

fn advance_owned_cursor(
    cursor: &mut OwnedRuleCursor,
    master: &crate::recurrence::series::MasterSeries,
    cutoff: Option<i64>,
    earliest_end_utc: i64,
    budget: IterationBudget,
) -> Result<Option<Occurrence>, RRuleError> {
    use crate::rrule::{LocalDateTime, RRuleIter};

    let rule = &master.rrules[cursor.rule_idx];
    let dtstart_ldt = LocalDateTime::new(master.start_local.date, master.start_local.time);
    let max_span_secs = match master.span {
        SpanDuration::Days(d) => i64::from(d) * 86_400 + 86_400,
        SpanDuration::Seconds(s) => s,
    };
    let ff_utc = earliest_end_utc
        .saturating_sub(max_span_secs)
        .max(master.min_start_utc);
    let ff_off = master.tz.offset_at_utc(ff_utc).unwrap_or(0);
    let ff_local = LocalDateTime::from_naive_timestamp(ff_utc + i64::from(ff_off));

    // Resume from saved cursor state on a lightweight RRuleIter by setting start fast-forward on first run.
    let mut iter = RRuleIter::new(
        rule,
        dtstart_ldt,
        cursor.until_utc.map(|u| {
            let off = master.tz.offset_at_utc(u).unwrap_or(0);
            LocalDateTime::from_naive_timestamp(u + 86_400 + i64::from(off))
        }),
        if cursor.periods_checked == 0 && cursor.emitted == 0 && cursor.pending.is_empty() {
            Some(ff_local)
        } else {
            None
        },
        budget,
    );

    if cursor.periods_checked > 0 || cursor.emitted > 0 || !cursor.pending.is_empty() {
        iter.restore_state(
            cursor.next_period_idx,
            cursor.periods_checked,
            cursor.consecutive_empty,
            cursor.emitted,
            std::mem::take(&mut cursor.pending),
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
        let s_utc = resolve_local_with_tz(&cand_local, &master.tz);
        if cursor.until_utc.is_some_and(|u| s_utc > u) {
            cursor.finished = true;
            break;
        }
        if cutoff.is_some_and(|c| s_utc >= c) || master.exdates.matches(s_utc, ldt.date, ldt.time) {
            continue;
        }
        let (e_utc, e_local) = compute_end_local(&cand_local, s_utc, master.span, &master.tz);
        let keep = if e_utc > s_utc {
            e_utc > earliest_end_utc
        } else {
            s_utc >= earliest_end_utc
        };
        if !keep {
            continue;
        }
        found = Some(Occurrence {
            component_id: master.component_id,
            kind: master.kind.clone(),
            start_utc: s_utc,
            end_utc: e_utc,
            start_local: cand_local,
            end_local: e_local,
            is_all_day: master.is_all_day,
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
    if fin || found.is_none() {
        cursor.finished = true;
    }
    Ok(found)
}

#[allow(clippy::too_many_arguments)]
fn classify_component_group(
    comps: &[ComponentRef<'_>],
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
    skip_cancelled: bool,
    is_direct: bool,
    has_alarm: bool,
    singles: &mut Vec<Occurrence>,
    series: &mut Vec<CompiledSeries>,
    alarm_parent_singles: &mut Vec<Occurrence>,
    alarm_parent_series: &mut Vec<CompiledSeries>,
) -> Result<(), RRuleError> {
    let mut master: Option<ComponentRef<'_>> = None;
    let mut overrides: Vec<ComponentRef<'_>> = Vec::new();

    for &c in comps {
        if c.property(&PropertyName::RecurrenceId).is_some() {
            overrides.push(c);
        } else if master.is_none() {
            master = Some(c);
        }
    }

    let is_simple_single = overrides.is_empty()
        && master.is_some_and(|m| {
            m.property(&PropertyName::RRule).is_none() && m.property(&PropertyName::RDate).is_none()
        });

    if is_simple_single {
        let mc = master.unwrap();
        if let Some(timing) = extract_component_timing(mc, reg, default_tz) {
            if skip_cancelled && timing.cancelled {
                return Ok(());
            }
            let (end_utc, end_local) = compute_end_local(
                &timing.start_local,
                timing.start_utc,
                timing.span,
                &timing.tz,
            );
            let occ = Occurrence {
                component_id: mc.id(),
                kind: mc.kind().clone(),
                start_utc: timing.start_utc,
                end_utc,
                start_local: timing.start_local,
                end_local,
                is_all_day: timing.is_all_day,
                recurrence_id_utc: None,
                parent_id: None,
            };
            if is_direct {
                singles.push(occ.clone());
            }
            if has_alarm {
                alarm_parent_singles.push(occ);
            }
        }
        return Ok(());
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

fn push_master_statics(
    master: &crate::recurrence::series::MasterSeries,
    series_idx: usize,
    cutoff: Option<i64>,
    earliest_end_utc: i64,
    static_items: &mut BinaryHeap<Reverse<HeapEntry>>,
) {
    let mut try_push = |dt: &CalDateTime| {
        let s_utc = resolve_local_with_tz(dt, &master.tz);
        let (e_utc, e_loc) = compute_end_local(dt, s_utc, master.span, &master.tz);
        let keep = if e_utc > s_utc {
            e_utc > earliest_end_utc
        } else {
            s_utc >= earliest_end_utc
        };
        if keep
            && cutoff.is_none_or(|c| s_utc < c)
            && !master.exdates.matches(s_utc, dt.date, dt.time)
        {
            static_items.push(Reverse(HeapEntry {
                occ: Occurrence {
                    component_id: master.component_id,
                    kind: master.kind.clone(),
                    start_utc: s_utc,
                    end_utc: e_utc,
                    start_local: dt.clone(),
                    end_local: e_loc,
                    is_all_day: master.is_all_day,
                    recurrence_id_utc: Some(s_utc),
                    parent_id: None,
                },
                source: HeapSource::Series(series_idx),
            }));
        }
    };

    if master.rrules.is_empty() {
        try_push(&master.start_local);
    }
    for rdt in &master.rdates {
        try_push(rdt);
    }
}

fn has_valarm_child(comp: ComponentRef<'_>) -> bool {
    comp.children().any(|c| c.kind() == &ComponentKind::VAlarm)
}

fn resolve_local_with_tz(dt: &CalDateTime, tz: &CalendarTimeZone) -> i64 {
    if matches!(dt.tz, icaloxide_core::datetime::TimeZoneRef::Utc) {
        return dt.to_naive_timestamp();
    }
    match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
        Ok(r) => r.utc_timestamp,
        Err(_) => dt.to_naive_timestamp(),
    }
}

fn resolve_until_with_tz(rule: &crate::rrule::RRule, tz: &CalendarTimeZone) -> Option<i64> {
    let u = rule.until.as_ref()?;
    Some(match u {
        crate::rrule::UntilValue::Date(d) => {
            let end_time = CalTime {
                hour: 23,
                minute: 59,
                second: 59,
            };
            match tz.resolve_local(*d, end_time, DstDisambiguation::Compatible) {
                Ok(r) => r.utc_timestamp,
                Err(_) => i64::from(d.to_days_since_epoch()) * 86_400 + 86_399,
            }
        }
        crate::rrule::UntilValue::DateTime(dt) => {
            if matches!(dt.tz, icaloxide_core::datetime::TimeZoneRef::Utc) {
                dt.to_naive_timestamp()
            } else {
                match tz.resolve_local(dt.date, dt.time, DstDisambiguation::Compatible) {
                    Ok(r) => r.utc_timestamp,
                    Err(_) => dt.to_naive_timestamp(),
                }
            }
        }
    })
}
