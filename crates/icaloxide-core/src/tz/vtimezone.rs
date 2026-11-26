use compact_str::CompactString;

use crate::datetime::{days_in_month, CalDate, CalDateTime, CalTime, TimeZoneRef};
use crate::model::{ComponentKind, ComponentRef, PropertyName, PropertyValue};
use crate::tz::{DstDisambiguation, ResolvedInstant, TzError};

/// Maximum year used when expanding unbounded `VTIMEZONE` RRULE transitions.
const MAX_VTIMEZONE_YEAR: i16 = 2100;

/// Single compiled UTC transition inside a [`CompiledVTimezone`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TzTransition {
    /// UTC timestamp (seconds since Unix epoch) at which this observance takes effect.
    pub utc_timestamp: i64,
    /// Active UTC offset immediately before this transition (seconds).
    pub offset_from: i32,
    /// Active UTC offset starting at this transition (seconds).
    pub offset_to: i32,
    /// `true` if `DAYLIGHT`, `false` if `STANDARD`.
    pub is_dst: bool,
    /// Optional `TZNAME` (e.g. `"EST"`, `"EDT"`).
    pub tzname: Option<CompactString>,
}

/// Precompiled transition table for an RFC 5545 `VTIMEZONE` component.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledVTimezone {
    pub tzid: CompactString,
    pub initial_offset: i32,
    pub initial_is_dst: bool,
    pub initial_tzname: Option<CompactString>,
    pub transitions: Vec<TzTransition>,
}

impl CompiledVTimezone {
    /// Compile a `VTIMEZONE` [`ComponentRef`] (`STANDARD`/`DAYLIGHT` + `RRULE`/`RDATE`)
    /// into a sorted transition table for $O(\log n)$ lookups.
    pub fn compile(vtz: ComponentRef<'_>) -> Result<Self, TzError> {
        let Some(tzid) = vtz.property_text(&PropertyName::TzId) else {
            return Err(TzError::InvalidVTimezone("missing TZID property"));
        };
        let tzid = CompactString::from(tzid);

        let mut transitions = Vec::new();
        for child in vtz.children() {
            let is_dst = match child.kind() {
                ComponentKind::Standard => false,
                ComponentKind::Daylight => true,
                _ => continue,
            };
            compile_observance(child, is_dst, &mut transitions)?;
        }

        if transitions.is_empty() {
            return Err(TzError::InvalidVTimezone(
                "VTIMEZONE has no valid STANDARD or DAYLIGHT observances",
            ));
        }

        transitions.sort_by_key(|t| t.utc_timestamp);
        // Keep the latest entry if duplicate transition timestamps occur.
        transitions.dedup_by(|later, earlier| {
            if later.utc_timestamp == earlier.utc_timestamp {
                *earlier = later.clone();
                true
            } else {
                false
            }
        });

        let first = &transitions[0];
        Ok(Self {
            tzid,
            initial_offset: first.offset_from,
            initial_is_dst: !first.is_dst,
            initial_tzname: None,
            transitions,
        })
    }

    /// Return `(offset_seconds, is_dst, tzname)` active at a given UTC timestamp.
    pub fn offset_at_utc(&self, utc_timestamp: i64) -> (i32, bool, Option<&str>) {
        let idx = self
            .transitions
            .partition_point(|t| t.utc_timestamp <= utc_timestamp);
        if idx == 0 {
            (
                self.initial_offset,
                self.initial_is_dst,
                self.initial_tzname.as_deref(),
            )
        } else {
            let t = &self.transitions[idx - 1];
            (t.offset_to, t.is_dst, t.tzname.as_deref())
        }
    }

