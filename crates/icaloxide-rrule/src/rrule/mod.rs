pub mod calendar_math;
pub mod expander;

use std::fmt;

use icaloxide_core::datetime::{CalDate, CalDateTime};

use crate::error::RRuleError;

pub use expander::{expand_rrule, LocalDateTime, RRuleIter};

/// RFC 5545 `FREQ` enumeration.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Frequency {
    Yearly,
    Monthly,
    Weekly,
    Daily,
    Hourly,
    Minutely,
    Secondly,
}

impl Frequency {
    pub fn parse(raw: &str) -> Result<Self, RRuleError> {
        match raw.trim().to_ascii_uppercase().as_str() {
            "YEARLY" => Ok(Self::Yearly),
            "MONTHLY" => Ok(Self::Monthly),
            "WEEKLY" => Ok(Self::Weekly),
            "DAILY" => Ok(Self::Daily),
            "HOURLY" => Ok(Self::Hourly),
            "MINUTELY" => Ok(Self::Minutely),
            "SECONDLY" => Ok(Self::Secondly),
            other => Err(RRuleError::InvalidFreq(other.into())),
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Yearly => "YEARLY",
            Self::Monthly => "MONTHLY",
            Self::Weekly => "WEEKLY",
            Self::Daily => "DAILY",
            Self::Hourly => "HOURLY",
            Self::Minutely => "MINUTELY",
            Self::Secondly => "SECONDLY",
        }
    }
}

impl fmt::Display for Frequency {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Day of week (`Mon = 0 .. Sun = 6`), matching [`CalDate::weekday`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum Weekday {
    Mon = 0,
    Tue = 1,
    Wed = 2,
    Thu = 3,
    Fri = 4,
    Sat = 5,
    Sun = 6,
}

impl Weekday {
    pub fn parse(raw: &str) -> Result<Self, RRuleError> {
        match raw.trim().to_ascii_uppercase().as_str() {
            "MO" => Ok(Self::Mon),
            "TU" => Ok(Self::Tue),
            "WE" => Ok(Self::Wed),
            "TH" => Ok(Self::Thu),
            "FR" => Ok(Self::Fri),
            "SA" => Ok(Self::Sat),
            "SU" => Ok(Self::Sun),
            other => Err(RRuleError::InvalidWeekday(other.into())),
        }
    }

    #[inline]
    pub const fn to_u8(self) -> u8 {
        self as u8
    }

    #[inline]
    pub const fn from_u8(val: u8) -> Self {
        match val.rem_euclid(7) {
            0 => Self::Mon,
            1 => Self::Tue,
            2 => Self::Wed,
            3 => Self::Thu,
            4 => Self::Fri,
            5 => Self::Sat,
            _ => Self::Sun,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Mon => "MO",
            Self::Tue => "TU",
            Self::Wed => "WE",
            Self::Thu => "TH",
            Self::Fri => "FR",
            Self::Sat => "SA",
            Self::Sun => "SU",
        }
    }
}

impl fmt::Display for Weekday {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// `BYDAY` item with optional signed ordinal (e.g. `MO`, `2MO`, `-1SU`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ByWeekday {
    pub ordinal: Option<i16>,
    pub weekday: Weekday,
}

impl ByWeekday {
    pub fn parse(raw: &str) -> Result<Self, RRuleError> {
        let trimmed = raw.trim();
        if trimmed.len() < 2 {
            return Err(RRuleError::InvalidWeekday(trimmed.into()));
        }
        let split_at = trimmed.len() - 2;
        let ord_part = &trimmed[..split_at];
        let wd_part = &trimmed[split_at..];
        let weekday = Weekday::parse(wd_part)?;
        let ordinal = if ord_part.is_empty() {
            None
        } else {
            let n = ord_part
                .parse::<i16>()
                .map_err(|_| RRuleError::InvalidWeekday(trimmed.into()))?;
            if n == 0 || !(-53..=53).contains(&n) {
                return Err(RRuleError::InvalidWeekday(trimmed.into()));
            }
            Some(n)
        };
        Ok(Self { ordinal, weekday })
    }
}

impl fmt::Display for ByWeekday {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(ord) = self.ordinal {
            write!(f, "{ord}")?;
        }
        f.write_str(self.weekday.as_str())
    }
}

/// `UNTIL` boundary value (`DATE` or `DATE-TIME`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum UntilValue {
    Date(CalDate),
    DateTime(CalDateTime),
}

