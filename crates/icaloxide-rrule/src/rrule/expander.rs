use std::collections::VecDeque;

use icaloxide_core::datetime::{days_in_month, CalDate, CalTime};
use smallvec::SmallVec;

use crate::error::{IterationBudget, RRuleError};
use crate::rrule::calendar_math::{
    byweekno_dates_in_year, month_day_to_date, week_start_on_or_before, weekdays_in_month,
    weekdays_in_year, year_day_to_date,
};
use crate::rrule::{Frequency, RRule};

const MAX_SUPPORTED_YEAR: i16 = 9999;

/// Lightweight sortable local date-time pair used during `RRULE` expansion.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LocalDateTime {
    pub date: CalDate,
    pub time: CalTime,
}

impl LocalDateTime {
    #[inline]
    pub const fn new(date: CalDate, time: CalTime) -> Self {
        Self { date, time }
    }

    #[inline]
    pub const fn to_naive_timestamp(self) -> i64 {
        (self.date.to_days_since_epoch() as i64) * 86_400
            + (self.time.seconds_since_midnight() as i64)
    }

    #[inline]
    pub const fn from_naive_timestamp(ts: i64) -> Self {
        let days = ts.div_euclid(86_400) as i32;
        let rem = ts.rem_euclid(86_400) as u32;
        Self {
            date: CalDate::from_days_since_epoch(days),
            time: CalTime {
                hour: (rem / 3600) as u8,
                minute: ((rem % 3600) / 60) as u8,
                second: (rem % 60) as u8,
            },
        }
    }
}

/// Expand `rule` starting at `dtstart` into a chronological `Vec<LocalDateTime>`.
pub fn expand_rrule(
    rule: &RRule,
    dtstart: LocalDateTime,
    until_limit: Option<LocalDateTime>,
    window_start: Option<LocalDateTime>,
    window_end: Option<LocalDateTime>,
    budget: IterationBudget,
) -> Result<Vec<LocalDateTime>, RRuleError> {
    let mut iter = RRuleIter::new(rule, dtstart, until_limit, window_start, budget);
    let mut out = Vec::new();
    for item in &mut iter {
        let dt = item?;
        if window_end.is_some_and(|end| dt >= end) {
            break;
        }
        if window_start.is_some_and(|start| dt < start) {
            continue;
        }
        out.push(dt);
    }
    Ok(out)
}

/// Streaming, O(1)-fast-forward RFC 5545 `RRULE` iterator.
#[derive(Debug, Clone)]
pub struct RRuleIter<'a> {
    rule: &'a RRule,
    dtstart: LocalDateTime,
    until_limit: Option<LocalDateTime>,
    budget: IterationBudget,
    period_idx: u64,
    periods_checked: usize,
    consecutive_empty: usize,
    emitted: u32,
    pending: VecDeque<LocalDateTime>,
    finished: bool,
}

impl<'a> RRuleIter<'a> {
    /// Construct a lazy `RRULE` iterator.
    ///
    /// When `rule.count.is_none()` and `fast_forward_to` is strictly after `dtstart`,
    /// jumps directly to one period before `fast_forward_to` in $O(1)$ time.
    pub fn new(
        rule: &'a RRule,
        dtstart: LocalDateTime,
        until_limit: Option<LocalDateTime>,
        fast_forward_to: Option<LocalDateTime>,
        budget: IterationBudget,
    ) -> Self {
        let mut period_idx = 0_u64;
        if rule.count.is_none() {
            if let Some(target) = fast_forward_to {
                if target > dtstart {
                    period_idx = compute_fast_forward_period(rule, dtstart, target);
                }
            }
        }
        Self {
            rule,
            dtstart,
            until_limit,
            budget,
            period_idx,
            periods_checked: 0,
            consecutive_empty: 0,
            emitted: 0,
            pending: VecDeque::new(),
            finished: false,
        }
    }

    pub(crate) fn save_state(&mut self) -> (u64, usize, usize, u32, VecDeque<LocalDateTime>, bool) {
        (
            self.period_idx,
            self.periods_checked,
            self.consecutive_empty,
            self.emitted,
            std::mem::take(&mut self.pending),
            self.finished,
        )
    }

