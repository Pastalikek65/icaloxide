pub mod generator;
pub mod registry;
pub mod vtimezone;
pub mod windows;

use std::sync::Arc;

use compact_str::CompactString;
use jiff::tz::{AmbiguousOffset, Disambiguation, TimeZone};
use thiserror::Error;

use crate::datetime::{CalDate, CalDateTime, CalTime, TimeZoneRef, UtcOffset};

pub use generator::{add_missing_timezones, collect_missing_tzids, collect_used_tzids};
pub use registry::TzRegistry;
pub use vtimezone::{CompiledVTimezone, TzTransition};
pub use windows::{parse_fixed_offset_tzid, resolve_iana_alias, strip_vendor_prefix};

/// Errors emitted during timezone lookup or local-time disambiguation.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum TzError {
    #[error("unknown timezone identifier `{0}`")]
    UnknownTimeZone(CompactString),
    #[error("cannot resolve floating time to UTC without a default timezone")]
    FloatingTimeZone,
    #[error("local wall time falls in a non-existent DST spring-forward gap")]
    NonExistentLocalTime,
    #[error("local wall time falls in an ambiguous DST fall-back fold")]
    AmbiguousLocalTime,
    #[error("invalid VTIMEZONE component: {0}")]
    InvalidVTimezone(&'static str),
}

/// Strategy for resolving DST spring-forward gaps and fall-back folds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DstDisambiguation {
    /// RFC 5545 / JS Temporal `compatible`: earlier offset in fall-back folds,
    /// shifted forward after spring-forward gaps.
    #[default]
    Compatible,
    /// Always pick the earlier instant (pre-transition offset).
    Earlier,
    /// Always pick the later instant (post-transition offset).
    Later,
    /// Return an error when encountering a gap or fold.
    Reject,
}

/// Result of resolving a local date-time against a timezone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedInstant {
    /// Seconds since Unix epoch (`1970-01-01T00:00:00Z`).
    pub utc_timestamp: i64,
    /// Active UTC offset in seconds.
    pub offset_seconds: i32,
    /// Whether Daylight Saving Time is active at the resolved instant.
    pub is_dst: bool,
    /// Timezone abbreviation (e.g. `"EST"`, `"EDT"`, `"TRT"`), if known.
    pub tz_abbr: Option<CompactString>,
    /// `true` if the wall time landed in a spring-forward gap.
    pub was_gap: bool,
    /// `true` if the wall time landed in a fall-back fold.
    pub was_fold: bool,
}

/// Unified timezone representation for iCalendar processing.
#[derive(Debug, Clone)]
pub enum CalendarTimeZone {
    Utc,
    Floating,
    FixedOffset(UtcOffset),
    Iana(TimeZone),
    Custom(Arc<CompiledVTimezone>),
}

impl CalendarTimeZone {
    /// Resolve `(CalDate, CalTime)` to a [`ResolvedInstant`] under `disamb`.
    pub fn resolve_local(
        &self,
        date: CalDate,
        time: CalTime,
        disamb: DstDisambiguation,
    ) -> Result<ResolvedInstant, TzError> {
        match self {
            Self::Utc => Ok(ResolvedInstant {
                utc_timestamp: naive_ts(date, time),
                offset_seconds: 0,
                is_dst: false,
                tz_abbr: Some(CompactString::const_new("UTC")),
                was_gap: false,
                was_fold: false,
            }),
            Self::Floating => Err(TzError::FloatingTimeZone),
            Self::FixedOffset(off) => Ok(ResolvedInstant {
                utc_timestamp: naive_ts(date, time) - i64::from(off.seconds),
                offset_seconds: off.seconds,
                is_dst: false,
                tz_abbr: None,
                was_gap: false,
                was_fold: false,
            }),
            Self::Custom(vtz) => vtz.resolve_local(date, time, disamb),
            Self::Iana(tz) => resolve_iana_local(tz, date, time, disamb),
        }
    }

    /// Resolve a [`CalDateTime`], honoring its embedded `TimeZoneRef`.
    pub fn resolve_datetime(
        &self,
        dt: &CalDateTime,
        disamb: DstDisambiguation,
    ) -> Result<ResolvedInstant, TzError> {
        if matches!(dt.tz, TimeZoneRef::Utc) {
            return Self::Utc.resolve_local(dt.date, dt.time, disamb);
        }
        self.resolve_local(dt.date, dt.time, disamb)
    }

    /// Return the UTC offset in seconds at a given UTC timestamp.
    pub fn offset_at_utc(&self, utc_timestamp: i64) -> Result<i32, TzError> {
        match self {
            Self::Utc => Ok(0),
            Self::Floating => Err(TzError::FloatingTimeZone),
            Self::FixedOffset(off) => Ok(off.seconds),
            Self::Custom(vtz) => Ok(vtz.offset_at_utc(utc_timestamp).0),
            Self::Iana(tz) => {
                let ts = jiff::Timestamp::from_second(utc_timestamp)
                    .map_err(|_| TzError::UnknownTimeZone("timestamp out of range".into()))?;
                Ok(tz.to_offset(ts).seconds())
            }
        }
    }
}

#[inline]
const fn naive_ts(date: CalDate, time: CalTime) -> i64 {
    (date.to_days_since_epoch() as i64) * 86_400 + (time.seconds_since_midnight() as i64)
}

fn resolve_iana_local(
    tz: &TimeZone,
    date: CalDate,
    time: CalTime,
    disamb: DstDisambiguation,
) -> Result<ResolvedInstant, TzError> {
    let civil_dt = jiff::civil::DateTime::new(
        date.year,
        date.month as i8,
        date.day as i8,
        time.hour as i8,
        time.minute as i8,
        time.second.min(59) as i8,
        0,
    )
    .map_err(|_| TzError::UnknownTimeZone("civil datetime out of jiff range".into()))?;

    let amb = tz.to_ambiguous_timestamp(civil_dt);
    let (was_gap, was_fold) = match amb.offset() {
        AmbiguousOffset::Unambiguous { .. } => (false, false),
        AmbiguousOffset::Gap { .. } => (true, false),
        AmbiguousOffset::Fold { .. } => (false, true),
    };

    let jiff_disamb = match disamb {
        DstDisambiguation::Compatible => Disambiguation::Compatible,
        DstDisambiguation::Earlier => Disambiguation::Earlier,
        DstDisambiguation::Later => Disambiguation::Later,
        DstDisambiguation::Reject => Disambiguation::Reject,
    };

    let ts = amb.disambiguate(jiff_disamb).map_err(|_| {
        if was_gap {
            TzError::NonExistentLocalTime
        } else {
            TzError::AmbiguousLocalTime
        }
    })?;

    let info = tz.to_offset_info(ts);
    Ok(ResolvedInstant {
        utc_timestamp: ts.as_second(),
        offset_seconds: info.offset().seconds(),
        is_dst: info.dst().is_dst(),
        tz_abbr: Some(CompactString::from(info.abbreviation())),
        was_gap,
        was_fold,
    })
}