impl fmt::Display for UntilValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Date(d) => d.fmt(f),
            Self::DateTime(dt) => dt.fmt(f),
        }
    }
}

/// Parsed and validated RFC 5545 Section 3.3.10 recurrence rule (`RRULE`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RRule {
    pub freq: Frequency,
    pub interval: u32,
    pub count: Option<u32>,
    pub until: Option<UntilValue>,
    pub wkst: Weekday,
    pub by_second: Vec<u8>,
    pub by_minute: Vec<u8>,
    pub by_hour: Vec<u8>,
    pub by_day: Vec<ByWeekday>,
    pub by_month_day: Vec<i8>,
    pub by_year_day: Vec<i16>,
    pub by_week_no: Vec<i8>,
    pub by_month: Vec<u8>,
    pub by_set_pos: Vec<i16>,
}

impl RRule {
    /// Create a minimal `RRule` with the given `Frequency`, `interval = 1`, and `wkst = Mon`.
    pub fn new(freq: Frequency) -> Self {
        Self {
            freq,
            interval: 1,
            count: None,
            until: None,
            wkst: Weekday::Mon,
            by_second: Vec::new(),
            by_minute: Vec::new(),
            by_hour: Vec::new(),
            by_day: Vec::new(),
            by_month_day: Vec::new(),
            by_year_day: Vec::new(),
            by_week_no: Vec::new(),
            by_month: Vec::new(),
            by_set_pos: Vec::new(),
        }
    }

    /// Parse an RFC 5545 `RRULE` value string (without the `RRULE:` prefix).
    pub fn parse(raw: &str) -> Result<Self, RRuleError> {
        let mut freq: Option<Frequency> = None;
        let mut rule = Self::new(Frequency::Daily);

        for part in raw.split(';') {
            let trimmed = part.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Some((k, v)) = trimmed.split_once('=') else {
                return Err(RRuleError::InvalidPart(trimmed.into()));
            };
            let key = k.trim().to_ascii_uppercase();
            let val = v.trim();
            match key.as_str() {
                "FREQ" => freq = Some(Frequency::parse(val)?),
                "INTERVAL" => {
                    let iv = parse_ranged_i32(val, 1, 100_000)? as u32;
                    rule.interval = iv;
                }
                "COUNT" => {
                    let c = parse_ranged_i32(val, 0, 10_000_000)? as u32;
                    rule.count = Some(c);
                }
                "UNTIL" => rule.until = Some(parse_until(val)?),
                "WKST" => rule.wkst = Weekday::parse(val)?,
                "BYSECOND" => rule.by_second = parse_u8_list(val, 0, 60)?,
                "BYMINUTE" => rule.by_minute = parse_u8_list(val, 0, 59)?,
                "BYHOUR" => rule.by_hour = parse_u8_list(val, 0, 23)?,
                "BYMONTH" => rule.by_month = parse_u8_list(val, 1, 12)?,
                "BYMONTHDAY" => rule.by_month_day = parse_nonzero_i8_list(val, -31, 31)?,
                "BYWEEKNO" => rule.by_week_no = parse_nonzero_i8_list(val, -53, 53)?,
                "BYYEARDAY" => rule.by_year_day = parse_nonzero_i16_list(val, -366, 366)?,
                "BYSETPOS" => rule.by_set_pos = parse_nonzero_i16_list(val, -366, 366)?,
                "BYDAY" => {
                    for item in val.split(',') {
                        rule.by_day.push(ByWeekday::parse(item)?);
                    }
                }
                _ => return Err(RRuleError::InvalidPart(trimmed.into())),
            }
        }

        rule.freq = freq.ok_or(RRuleError::MissingFreq)?;
        if rule.count.is_some() && rule.until.is_some() {
            return Err(RRuleError::CountAndUntilConflict);
        }

        rule.by_second.sort_unstable();
        rule.by_second.dedup();
        rule.by_minute.sort_unstable();
        rule.by_minute.dedup();
        rule.by_hour.sort_unstable();
        rule.by_hour.dedup();
        rule.by_month.sort_unstable();
        rule.by_month.dedup();
        rule.by_set_pos.sort_unstable();
        rule.by_set_pos.dedup();

        Ok(rule)
    }
}