    /// Resolve a local wall-clock `(CalDate, CalTime)` to a [`ResolvedInstant`],
    /// handling DST spring-forward gaps and fall-back folds.
    pub fn resolve_local(
        &self,
        date: CalDate,
        time: CalTime,
        disamb: DstDisambiguation,
    ) -> Result<ResolvedInstant, TzError> {
        let local_naive = (i64::from(date.to_days_since_epoch())) * 86_400
            + i64::from(time.seconds_since_midnight());

        // Check nearby transitions (any transition whose local wall time is within 2 days).
        let search_lo = local_naive - 2 * 86_400;
        let start_idx = self
            .transitions
            .partition_point(|t| t.utc_timestamp < search_lo);

        for t in &self.transitions[start_idx..] {
            if t.utc_timestamp > local_naive + 2 * 86_400 {
                break;
            }
            let local_before = t.utc_timestamp + i64::from(t.offset_from);
            let local_after = t.utc_timestamp + i64::from(t.offset_to);

            if t.offset_to > t.offset_from
                && local_naive >= local_before
                && local_naive < local_after
            {
                // Spring-forward gap
                return match disamb {
                    DstDisambiguation::Reject => Err(TzError::NonExistentLocalTime),
                    DstDisambiguation::Earlier => Ok(ResolvedInstant {
                        utc_timestamp: local_naive - i64::from(t.offset_from),
                        offset_seconds: t.offset_from,
                        is_dst: !t.is_dst,
                        tz_abbr: None,
                        was_gap: true,
                        was_fold: false,
                    }),
                    DstDisambiguation::Compatible | DstDisambiguation::Later => {
                        let gap = i64::from(t.offset_to - t.offset_from);
                        Ok(ResolvedInstant {
                            utc_timestamp: (local_naive + gap) - i64::from(t.offset_to),
                            offset_seconds: t.offset_to,
                            is_dst: t.is_dst,
                            tz_abbr: t.tzname.clone(),
                            was_gap: true,
                            was_fold: false,
                        })
                    }
                };
            }

            if t.offset_to < t.offset_from
                && local_naive >= local_after
                && local_naive < local_before
            {
                // Fall-back fold
                return match disamb {
                    DstDisambiguation::Reject => Err(TzError::AmbiguousLocalTime),
                    DstDisambiguation::Compatible | DstDisambiguation::Earlier => {
                        Ok(ResolvedInstant {
                            utc_timestamp: local_naive - i64::from(t.offset_from),
                            offset_seconds: t.offset_from,
                            is_dst: !t.is_dst,
                            tz_abbr: None,
                            was_gap: false,
                            was_fold: true,
                        })
                    }
                    DstDisambiguation::Later => Ok(ResolvedInstant {
                        utc_timestamp: local_naive - i64::from(t.offset_to),
                        offset_seconds: t.offset_to,
                        is_dst: t.is_dst,
                        tz_abbr: t.tzname.clone(),
                        was_gap: false,
                        was_fold: true,
                    }),
                };
            }
        }

        // Unambiguous local wall time: find active observance via local_after boundary.
        let idx = self
            .transitions
            .partition_point(|t| t.utc_timestamp + i64::from(t.offset_to) <= local_naive);
        let (offset_seconds, is_dst, tz_abbr) = if idx == 0 {
            (
                self.initial_offset,
                self.initial_is_dst,
                self.initial_tzname.clone(),
            )
        } else {
            let t = &self.transitions[idx - 1];
            (t.offset_to, t.is_dst, t.tzname.clone())
        };

        Ok(ResolvedInstant {
            utc_timestamp: local_naive - i64::from(offset_seconds),
            offset_seconds,
            is_dst,
            tz_abbr,
            was_gap: false,
            was_fold: false,
        })
    }
}