    pub(crate) fn restore_state(
        &mut self,
        period_idx: u64,
        periods_checked: usize,
        consecutive_empty: usize,
        emitted: u32,
        pending: VecDeque<LocalDateTime>,
        finished: bool,
    ) {
        self.period_idx = period_idx;
        self.periods_checked = periods_checked;
        self.consecutive_empty = consecutive_empty;
        self.emitted = emitted;
        self.pending = pending;
        self.finished = finished;
    }

    fn refill(&mut self) -> Result<bool, RRuleError> {
        while self.pending.is_empty() {
            if self.finished {
                return Ok(false);
            }
            if self.periods_checked >= self.budget.max_periods
                || self.consecutive_empty >= self.budget.max_empty_periods
            {
                self.finished = true;
                return Err(RRuleError::BudgetExceeded);
            }

            let Some(mut candidates) =
                generate_period_candidates(self.rule, self.dtstart, self.period_idx)
            else {
                self.finished = true;
                return Ok(false);
            };
            self.period_idx += 1;
            self.periods_checked += 1;

            if candidates.is_empty() {
                self.consecutive_empty += 1;
                continue;
            }

            candidates.sort_unstable();
            candidates.dedup();

            if !self.rule.by_set_pos.is_empty() {
                candidates = apply_bysetpos(&candidates, &self.rule.by_set_pos);
            }

            let mut pushed_any = false;
            for dt in candidates {
                if dt < self.dtstart {
                    continue;
                }
                if self.until_limit.is_some_and(|u| dt > u) {
                    self.finished = true;
                    return Ok(!self.pending.is_empty());
                }
                self.pending.push_back(dt);
                pushed_any = true;
            }

            if pushed_any {
                self.consecutive_empty = 0;
            } else {
                self.consecutive_empty += 1;
            }
        }
        Ok(true)
    }
}

impl Iterator for RRuleIter<'_> {
    type Item = Result<LocalDateTime, RRuleError>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.rule.count.is_some_and(|c| self.emitted >= c) {
            self.finished = true;
            return None;
        }
        if self.pending.is_empty() {
            if self.finished {
                return None;
            }
            match self.refill() {
                Ok(true) => {}
                Ok(false) => return None,
                Err(e) => return Some(Err(e)),
            }
        }
        let next_dt = self.pending.pop_front()?;
        self.emitted += 1;
        if self.rule.count.is_some_and(|c| self.emitted >= c) {
            self.finished = true;
        }
        Some(Ok(next_dt))
    }
}

fn compute_fast_forward_period(rule: &RRule, dtstart: LocalDateTime, target: LocalDateTime) -> u64 {
    let iv = i64::from(rule.interval.max(1));
    let k = match rule.freq {
        Frequency::Yearly => {
            let dy = i64::from(target.date.year) - i64::from(dtstart.date.year);
            dy / iv - 1
        }
        Frequency::Monthly => {
            let m_start = i64::from(dtstart.date.year) * 12 + i64::from(dtstart.date.month) - 1;
            let m_target = i64::from(target.date.year) * 12 + i64::from(target.date.month) - 1;
            (m_target - m_start) / iv - 1
        }
        Frequency::Weekly => {
            let dd = i64::from(target.date.to_days_since_epoch())
                - i64::from(dtstart.date.to_days_since_epoch());
            dd / (7 * iv) - 1
        }
        Frequency::Daily => {
            let dd = i64::from(target.date.to_days_since_epoch())
                - i64::from(dtstart.date.to_days_since_epoch());
            dd / iv - 1
        }
        Frequency::Hourly => {
            let ds = target.to_naive_timestamp() - dtstart.to_naive_timestamp();
            ds / (3600 * iv) - 1
        }
        Frequency::Minutely => {
            let ds = target.to_naive_timestamp() - dtstart.to_naive_timestamp();
            ds / (60 * iv) - 1
        }
        Frequency::Secondly => {
            let ds = target.to_naive_timestamp() - dtstart.to_naive_timestamp();
            ds / iv - 1
        }
    };
    k.max(0) as u64
}

fn apply_bysetpos(sorted: &[LocalDateTime], positions: &[i16]) -> Vec<LocalDateTime> {
    let len = sorted.len() as i32;
    let mut out = Vec::with_capacity(positions.len());
    for &pos in positions {
        let idx = if pos > 0 {
            i32::from(pos) - 1
        } else {
            len + i32::from(pos)
        };
        if idx >= 0 && idx < len {
            out.push(sorted[idx as usize]);
        }
    }
    out.sort_unstable();
    out.dedup();
    out
}