fn parse_until(val: &str) -> Result<UntilValue, RRuleError> {
    let bytes = val.as_bytes();
    if bytes.len() == 8 {
        CalDate::parse_bytes(bytes)
            .map(UntilValue::Date)
            .map_err(|_| RRuleError::InvalidUntil(val.into()))
    } else {
        CalDateTime::parse_bytes(bytes, None)
            .map(UntilValue::DateTime)
            .map_err(|_| RRuleError::InvalidUntil(val.into()))
    }
}

fn parse_ranged_i32(raw: &str, min: i32, max: i32) -> Result<i32, RRuleError> {
    let n = raw
        .trim()
        .parse::<i32>()
        .map_err(|_| RRuleError::InvalidNumber(raw.into()))?;
    if n < min || n > max {
        return Err(RRuleError::InvalidNumber(raw.into()));
    }
    Ok(n)
}

fn parse_u8_list(raw: &str, min: i32, max: i32) -> Result<Vec<u8>, RRuleError> {
    raw.split(',')
        .map(|s| parse_ranged_i32(s, min, max).map(|v| v as u8))
        .collect()
}

fn parse_nonzero_i8_list(raw: &str, min: i32, max: i32) -> Result<Vec<i8>, RRuleError> {
    raw.split(',')
        .map(|s| {
            let v = parse_ranged_i32(s, min, max)?;
            if v == 0 {
                return Err(RRuleError::InvalidNumber(s.into()));
            }
            Ok(v as i8)
        })
        .collect()
}

fn parse_nonzero_i16_list(raw: &str, min: i32, max: i32) -> Result<Vec<i16>, RRuleError> {
    raw.split(',')
        .map(|s| {
            let v = parse_ranged_i32(s, min, max)?;
            if v == 0 {
                return Err(RRuleError::InvalidNumber(s.into()));
            }
            Ok(v as i16)
        })
        .collect()
}

impl fmt::Display for RRule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "FREQ={}", self.freq)?;
        if self.interval != 1 {
            write!(f, ";INTERVAL={}", self.interval)?;
        }
        if let Some(c) = self.count {
            write!(f, ";COUNT={c}")?;
        }
        if let Some(u) = &self.until {
            write!(f, ";UNTIL={u}")?;
        }
        if self.wkst != Weekday::Mon {
            write!(f, ";WKST={}", self.wkst)?;
        }
        write_joined(f, "BYMONTH", &self.by_month)?;
        write_joined(f, "BYWEEKNO", &self.by_week_no)?;
        write_joined(f, "BYYEARDAY", &self.by_year_day)?;
        write_joined(f, "BYMONTHDAY", &self.by_month_day)?;
        write_joined(f, "BYDAY", &self.by_day)?;
        write_joined(f, "BYHOUR", &self.by_hour)?;
        write_joined(f, "BYMINUTE", &self.by_minute)?;
        write_joined(f, "BYSECOND", &self.by_second)?;
        write_joined(f, "BYSETPOS", &self.by_set_pos)?;
        Ok(())
    }
}

fn write_joined<T: fmt::Display>(
    f: &mut fmt::Formatter<'_>,
    key: &str,
    items: &[T],
) -> fmt::Result {
    if items.is_empty() {
        return Ok(());
    }
    write!(f, ";{key}=")?;
    for (idx, item) in items.iter().enumerate() {
        if idx > 0 {
            f.write_str(",")?;
        }
        write!(f, "{item}")?;
    }
    Ok(())
}