fn compile_observance(
    obs: ComponentRef<'_>,
    is_dst: bool,
    out: &mut Vec<TzTransition>,
) -> Result<(), TzError> {
    let dtstart = match obs.property(&PropertyName::DtStart).map(|p| &p.value) {
        Some(PropertyValue::DateTime(dt)) => dt.clone(),
        Some(PropertyValue::Date(d)) => CalDateTime {
            date: *d,
            time: CalTime {
                hour: 0,
                minute: 0,
                second: 0,
            },
            tz: TimeZoneRef::Floating,
        },
        _ => return Err(TzError::InvalidVTimezone("observance missing DTSTART")),
    };

    let offset_from = match obs.property(&PropertyName::TzOffsetFrom).map(|p| &p.value) {
        Some(PropertyValue::UtcOffset(off)) => off.seconds,
        _ => return Err(TzError::InvalidVTimezone("observance missing TZOFFSETFROM")),
    };

    let offset_to = match obs.property(&PropertyName::TzOffsetTo).map(|p| &p.value) {
        Some(PropertyValue::UtcOffset(off)) => off.seconds,
        _ => return Err(TzError::InvalidVTimezone("observance missing TZOFFSETTO")),
    };

    let tzname = obs
        .property_text(&PropertyName::TzName)
        .map(CompactString::from);

    let dtstart_utc = dtstart.to_naive_timestamp() - i64::from(offset_from);

    out.push(TzTransition {
        utc_timestamp: dtstart_utc,
        offset_from,
        offset_to,
        is_dst,
        tzname: tzname.clone(),
    });

    // RDATE entries
    for prop in obs.properties_named(&PropertyName::RDate) {
        match &prop.value {
            PropertyValue::DateTimeList(list) => {
                for rdt in list {
                    let utc_ts = if matches!(rdt.tz, TimeZoneRef::Utc) {
                        rdt.to_naive_timestamp()
                    } else {
                        rdt.to_naive_timestamp() - i64::from(offset_from)
                    };
                    out.push(TzTransition {
                        utc_timestamp: utc_ts,
                        offset_from,
                        offset_to,
                        is_dst,
                        tzname: tzname.clone(),
                    });
                }
            }
            PropertyValue::DateList(list) => {
                for rd in list {
                    let naive = i64::from(rd.to_days_since_epoch()) * 86_400
                        + i64::from(dtstart.time.seconds_since_midnight());
                    out.push(TzTransition {
                        utc_timestamp: naive - i64::from(offset_from),
                        offset_from,
                        offset_to,
                        is_dst,
                        tzname: tzname.clone(),
                    });
                }
            }
            _ => {}
        }
    }

    // RRULE expansion for VTIMEZONE observances
    for prop in obs.properties_named(&PropertyName::RRule) {
        if let Some(rule_str) = prop.value.as_str() {
            expand_vtimezone_rrule(
                rule_str,
                &dtstart,
                offset_from,
                offset_to,
                is_dst,
                tzname.as_ref(),
                out,
            );
        }
    }

    Ok(())
}

#[derive(Default)]
struct ObsRRule {
    freq: ObsFreq,
    interval: i32,
    count: Option<usize>,
    until_utc: Option<i64>,
    by_month: Vec<u8>,
    by_day: Vec<(Option<i8>, u8)>, // (ordinal, weekday 0=Mon..6=Sun)
    by_monthday: Vec<i8>,
}

#[derive(Default, Clone, Copy, PartialEq, Eq)]
enum ObsFreq {
    #[default]
    Yearly,
    Monthly,
    Weekly,
    Daily,
}

fn parse_weekday_code(code: &str) -> Option<(Option<i8>, u8)> {
    let trimmed = code.trim();
    if trimmed.len() < 2 {
        return None;
    }
    let split_at = trimmed.len() - 2;
    let ord_str = &trimmed[..split_at];
    let day_str = &trimmed[split_at..];

    let weekday = match day_str.to_ascii_uppercase().as_str() {
        "MO" => 0,
        "TU" => 1,
        "WE" => 2,
        "TH" => 3,
        "FR" => 4,
        "SA" => 5,
        "SU" => 6,
        _ => return None,
    };

    let ord = if ord_str.is_empty() {
        None
    } else {
        Some(ord_str.parse::<i8>().ok()?)
    };

    Some((ord, weekday))
}