fn generate_period_candidates(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    match rule.freq {
        Frequency::Yearly => generate_yearly(rule, dtstart, period_idx),
        Frequency::Monthly => generate_monthly(rule, dtstart, period_idx),
        Frequency::Weekly => generate_weekly(rule, dtstart, period_idx),
        Frequency::Daily => generate_daily(rule, dtstart, period_idx),
        Frequency::Hourly | Frequency::Minutely | Frequency::Secondly => {
            generate_subdaily(rule, dtstart, period_idx)
        }
    }
}

fn generate_yearly(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    let year_i64 = i64::from(dtstart.date.year) + (period_idx as i64) * i64::from(rule.interval);
    if year_i64 > i64::from(MAX_SUPPORTED_YEAR) {
        return None;
    }
    let year = year_i64 as i16;

    let mut dates = if !rule.by_week_no.is_empty() {
        let mut d = byweekno_dates_in_year(year, &rule.by_week_no, rule.wkst);
        if !rule.by_month.is_empty() {
            d.retain(|date| rule.by_month.contains(&date.month));
        }
        if !rule.by_month_day.is_empty() {
            d.retain(|date| matches_month_days(*date, &rule.by_month_day));
        }
        if !rule.by_day.is_empty() {
            d.retain(|date| {
                rule.by_day
                    .iter()
                    .any(|bw| bw.ordinal.is_none() && bw.weekday.to_u8() == date.weekday())
            });
        }
        d
    } else if !rule.by_year_day.is_empty() {
        let mut d: Vec<CalDate> = rule
            .by_year_day
            .iter()
            .filter_map(|&yd| year_day_to_date(year, yd))
            .collect();
        filter_by_month_monthday_day(rule, &mut d);
        d
    } else if !rule.by_month.is_empty() {
        let mut d = Vec::new();
        for &month in &rule.by_month {
            d.extend(resolve_month_dates(year, month, dtstart.date.day, rule));
        }
        d
    } else if !rule.by_day.is_empty() {
        let mut d = Vec::new();
        for &bw in &rule.by_day {
            d.extend(weekdays_in_year(year, bw));
        }
        if !rule.by_month_day.is_empty() {
            d.retain(|date| matches_month_days(*date, &rule.by_month_day));
        }
        d
    } else if !rule.by_month_day.is_empty() {
        let mut d = Vec::new();
        for month in 1..=12_u8 {
            d.extend(resolve_month_dates(year, month, dtstart.date.day, rule));
        }
        d
    } else {
        CalDate::new(year, dtstart.date.month, dtstart.date.day)
            .ok()
            .into_iter()
            .collect()
    };

    dates.sort_unstable();
    dates.dedup();
    Some(expand_times_for_dates(rule, dtstart.time, &dates))
}

fn generate_monthly(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    let base_months = i64::from(dtstart.date.year) * 12 + i64::from(dtstart.date.month) - 1;
    let total_months = base_months + (period_idx as i64) * i64::from(rule.interval);
    let year_i64 = total_months.div_euclid(12);
    if year_i64 > i64::from(MAX_SUPPORTED_YEAR) {
        return None;
    }
    let year = year_i64 as i16;
    let month = (total_months.rem_euclid(12) + 1) as u8;

    if !rule.by_month.is_empty() && !rule.by_month.contains(&month) {
        return Some(Vec::new());
    }

    let mut dates = resolve_month_dates(year, month, dtstart.date.day, rule);
    dates.sort_unstable();
    dates.dedup();
    Some(expand_times_for_dates(rule, dtstart.time, &dates))
}

