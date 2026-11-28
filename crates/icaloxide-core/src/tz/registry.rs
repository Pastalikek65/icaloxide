use std::sync::Arc;

use compact_str::CompactString;
use jiff::tz::TimeZone;
use rustc_hash::FxHashMap;

use crate::datetime::{CalDateTime, TimeZoneRef};
use crate::model::{Calendar, PropertyName};
use crate::tz::vtimezone::CompiledVTimezone;
use crate::tz::windows::{parse_fixed_offset_tzid, resolve_iana_alias, strip_vendor_prefix};
use crate::tz::{CalendarTimeZone, DstDisambiguation, ResolvedInstant, TzError};

/// Calendar-scoped timezone resolver combining embedded IANA TZDB,
/// Windows CLDR aliases, vendor prefix stripping, fixed offsets, and compiled `VTIMEZONE`s.
#[derive(Debug, Clone, Default)]
pub struct TzRegistry {
    vtimezones: FxHashMap<CompactString, Arc<CompiledVTimezone>>,
    default_tzid: Option<CompactString>,
}

impl TzRegistry {
    /// Build a [`TzRegistry`] by compiling all `VTIMEZONE` components and reading `X-WR-TIMEZONE`.
    pub fn from_calendar(cal: &Calendar) -> Self {
        let mut vtimezones = FxHashMap::default();
        for vtz in cal.timezones() {
            if let Ok(compiled) = CompiledVTimezone::compile(vtz) {
                vtimezones.insert(compiled.tzid.clone(), Arc::new(compiled));
            }
        }
        let default_tzid = cal
            .root()
            .property_text(&PropertyName::XWrTimeZone)
            .map(CompactString::from);

        Self {
            vtimezones,
            default_tzid,
        }
    }

    /// Return the `X-WR-TIMEZONE` declared at calendar level, if any.
    pub fn default_tzid(&self) -> Option<&str> {
        self.default_tzid.as_deref()
    }

    /// Return `true` if `tzid` is defined by an embedded `VTIMEZONE` component in this calendar.
    pub fn has_custom_vtimezone(&self, tzid: &str) -> bool {
        self.vtimezones.contains_key(tzid)
    }

    /// Resolve the calendar's `X-WR-TIMEZONE`, if present and resolvable.
    pub fn default_timezone(&self) -> Option<CalendarTimeZone> {
        self.default_tzid.as_deref().and_then(|id| self.resolve(id))
    }

    /// Resolve a `TZID` string using the precedence:
    /// 1. Calendar's compiled `VTIMEZONE`
    /// 2. Direct embedded IANA TZDB lookup
    /// 3. Windows CLDR / legacy alias -> IANA TZDB
    /// 4. Vendor prefix stripping (`/mozilla.org/.../Area/City`) -> IANA TZDB
    /// 5. Fixed offset (`UTC+03:00`, `Etc/GMT-3`, `+02:00`)
    pub fn resolve(&self, tzid: &str) -> Option<CalendarTimeZone> {
        let cleaned = tzid.trim().trim_matches('"');
        if cleaned.is_empty() {
            return None;
        }

        if let Some(custom) = self.vtimezones.get(cleaned) {
            return Some(CalendarTimeZone::Custom(Arc::clone(custom)));
        }

        if let Some(iana_name) = self.resolve_canonical_iana(cleaned) {
            if iana_name == "UTC" {
                return Some(CalendarTimeZone::Utc);
            }
            if let Ok(tz) = TimeZone::get(iana_name.as_str()) {
                return Some(CalendarTimeZone::Iana(tz));
            }
        }

        if let Some(off) = parse_fixed_offset_tzid(cleaned) {
            if off.seconds == 0
                && (cleaned.eq_ignore_ascii_case("UTC") || cleaned.eq_ignore_ascii_case("GMT"))
            {
                return Some(CalendarTimeZone::Utc);
            }
            return Some(CalendarTimeZone::FixedOffset(off));
        }

        None
    }

    /// Map `tzid` to a canonical IANA timezone name if one exists in the bundled TZDB.
    pub fn resolve_canonical_iana(&self, tzid: &str) -> Option<CompactString> {
        let cleaned = tzid.trim().trim_matches('"');
        if cleaned.is_empty() {
            return None;
        }
        if cleaned.eq_ignore_ascii_case("UTC") || cleaned.eq_ignore_ascii_case("Etc/UTC") {
            return Some(CompactString::const_new("UTC"));
        }
        if let Ok(tz) = TimeZone::get(cleaned) {
            if let Some(name) = tz.iana_name() {
                return Some(CompactString::from(name));
            }
            return Some(CompactString::from(cleaned));
        }
        if let Some(alias) = resolve_iana_alias(cleaned) {
            if alias == "UTC" {
                return Some(CompactString::const_new("UTC"));
            }
            if let Ok(tz) = TimeZone::get(alias) {
                return Some(CompactString::from(tz.iana_name().unwrap_or(alias)));
            }
        }
        if let Some(stripped) = strip_vendor_prefix(cleaned) {
            if stripped.eq_ignore_ascii_case("UTC") || stripped.eq_ignore_ascii_case("Etc/UTC") {
                return Some(CompactString::const_new("UTC"));
            }
            if let Ok(tz) = TimeZone::get(stripped) {
                return Some(CompactString::from(tz.iana_name().unwrap_or(stripped)));
            }
            if let Some(alias) = resolve_iana_alias(stripped) {
                if alias == "UTC" {
                    return Some(CompactString::const_new("UTC"));
                }
                if let Ok(tz) = TimeZone::get(alias) {
                    return Some(CompactString::from(tz.iana_name().unwrap_or(alias)));
                }
            }
        }
        None
    }

    /// Resolve a [`CalDateTime`] to a [`ResolvedInstant`].
    ///
    /// Floating datetimes use `default_timezone()` if set, otherwise return [`TzError::FloatingTimeZone`].
    pub fn resolve_datetime(
        &self,
        dt: &CalDateTime,
        disamb: DstDisambiguation,
    ) -> Result<ResolvedInstant, TzError> {
        match &dt.tz {
            TimeZoneRef::Utc => CalendarTimeZone::Utc.resolve_local(dt.date, dt.time, disamb),
            TimeZoneRef::TzId(id) => {
                let tz = self
                    .resolve(id.as_str())
                    .ok_or_else(|| TzError::UnknownTimeZone(id.clone()))?;
                tz.resolve_local(dt.date, dt.time, disamb)
            }
            TimeZoneRef::Floating => {
                if let Some(default_tz) = self.default_timezone() {
                    default_tz.resolve_local(dt.date, dt.time, disamb)
                } else {
                    Err(TzError::FloatingTimeZone)
                }
            }
        }
    }
}
