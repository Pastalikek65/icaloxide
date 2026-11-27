use std::fmt;

use compact_str::CompactString;

use crate::error::DateTimeError;

/// Parse `n` ASCII digits into a `u32`.
#[inline]
fn parse_digits(bytes: &[u8], kind: &'static str) -> Result<u32, DateTimeError> {
    if bytes.is_empty() {
        return Err(DateTimeError::InvalidFormat { kind });
    }
    let mut val: u32 = 0;
    for &b in bytes {
        if !b.is_ascii_digit() {
            return Err(DateTimeError::InvalidDigit { kind });
        }
        val = val
            .checked_mul(10)
            .and_then(|v| v.checked_add(u32::from(b - b'0')))
            .ok_or(DateTimeError::InvalidFormat { kind })?;
    }
    Ok(val)
}

/// Return `true` if `year` is a leap year in the proleptic Gregorian calendar.
#[inline]
pub const fn is_leap_year(year: i16) -> bool {
    let y = year as i32;
    (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0)
}

/// Return the number of days in `(year, month)` (`month` in `1..=12`).
#[inline]
pub const fn days_in_month(year: i16, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap_year(year) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

/// RFC 5545 Section 3.3.4 `DATE` value (`YYYYMMDD`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CalDate {
    pub year: i16,
    pub month: u8,
    pub day: u8,
}

impl CalDate {
    /// Construct and validate a calendar date.
    pub fn new(year: i16, month: u8, day: u8) -> Result<Self, DateTimeError> {
        if !(1..=12).contains(&month) {
            return Err(DateTimeError::InvalidDate { year, month, day });
        }
        let max_day = days_in_month(year, month);
        if day < 1 || day > max_day {
            return Err(DateTimeError::InvalidDate { year, month, day });
        }
        Ok(Self { year, month, day })
    }

    /// Parse `YYYYMMDD` directly from ASCII bytes (supports optional `YYYY-MM-DD` in callers if needed, strict 8 bytes here).
    pub fn parse_bytes(bytes: &[u8]) -> Result<Self, DateTimeError> {
        if bytes.len() != 8 {
            return Err(DateTimeError::InvalidFormat { kind: "DATE" });
        }
        let year = parse_digits(&bytes[0..4], "DATE")? as i16;
        let month = parse_digits(&bytes[4..6], "DATE")? as u8;
        let day = parse_digits(&bytes[6..8], "DATE")? as u8;
        Self::new(year, month, day)
    }

    /// Convert to days since Unix epoch (`1970-01-01 = 0`) using Howard Hinnant's O(1) algorithm.
    #[inline]
    pub const fn to_days_since_epoch(self) -> i32 {
        let mut y = self.year as i32;
        let m = self.month as i32;
        let d = self.day as i32;
        if m <= 2 {
            y -= 1;
        }
        let era = if y >= 0 { y } else { y - 399 } / 400;
        let yoe = y - era * 400;
        let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        era * 146_097 + doe - 719_468
    }

    /// Convert days since Unix epoch (`1970-01-01 = 0`) back into [`CalDate`].
    #[inline]
    pub const fn from_days_since_epoch(days: i32) -> Self {
        let z = days + 719_468;
        let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = mp + if mp < 10 { 3 } else { -9 };
        let year = (y + if m <= 2 { 1 } else { 0 }) as i16;
        Self {
            year,
            month: m as u8,
            day: d as u8,
        }
    }

    /// ISO weekday: `0 = Monday .. 6 = Sunday`.
    #[inline]
    pub const fn weekday(self) -> u8 {
        // 1970-01-01 was a Thursday (index 3).
        (self.to_days_since_epoch() + 3).rem_euclid(7) as u8
    }

    /// 1-based ordinal day of the year (`1..=366`).
    #[inline]
    pub const fn day_of_year(self) -> u16 {
        let jan1 = Self {
            year: self.year,
            month: 1,
            day: 1,
        };
        (self.to_days_since_epoch() - jan1.to_days_since_epoch() + 1) as u16
    }