fn generate_weekly(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    let base_week = week_start_on_or_before(dtstart.date, rule.wkst);
    let delta_days = (period_idx as i64) * 7 * i64::from(rule.interval);
    let week_start = i64::from(base_week) + delta_days;
    if week_start
        > i64::from(
            CalDate::new(MAX_SUPPORTED_YEAR, 12, 31)
                .ok()?
                .to_days_since_epoch(),
        )
    {
        return None;
    }

    let mut dates = Vec::with_capacity(7);
    for offset in 0..7_i32 {
        let date = CalDate::from_days_since_epoch(week_start as i32 + offset);
        let wd_matches = if rule.by_day.is_empty() {
            if rule.by_month_day.is_empty() && rule.by_year_day.is_empty() {
                date.weekday() == dtstart.date.weekday()
            } else {
                true
            }
        } else {
            rule.by_day
                .iter()
                .any(|bw| bw.weekday.to_u8() == date.weekday())
        };
        if !wd_matches {
            continue;
        }
        if !rule.by_month.is_empty() && !rule.by_month.contains(&date.month) {
            continue;
        }
        if !rule.by_month_day.is_empty() && !matches_month_days(date, &rule.by_month_day) {
            continue;
        }
        if !rule.by_year_day.is_empty() && !matches_year_days(date, &rule.by_year_day) {
            continue;
        }
        dates.push(date);
    }

    Some(expand_times_for_dates(rule, dtstart.time, &dates))
}

fn generate_daily(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    let delta_days = (period_idx as i64) * i64::from(rule.interval);
    let epoch = i64::from(dtstart.date.to_days_since_epoch()) + delta_days;
    if epoch
        > i64::from(
            CalDate::new(MAX_SUPPORTED_YEAR, 12, 31)
                .ok()?
                .to_days_since_epoch(),
        )
    {
        return None;
    }
    let date = CalDate::from_days_since_epoch(epoch as i32);

    if !rule.by_month.is_empty() && !rule.by_month.contains(&date.month) {
        return Some(Vec::new());
    }
    if !rule.by_month_day.is_empty() && !matches_month_days(date, &rule.by_month_day) {
        return Some(Vec::new());
    }
    if !rule.by_year_day.is_empty() && !matches_year_days(date, &rule.by_year_day) {
        return Some(Vec::new());
    }
    if !rule.by_day.is_empty()
        && !rule
            .by_day
            .iter()
            .any(|bw| bw.weekday.to_u8() == date.weekday())
    {
        return Some(Vec::new());
    }

    Some(expand_times_for_dates(rule, dtstart.time, &[date]))
}

fn generate_subdaily(
    rule: &RRule,
    dtstart: LocalDateTime,
    period_idx: u64,
) -> Option<Vec<LocalDateTime>> {
    let step_secs = match rule.freq {
        Frequency::Hourly => 3600_i64 * i64::from(rule.interval),
        Frequency::Minutely => 60_i64 * i64::from(rule.interval),
        Frequency::Secondly => i64::from(rule.interval),
        _ => unreachable!(),
    };
    let base_ts = dtstart.to_naive_timestamp();
    let cur_ts = base_ts + (period_idx as i64) * step_secs;
    let cur = LocalDateTime::from_naive_timestamp(cur_ts);
    if cur.date.year > MAX_SUPPORTED_YEAR {
        return None;
    }

    if !rule.by_month.is_empty() && !rule.by_month.contains(&cur.date.month) {
        return Some(Vec::new());
    }
    if !rule.by_month_day.is_empty() && !matches_month_days(cur.date, &rule.by_month_day) {
        return Some(Vec::new());
    }
    if !rule.by_year_day.is_empty() && !matches_year_days(cur.date, &rule.by_year_day) {
        return Some(Vec::new());
    }
    if !rule.by_day.is_empty()
        && !rule
            .by_day
            .iter()
            .any(|bw| bw.weekday.to_u8() == cur.date.weekday())
    {
        return Some(Vec::new());
    }
    if !rule.by_hour.is_empty() && !rule.by_hour.contains(&cur.time.hour) {
        return Some(Vec::new());
    }

    match rule.freq {
        Frequency::Hourly => {
            let mins: SmallVec<[u8; 1]> = if rule.by_minute.is_empty() {
                SmallVec::from_elem(dtstart.time.minute, 1)
            } else {
                rule.by_minute.iter().copied().collect()
            };
            let secs: SmallVec<[u8; 1]> = if rule.by_second.is_empty() {
                SmallVec::from_elem(dtstart.time.second, 1)
            } else {
                rule.by_second.iter().copied().collect()
            };
            let mut out = Vec::with_capacity(mins.len() * secs.len());
            for &m in &mins {
                for &s in &secs {
                    out.push(LocalDateTime::new(
                        cur.date,
                        CalTime {
                            hour: cur.time.hour,
                            minute: m,
                            second: s,
                        },
                    ));
                }
            }
            Some(out)
        }
        Frequency::Minutely => {
            if !rule.by_minute.is_empty() && !rule.by_minute.contains(&cur.time.minute) {
                return Some(Vec::new());
            }
            let secs: SmallVec<[u8; 1]> = if rule.by_second.is_empty() {
                SmallVec::from_elem(dtstart.time.second, 1)
            } else {
                rule.by_second.iter().copied().collect()
            };
            let mut out = Vec::with_capacity(secs.len());
            for &s in &secs {
                out.push(LocalDateTime::new(
                    cur.date,
                    CalTime {
                        hour: cur.time.hour,
                        minute: cur.time.minute,
                        second: s,
                    },
                ));
            }
            Some(out)
        }
        Frequency::Secondly => {
            if !rule.by_minute.is_empty() && !rule.by_minute.contains(&cur.time.minute) {
                return Some(Vec::new());
            }
            if !rule.by_second.is_empty() && !rule.by_second.contains(&cur.time.second) {
                return Some(Vec::new());
            }
            Some(vec![cur])
        }
        _ => unreachable!(),
    }
}

