use icaloxide_core::datetime::{days_in_month, is_leap_year, CalDate};
use smallvec::SmallVec;

use crate::rrule::{ByWeekday, Weekday};

/// Total calendar days in `year` (`365` or `366`).
#[inline]
pub const fn days_in_year(year: i16) -> u16 {
    if is_leap_year(year) {
        366
    } else {
        365
    }
}

/// Epoch day of the start of `date`'s week assuming weeks begin on `wkst`.
#[inline]
pub const fn week_start_on_or_before(date: CalDate, wkst: Weekday) -> i32 {
    let wd = date.weekday() as i32;
    let ws = wkst.to_u8() as i32;
    let offset = (wd - ws).rem_euclid(7);
    date.to_days_since_epoch() - offset
}

/// RFC 5545 week 1 start epoch day for `year` and `wkst`.
///
/// Week 1 is the first week starting on `wkst` that contains at least 4 days in `year`.
#[inline]
pub fn week1_start_epoch(year: i16, wkst: Weekday) -> i32 {
    let jan1 = CalDate {
        year,
        month: 1,
        day: 1,
    };
    let jan1_epoch = jan1.to_days_since_epoch();
    let jan1_wd = i32::from(jan1.weekday());
    let ws = i32::from(wkst.to_u8());
    // Number of days from Jan 1 until the next `wkst` (0..=6):
    // Wait: days in the partial week starting Jan 1 before the next `wkst`:
    let days_in_first_partial = (ws - jan1_wd).rem_euclid(7);
    let actual_first = if days_in_first_partial == 0 {
        7
    } else {
        days_in_first_partial
    };
    if actual_first >= 4 {
        // The week containing Jan 1 already has >= 4 days in `year`.
        week_start_on_or_before(jan1, wkst)
    } else {
        // Week 1 starts on the first `wkst` strictly after Jan 1.
        jan1_epoch + actual_first
    }
}

/// Number of RFC 5545 weeks in `year` (`52` or `53`).
#[inline]
pub fn weeks_in_year(year: i16, wkst: Weekday) -> u8 {
    let w1 = week1_start_epoch(year, wkst);
    let w1_next = week1_start_epoch(year + 1, wkst);
    ((w1_next - w1) / 7) as u8
}

/// Resolve an RFC 5545 `BYWEEKNO` value (`1..=53` or `-53..=-1`) in `year` into its 7 dates
/// (which may spill into late December of `year - 1` or early January of `year + 1`).
pub fn week_days_in_year(year: i16, week_no: i8, wkst: Weekday) -> SmallVec<[CalDate; 7]> {
    let total_weeks = i16::from(weeks_in_year(year, wkst));
    let target = if week_no > 0 {
        i16::from(week_no)
    } else {
        total_weeks + 1 + i16::from(week_no)
    };
    let mut out = SmallVec::new();
    if target < 1 || target > total_weeks {
        return out;
    }
    let start = week1_start_epoch(year, wkst) + i32::from(target - 1) * 7;
    for d in 0..7 {
        out.push(CalDate::from_days_since_epoch(start + d));
    }
    out
}

/// Resolve RFC 5545 `BYWEEKNO` values within calendar `year` (`Jan 1..Dec 31`),
/// including late-December days belonging to `(year + 1)` week 1 and early-January
/// days belonging to `(year - 1)`'s final week (matching `dateutil.rrule` even for `INTERVAL > 1`).
pub fn byweekno_dates_in_year(year: i16, by_week_no: &[i8], wkst: Weekday) -> Vec<CalDate> {
    let mut out = Vec::new();
    for &wn in by_week_no {
        for d in week_days_in_year(year, wn, wkst) {
            if d.year == year {
                out.push(d);
            }
        }
    }
    let next_weeks = weeks_in_year(year + 1, wkst) as i8;
    if by_week_no.contains(&1) || by_week_no.contains(&(-next_weeks)) {
        for d in week_days_in_year(year + 1, 1, wkst) {
            if d.year == year {
                out.push(d);
            }
        }
    }
    let jan1_epoch = CalDate {
        year,
        month: 1,
        day: 1,
    }
    .to_days_since_epoch();
    if week1_start_epoch(year, wkst) > jan1_epoch {
        let prev_weeks = weeks_in_year(year - 1, wkst) as i8;
        if by_week_no.contains(&-1) || by_week_no.contains(&prev_weeks) {
            for d in week_days_in_year(year - 1, prev_weeks, wkst) {
                if d.year == year {
                    out.push(d);
                }
            }
        }
    }
    out
}