    /// Add `delta_days` to this date.
    #[must_use]
    #[inline]
    pub const fn add_days(self, delta_days: i32) -> Self {
        Self::from_days_since_epoch(self.to_days_since_epoch() + delta_days)
    }
}

impl fmt::Display for CalDate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:04}{:02}{:02}", self.year, self.month, self.day)
    }
}

/// RFC 5545 Section 3.3.12 `TIME` value (`HHMMSS`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CalTime {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
}

impl CalTime {
    /// Construct and validate a wall-clock time (allows leap second `60`).
    pub fn new(hour: u8, minute: u8, second: u8) -> Result<Self, DateTimeError> {
        if hour > 23 || minute > 59 || second > 60 {
            return Err(DateTimeError::InvalidTime {
                hour,
                minute,
                second,
            });
        }
        Ok(Self {
            hour,
            minute,
            second,
        })
    }

    /// Parse `HHMMSS` directly from 6 ASCII bytes.
    pub fn parse_bytes(bytes: &[u8]) -> Result<Self, DateTimeError> {
        if bytes.len() != 6 {
            return Err(DateTimeError::InvalidFormat { kind: "TIME" });
        }
        let hour = parse_digits(&bytes[0..2], "TIME")? as u8;
        let minute = parse_digits(&bytes[2..4], "TIME")? as u8;
        let second = parse_digits(&bytes[4..6], "TIME")? as u8;
        Self::new(hour, minute, second)
    }

    /// Seconds elapsed since midnight (`0..=86400`).
    #[inline]
    pub const fn seconds_since_midnight(self) -> u32 {
        (self.hour as u32) * 3600 + (self.minute as u32) * 60 + (self.second as u32)
    }
}

impl fmt::Display for CalTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:02}{:02}{:02}", self.hour, self.minute, self.second)
    }
}

/// Timezone reference attached to a [`CalDateTime`].
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub enum TimeZoneRef {
    /// UTC instant (`Z` suffix).
    Utc,
    /// Floating local time (no `Z` and no `TZID`).
    #[default]
    Floating,
    /// Named timezone identifier (`TZID=...`).
    TzId(CompactString),
}

/// RFC 5545 Section 3.3.5 `DATE-TIME` value (`YYYYMMDDTHHMMSS[Z]`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct CalDateTime {
    pub date: CalDate,
    pub time: CalTime,
    pub tz: TimeZoneRef,
}

impl CalDateTime {
    /// Parse `YYYYMMDDTHHMMSS[Z]` combined with an optional `TZID` parameter.
    pub fn parse_bytes(bytes: &[u8], tzid: Option<&str>) -> Result<Self, DateTimeError> {
        let is_utc = match bytes.len() {
            15 => false,
            16 if bytes[15] == b'Z' || bytes[15] == b'z' => true,
            _ => return Err(DateTimeError::InvalidFormat { kind: "DATE-TIME" }),
        };

        if bytes[8] != b'T' && bytes[8] != b't' {
            return Err(DateTimeError::InvalidFormat { kind: "DATE-TIME" });
        }

        if is_utc && tzid.is_some() {
            return Err(DateTimeError::ConflictingUtcAndTzid);
        }

        let date = CalDate::parse_bytes(&bytes[0..8])?;
        let time = CalTime::parse_bytes(&bytes[9..15])?;
        let tz = if is_utc {
            TimeZoneRef::Utc
        } else if let Some(id) = tzid {
            TimeZoneRef::TzId(CompactString::from(id))
        } else {
            TimeZoneRef::Floating
        };

        Ok(Self { date, time, tz })
    }

    /// Naive Unix timestamp in seconds (ignoring timezone offsets).
    #[inline]
    pub const fn to_naive_timestamp(&self) -> i64 {
        (self.date.to_days_since_epoch() as i64) * 86_400
            + (self.time.seconds_since_midnight() as i64)
    }
}

impl fmt::Display for CalDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}T{}", self.date, self.time)?;
        if matches!(self.tz, TimeZoneRef::Utc) {
            f.write_str("Z")?;
        }
        Ok(())
    }
}