fn resolve_month_dates(year: i16, month: u8, default_day: u8, rule: &RRule) -> Vec<CalDate> {
    if !rule.by_day.is_empty() {
        let mut dates = Vec::new();
        for &bw in &rule.by_day {
            dates.extend(weekdays_in_month(year, month, bw));
        }
        if !rule.by_month_day.is_empty() {
            dates.retain(|d| matches_month_days(*d, &rule.by_month_day));
        }
        dates
    } else if !rule.by_month_day.is_empty() {
        rule.by_month_day
            .iter()
            .filter_map(|&md| month_day_to_date(year, month, md))
            .collect()
    } else {
        CalDate::new(year, month, default_day)
            .ok()
            .into_iter()
            .collect()
    }
}

fn filter_by_month_monthday_day(rule: &RRule, dates: &mut Vec<CalDate>) {
    if !rule.by_month.is_empty() {
        dates.retain(|d| rule.by_month.contains(&d.month));
    }
    if !rule.by_month_day.is_empty() {
        dates.retain(|d| matches_month_days(*d, &rule.by_month_day));
    }
    if !rule.by_day.is_empty() {
        dates.retain(|d| {
            rule.by_day
                .iter()
                .any(|bw| bw.ordinal.is_none() && bw.weekday.to_u8() == d.weekday())
        });
    }
}

fn matches_month_days(date: CalDate, by_month_day: &[i8]) -> bool {
    let max_d = i16::from(days_in_month(date.year, date.month));
    by_month_day.iter().any(|&md| {
        let target = if md > 0 {
            i16::from(md)
        } else {
            max_d + 1 + i16::from(md)
        };
        i16::from(date.day) == target
    })
}

fn matches_year_days(date: CalDate, by_year_day: &[i16]) -> bool {
    by_year_day
        .iter()
        .any(|&yd| year_day_to_date(date.year, yd) == Some(date))
}

fn expand_times_for_dates(
    rule: &RRule,
    default_time: CalTime,
    dates: &[CalDate],
) -> Vec<LocalDateTime> {
    if dates.is_empty() {
        return Vec::new();
    }
    let hours: SmallVec<[u8; 1]> = if rule.by_hour.is_empty() {
        SmallVec::from_elem(default_time.hour, 1)
    } else {
        rule.by_hour.iter().copied().collect()
    };
    let mins: SmallVec<[u8; 1]> = if rule.by_minute.is_empty() {
        SmallVec::from_elem(default_time.minute, 1)
    } else {
        rule.by_minute.iter().copied().collect()
    };
    let secs: SmallVec<[u8; 1]> = if rule.by_second.is_empty() {
        SmallVec::from_elem(default_time.second, 1)
    } else {
        rule.by_second.iter().copied().collect()
    };

    let mut out = Vec::with_capacity(dates.len() * hours.len() * mins.len() * secs.len());
    for &date in dates {
        for &hour in &hours {
            for &minute in &mins {
                for &second in &secs {
                    out.push(LocalDateTime::new(
                        date,
                        CalTime {
                            hour,
                            minute,
                            second,
                        },
                    ));
                }
            }
        }
    }
    out
}