/// Resolve a signed `BYYEARDAY` (`1..=366` or `-366..=-1`) in `year`.
pub fn year_day_to_date(year: i16, yd: i16) -> Option<CalDate> {
    let total = i32::from(days_in_year(year));
    let idx = if yd > 0 {
        i32::from(yd)
    } else {
        total + 1 + i32::from(yd)
    };
    if idx < 1 || idx > total {
        return None;
    }
    let jan1 = CalDate {
        year,
        month: 1,
        day: 1,
    };
    Some(jan1.add_days(idx - 1))
}

/// Resolve a signed `BYMONTHDAY` (`1..=31` or `-31..=-1`) in `(year, month)`.
pub fn month_day_to_date(year: i16, month: u8, md: i8) -> Option<CalDate> {
    let max_d = i16::from(days_in_month(year, month));
    if max_d == 0 {
        return None;
    }
    let day = if md > 0 {
        i16::from(md)
    } else {
        max_d + 1 + i16::from(md)
    };
    if day < 1 || day > max_d {
        return None;
    }
    CalDate::new(year, month, day as u8).ok()
}

/// Resolve a [`ByWeekday`] inside `(year, month)`.
pub fn weekdays_in_month(year: i16, month: u8, bw: ByWeekday) -> SmallVec<[CalDate; 5]> {
    let max_d = days_in_month(year, month);
    let target_wd = bw.weekday.to_u8();
    let mut matches: SmallVec<[CalDate; 5]> = SmallVec::new();
    for day in 1..=max_d {
        if let Ok(d) = CalDate::new(year, month, day) {
            if d.weekday() == target_wd {
                matches.push(d);
            }
        }
    }
    select_ordinal(matches, bw.ordinal)
}

/// Resolve a [`ByWeekday`] across the whole `year` (for `FREQ=YEARLY` without `BYMONTH`).
pub fn weekdays_in_year(year: i16, bw: ByWeekday) -> Vec<CalDate> {
    let total = i32::from(days_in_year(year));
    let jan1 = CalDate {
        year,
        month: 1,
        day: 1,
    };
    let jan1_epoch = jan1.to_days_since_epoch();
    let target_wd = i32::from(bw.weekday.to_u8());
    let jan1_wd = i32::from(jan1.weekday());
    let mut offset = (target_wd - jan1_wd).rem_euclid(7);

    let mut matches = Vec::with_capacity(54);
    while offset < total {
        matches.push(CalDate::from_days_since_epoch(jan1_epoch + offset));
        offset += 7;
    }
    select_ordinal_vec(matches, bw.ordinal)
}

fn select_ordinal(matches: SmallVec<[CalDate; 5]>, ord: Option<i16>) -> SmallVec<[CalDate; 5]> {
    let Some(n) = ord else {
        return matches;
    };
    let mut out = SmallVec::new();
    if n > 0 {
        if let Some(&d) = matches.get((n - 1) as usize) {
            out.push(d);
        }
    } else if n < 0 {
        let rev = (-n - 1) as usize;
        if rev < matches.len() {
            out.push(matches[matches.len() - 1 - rev]);
        }
    }
    out
}

fn select_ordinal_vec(matches: Vec<CalDate>, ord: Option<i16>) -> Vec<CalDate> {
    let Some(n) = ord else {
        return matches;
    };
    match n.cmp(&0) {
        std::cmp::Ordering::Greater => matches.get((n - 1) as usize).copied().into_iter().collect(),
        std::cmp::Ordering::Less => {
            let rev = (-n - 1) as usize;
            if rev < matches.len() {
                vec![matches[matches.len() - 1 - rev]]
            } else {
                Vec::new()
            }
        }
        std::cmp::Ordering::Equal => Vec::new(),
    }
}