fn parse_obs_rrule(raw: &str, offset_from: i32) -> Option<ObsRRule> {
    let mut rule = ObsRRule {
        interval: 1,
        ..ObsRRule::default()
    };

    for part in raw.split(';') {
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        match k.trim().to_ascii_uppercase().as_str() {
            "FREQ" => {
                rule.freq = match v.trim().to_ascii_uppercase().as_str() {
                    "YEARLY" => ObsFreq::Yearly,
                    "MONTHLY" => ObsFreq::Monthly,
                    "WEEKLY" => ObsFreq::Weekly,
                    "DAILY" => ObsFreq::Daily,
                    _ => return None,
                };
            }
            "INTERVAL" => {
                rule.interval = v.trim().parse::<i32>().ok()?.max(1);
            }
            "COUNT" => {
                rule.count = Some(v.trim().parse::<usize>().ok()?);
            }
            "UNTIL" => {
                let s = v.trim().as_bytes();
                if let Ok(dt) = CalDateTime::parse_bytes(s, None) {
                    let utc_ts = if matches!(dt.tz, TimeZoneRef::Utc) {
                        dt.to_naive_timestamp()
                    } else {
                        dt.to_naive_timestamp() - i64::from(offset_from)
                    };
                    rule.until_utc = Some(utc_ts);
                } else if let Ok(d) = CalDate::parse_bytes(s) {
                    rule.until_utc = Some(i64::from(d.to_days_since_epoch()) * 86_400 + 86_399);
                }
            }
            "BYMONTH" => {
                for m in v.split(',') {
                    if let Ok(mv) = m.trim().parse::<u8>() {
                        if (1..=12).contains(&mv) {
                            rule.by_month.push(mv);
                        }
                    }
                }
            }
            "BYDAY" => {
                for d in v.split(',') {
                    if let Some(parsed) = parse_weekday_code(d) {
                        rule.by_day.push(parsed);
                    }
                }
            }
            "BYMONTHDAY" => {
                for md in v.split(',') {
                    if let Ok(v) = md.trim().parse::<i8>() {
                        if v != 0 && (-31..=31).contains(&v) {
                            rule.by_monthday.push(v);
                        }
                    }
                }
            }
            _ => {}
        }
    }

    Some(rule)
}

fn expand_vtimezone_rrule(
    raw: &str,
    dtstart: &CalDateTime,
    offset_from: i32,
    offset_to: i32,
    is_dst: bool,
    tzname: Option<&CompactString>,
    out: &mut Vec<TzTransition>,
) {
    let Some(rule) = parse_obs_rrule(raw, offset_from) else {
        return;
    };

    if rule.count == Some(1) {
        return;
    }

    let template = TzTransition {
        utc_timestamp: 0,
        offset_from,
        offset_to,
        is_dst,
        tzname: tzname.cloned(),
    };

    match rule.freq {
        ObsFreq::Yearly => expand_yearly(&rule, dtstart, &template, out),
        ObsFreq::Monthly => expand_monthly(&rule, dtstart, &template, out),
        ObsFreq::Weekly | ObsFreq::Daily => expand_fixed_step(&rule, dtstart, &template, out),
    }
}

fn expand_yearly(
    rule: &ObsRRule,
    dtstart: &CalDateTime,
    template: &TzTransition,
    out: &mut Vec<TzTransition>,
) {
    let dtstart_utc = dtstart.to_naive_timestamp() - i64::from(template.offset_from);
    let mut emitted = 1_usize;
    let months = if rule.by_month.is_empty() {
        vec![dtstart.date.month]
    } else {
        rule.by_month.clone()
    };

    let mut year = i32::from(dtstart.date.year);
    while year <= i32::from(MAX_VTIMEZONE_YEAR) {
        let yr = year as i16;
        for &month in &months {
            for day in resolve_month_days(yr, month, dtstart.date.day, rule) {
                if let Ok(d) = CalDate::new(yr, month, day) {
                    let utc_ts = (i64::from(d.to_days_since_epoch())) * 86_400
                        + i64::from(dtstart.time.seconds_since_midnight())
                        - i64::from(template.offset_from);
                    if utc_ts <= dtstart_utc {
                        continue;
                    }
                    if rule.until_utc.is_some_and(|u| utc_ts > u) {
                        return;
                    }
                    let mut entry = template.clone();
                    entry.utc_timestamp = utc_ts;
                    out.push(entry);
                    emitted += 1;
                    if rule.count.is_some_and(|c| emitted >= c) {
                        return;
                    }
                }
            }
        }
        year += rule.interval;
    }
}