/// Either a calendar `DATE` or a `DATE-TIME`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum DateOrDateTime {
    Date(CalDate),
    DateTime(CalDateTime),
}

impl DateOrDateTime {
    /// Return the underlying [`CalDate`].
    pub const fn date(&self) -> CalDate {
        match self {
            Self::Date(d) => *d,
            Self::DateTime(dt) => dt.date,
        }
    }
}

impl fmt::Display for DateOrDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Date(d) => d.fmt(f),
            Self::DateTime(dt) => dt.fmt(f),
        }
    }
}

/// RFC 5545 Section 3.3.6 `DURATION` value (`[+/-]P...`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct CalDuration {
    pub negative: bool,
    pub weeks: u32,
    pub days: u32,
    pub hours: u32,
    pub minutes: u32,
    pub seconds: u32,
}

impl CalDuration {
    /// Parse an RFC 5545 duration string from ASCII bytes.
    pub fn parse_bytes(bytes: &[u8]) -> Result<Self, DateTimeError> {
        if bytes.is_empty() {
            return Err(DateTimeError::InvalidDuration);
        }

        let mut i = 0;
        let negative = match bytes[0] {
            b'-' => {
                i = 1;
                true
            }
            b'+' => {
                i = 1;
                false
            }
            _ => false,
        };

        if i >= bytes.len() || bytes[i] != b'P' {
            return Err(DateTimeError::InvalidDuration);
        }
        i += 1;

        if i >= bytes.len() {
            return Err(DateTimeError::InvalidDuration);
        }

        let mut dur = Self {
            negative,
            ..Self::default()
        };

        let mut in_time = false;
        let mut saw_any = false;
        let mut saw_weeks = false;

        while i < bytes.len() {
            if bytes[i] == b'T' {
                if in_time || saw_weeks {
                    return Err(DateTimeError::InvalidDuration);
                }
                in_time = true;
                i += 1;
                if i >= bytes.len() {
                    return Err(DateTimeError::InvalidDuration);
                }
                continue;
            }

            let num_start = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            if i == num_start || i >= bytes.len() {
                return Err(DateTimeError::InvalidDuration);
            }
            let val = parse_digits(&bytes[num_start..i], "DURATION")?;
            let unit = bytes[i];
            i += 1;
            saw_any = true;

            match (in_time, unit) {
                (false, b'W') => {
                    if saw_weeks || dur.days != 0 || i != bytes.len() {
                        return Err(DateTimeError::InvalidDuration);
                    }
                    saw_weeks = true;
                    dur.weeks = val;
                }
                (false, b'D') => {
                    dur.days = val;
                }
                (true, b'H') => dur.hours = val,
                (true, b'M') => dur.minutes = val,
                (true, b'S') => dur.seconds = val,
                _ => return Err(DateTimeError::InvalidDuration),
            }
        }

        if !saw_any {
            return Err(DateTimeError::InvalidDuration);
        }

        Ok(dur)
    }

    /// Total signed nominal seconds (assuming 1 day = 86,400 seconds).
    #[inline]
    pub const fn total_seconds(self) -> i64 {
        let abs = (self.weeks as i64) * 7 * 86_400
            + (self.days as i64) * 86_400
            + (self.hours as i64) * 3_600
            + (self.minutes as i64) * 60
            + (self.seconds as i64);
        if self.negative {
            -abs
        } else {
            abs
        }
    }
}

impl fmt::Display for CalDuration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.negative {
            f.write_str("-")?;
        }
        f.write_str("P")?;
        if self.weeks > 0
            && self.days == 0
            && self.hours == 0
            && self.minutes == 0
            && self.seconds == 0
        {
            return write!(f, "{}W", self.weeks);
        }
        let total_days = self.days + self.weeks * 7;
        if total_days > 0 {
            write!(f, "{total_days}D")?;
        }
        if self.hours > 0 || self.minutes > 0 || self.seconds > 0 || total_days == 0 {
            f.write_str("T")?;
            if self.hours > 0 {
                write!(f, "{}H", self.hours)?;
            }
            if self.minutes > 0 {
                write!(f, "{}M", self.minutes)?;
            }
            if self.seconds > 0 || (total_days == 0 && self.hours == 0 && self.minutes == 0) {
                write!(f, "{}S", self.seconds)?;
            }
        }
        Ok(())
    }
}