fn expand_monthly(
    rule: &ObsRRule,
    dtstart: &CalDateTime,
    template: &TzTransition,
    out: &mut Vec<TzTransition>,
) {
    let dtstart_utc = dtstart.to_naive_timestamp() - i64::from(template.offset_from);
    let mut emitted = 1_usize;
    let mut cur_year = i32::from(dtstart.date.year);
    let mut cur_month = i32::from(dtstart.date.month);

    while cur_year <= i32::from(MAX_VTIMEZONE_YEAR) {
        let yr = cur_year as i16;
        let mo = cur_month as u8;
        if rule.by_month.is_empty() || rule.by_month.contains(&mo) {
            for day in resolve_month_days(yr, mo, dtstart.date.day, rule) {
                if let Ok(d) = CalDate::new(yr, mo, day) {
                    let utc_ts = (i64::from(d.to_days_since_epoch())) * 86_400
                        + i64::from(dtstart.time.seconds_since_midnight())
                        - i64::from(template.offset_from);
                    if utc_ts <= dtstart_utc {
                        continue;
                    }
                    if rule.until_utc.is_some_and(|u| utc_ts > u) {
                        return;
                    }
                    let mut entry = template.clone();
                    entry.utc_timestamp = utc_ts;
                    out.push(entry);
                    emitted += 1;
                    if rule.count.is_some_and(|c| emitted >= c) {
                        return;
                    }
                }
            }
        }
        let total_months = (cur_year * 12 + (cur_month - 1)) + rule.interval;
        cur_year = total_months.div_euclid(12);
        cur_month = total_months.rem_euclid(12) + 1;
    }
}

fn expand_fixed_step(
    rule: &ObsRRule,
    dtstart: &CalDateTime,
    template: &TzTransition,
    out: &mut Vec<TzTransition>,
) {
    let mut emitted = 1_usize;
    let step_days = if rule.freq == ObsFreq::Weekly {
        7 * rule.interval
    } else {
        rule.interval
    };
    let mut d = dtstart.date.add_days(step_days);
    while d.year <= MAX_VTIMEZONE_YEAR {
        let utc_ts = (i64::from(d.to_days_since_epoch())) * 86_400
            + i64::from(dtstart.time.seconds_since_midnight())
            - i64::from(template.offset_from);
        if rule.until_utc.is_some_and(|u| utc_ts > u) {
            return;
        }
        let mut entry = template.clone();
        entry.utc_timestamp = utc_ts;
        out.push(entry);
        emitted += 1;
        if rule.count.is_some_and(|c| emitted >= c) {
            return;
        }
        d = d.add_days(step_days);
    }
}

fn resolve_month_days(year: i16, month: u8, default_day: u8, rule: &ObsRRule) -> Vec<u8> {
    let max_day = days_in_month(year, month);
    if max_day == 0 {
        return Vec::new();
    }

    let mut days = if !rule.by_day.is_empty() {
        let mut matched = Vec::new();
        for &(ord, target_wd) in &rule.by_day {
            let mut wd_days = Vec::with_capacity(5);
            for day in 1..=max_day {
                if let Ok(d) = CalDate::new(year, month, day) {
                    if d.weekday() == target_wd {
                        wd_days.push(day);
                    }
                }
            }
            match ord {
                None => matched.extend(wd_days),
                Some(n) if n > 0 => {
                    if let Some(&d) = wd_days.get((n - 1) as usize) {
                        matched.push(d);
                    }
                }
                Some(n) if n < 0 => {
                    let rev_idx = (-n - 1) as usize;
                    if rev_idx < wd_days.len() {
                        matched.push(wd_days[wd_days.len() - 1 - rev_idx]);
                    }
                }
                Some(_) => {}
            }
        }
        matched
    } else if !rule.by_monthday.is_empty() {
        rule.by_monthday
            .iter()
            .filter_map(|&md| {
                let d = if md > 0 {
                    i16::from(md)
                } else {
                    i16::from(max_day) + 1 + i16::from(md)
                };
                if d >= 1 && d <= i16::from(max_day) {
                    Some(d as u8)
                } else {
                    None
                }
            })
            .collect()
    } else if default_day <= max_day {
        vec![default_day]
    } else {
        Vec::new()
    };

    if !rule.by_day.is_empty() && !rule.by_monthday.is_empty() {
        days.retain(|&day| {
            rule.by_monthday.iter().any(|&md| {
                let target = if md > 0 {
                    i16::from(md)
                } else {
                    i16::from(max_day) + 1 + i16::from(md)
                };
                i16::from(day) == target
            })
        });
    }

    days.sort_unstable();
    days.dedup();
    days
}