/// RFC 5545 Section 3.3.9 `PERIOD` value (`start/end` or `start/duration`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum CalPeriod {
    Explicit {
        start: CalDateTime,
        end: CalDateTime,
    },
    Duration {
        start: CalDateTime,
        duration: CalDuration,
    },
}

impl CalPeriod {
    /// Parse an RFC 5545 period from ASCII bytes (`start/end` or `start/duration`).
    pub fn parse_bytes(bytes: &[u8], tzid: Option<&str>) -> Result<Self, DateTimeError> {
        let Some(slash) = bytes.iter().position(|&b| b == b'/') else {
            return Err(DateTimeError::InvalidPeriod);
        };
        let start = CalDateTime::parse_bytes(&bytes[..slash], tzid)?;
        let rhs = &bytes[slash + 1..];
        if rhs.is_empty() {
            return Err(DateTimeError::InvalidPeriod);
        }
        if rhs[0] == b'P' || rhs[0] == b'+' || rhs[0] == b'-' {
            let duration = CalDuration::parse_bytes(rhs)?;
            Ok(Self::Duration { start, duration })
        } else {
            let end = CalDateTime::parse_bytes(rhs, tzid)?;
            Ok(Self::Explicit { start, end })
        }
    }

    /// Return `true` if this period has zero or negative duration.
    #[inline]
    pub fn is_zero_or_negative(&self) -> bool {
        match self {
            Self::Duration { duration, .. } => duration.total_seconds() <= 0,
            Self::Explicit { start, end } => {
                if start.tz == end.tz {
                    end.to_naive_timestamp() <= start.to_naive_timestamp()
                } else {
                    false
                }
            }
        }
    }
}

impl fmt::Display for CalPeriod {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Explicit { start, end } => write!(f, "{start}/{end}"),
            Self::Duration { start, duration } => write!(f, "{start}/{duration}"),
        }
    }
}

/// RFC 5545 Section 3.3.14 `UTC-OFFSET` value (`[+/-]HHMM[SS]`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct UtcOffset {
    /// Signed offset from UTC in seconds.
    pub seconds: i32,
}

impl UtcOffset {
    /// Parse `[+/-]HHMM` or `[+/-]HHMMSS` from ASCII bytes.
    pub fn parse_bytes(bytes: &[u8]) -> Result<Self, DateTimeError> {
        if bytes.len() != 5 && bytes.len() != 7 {
            return Err(DateTimeError::InvalidUtcOffset);
        }
        let sign = match bytes[0] {
            b'+' => 1_i32,
            b'-' => -1_i32,
            _ => return Err(DateTimeError::InvalidUtcOffset),
        };
        let hours = parse_digits(&bytes[1..3], "UTC-OFFSET")? as i32;
        let minutes = parse_digits(&bytes[3..5], "UTC-OFFSET")? as i32;
        let seconds = if bytes.len() == 7 {
            parse_digits(&bytes[5..7], "UTC-OFFSET")? as i32
        } else {
            0
        };

        if hours > 23 || minutes > 59 || seconds > 59 {
            return Err(DateTimeError::InvalidUtcOffset);
        }
        let total = sign * (hours * 3600 + minutes * 60 + seconds);
        // RFC 5545 forbids `-0000` (normalize or accept as 0? RFC says -0000 is invalid, wait: some clients emit it; keep signed 0).
        Ok(Self { seconds: total })
    }
}

impl fmt::Display for UtcOffset {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let sign = if self.seconds < 0 { '-' } else { '+' };
        let abs = self.seconds.abs();
        let hours = abs / 3600;
        let minutes = (abs % 3600) / 60;
        let secs = abs % 60;
        if secs == 0 {
            write!(f, "{sign}{hours:02}{minutes:02}")
        } else {
            write!(f, "{sign}{hours:02}{minutes:02}{secs:02}")
        }
    }
}
