# Changelog

All notable changes to `icaloxide` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-11-27

### Fixed & Added

- **Cross-Year ISO `BYWEEKNO` Parity (`icaloxide-rrule`)**: Fixed `week_days_in_year` so ISO weeks spanning calendar year boundaries (`BYWEEKNO=1` / `BYWEEKNO=-1` / `WKST=SU`) include all 7 week days, plus `BYMONTHDAY` filtering on `BYWEEKNO` rules — verified 100% identical to `recurring-ical-events` / `dateutil.rrule`.
- **`vUTCOffset`, `vBinary`, and `vPeriod` Fixes (`icaloxide.compat`)**: Fixed `vUTCOffset.to_ical()` emitting RFC 5545 `+0300` (instead of `PT3H`), jCal `"utc-offset"` encoding/decoding (`+03:00`), and `vBinary.to_ical()` Base64 encoding.
- **Complete `v*`, `Component`, `Parameters`, Exception & Adapter Parity**: Added `from_ical`/`to_jcal`/`from_jcal` across all `v*` classes, `vAdr`/`vN`/`vOrg`/`ComponentFactory`/`TypesFactory`, `ICalParsingError`/`JCalParsingError` exception wrapping, `Component.__eq__` + metadata helpers, `Parameters` helpers, uppercase `DTSTART`/`DTEND`/`ACTION`/`TRIGGER` accessors, `AlarmTime.action`, and extensible `ComponentAdapter`/`EventAdapter`/`TodoAdapter`/`JournalAdapter`/`SelectComponents`/`OccurrenceID`.

## [0.3.0] - 2026-11-27

### Added

- **Upstream Bug Fixes & RFC Completeness Beyond `icalendar` / `recurring-ical-events`**
  - Legacy `CHARSET=ISO-8859-1` / `WINDOWS-1252` line decoding in `UnfoldIter` before UTF-8 fallback (beats `icalendar` #2078).
  - Proper semicolon-preserving `REQUEST-STATUS` parsing and serialization (`#2123`), stripped redundant `TZID` on `Z` UTC datetimes (`#2020`), and zero-length `PERIOD` strict validation (`#2087`).
  - Fixed UTC/zoned `EXDATE` / `RECURRENCE-ID` collisions with unrelated local wall-clock times (`recurring-ical-events` #306), and distinguished nominal-day `P1D` vs exact `PT24H` durations across DST (`#2193`).
  - Standalone component parsing (`Calendar::parse_component` / `Component.from_ical`), native Rust `busy_intervals()` & `free_intervals()`, `next_after()`, unbounded finite `count_all()`, and configurable DoS `max_iterations` / `max_periods` limits.
  - Complete `compat` parity: `Pages`/`Page` cursor pagination (`next_page_id`, `latest_start`), all `DateArgument` forms (`int`, `str`, 1–6 element tuples), multi-calendar `of([cal1, cal2])`, `Timezone.from_tzid`/`from_tzinfo`/`to_tz`/`get_transitions`, RFC 9074 `Alarms` (snooze/acknowledge/`active`), RFC 6321 xCal (`to_xcal`, `from_xcal`), RFC 9253 (`LINK`, `CONCEPT`, `REFID`), occurrence mutation helpers (`cancel_occurrence`, `modify_occurrence`, `complete_occurrence`), `enums`, `errors`, and `icaloxide` CLI (`view`, `validate`, `query`, `convert`).

## [0.2.0] - 2026-11-27

### Added

- **Full `icalendar` & `recurring-ical-events` Parity Upgrades**
  - Native Rust `CalendarBuilder`, `Calendar.used_tzids()`, `Calendar.missing_tzids()`, `Calendar.validate()`, `Calendar.with_uid()`, `freebusies`, and `availabilities`.
  - `RDATE;VALUE=PERIOD` custom occurrence duration support, full `RANGE=THISANDFUTURE` shifted/split-series semantics, `skip_bad_series` with `suppressed_errors`, `VALARM` streaming in `after()`, `VAVAILABILITY`/`AVAILABLE` query indexing, plus `first()`, `all()`, and `paginate()`.
  - Full `compat` `vRecur` dictionary interface, `Parameters`, `v*` property types, full Python `dict` protocol on `Component`, ergonomic `Event`/`Todo`/`Alarm` factories, and RFC 7265 jCal (`to_jcal`, `to_json`, `from_jcal`).

## [0.1.0] - 2026-11-27

### Added

- **Core Parser, Arena AST & Writer (`icaloxide-core`)**
  - SIMD-accelerated `memchr` line scanning and zero-copy fast-path RFC 5545 line unfolding (`UnfoldIter`), unfolding raw bytes before UTF-8 validation so multi-byte characters split across fold boundaries decode cleanly.
  - Byte-level, zero-regex RFC 5545 temporal primitives (`CalDate` with Howard Hinnant $O(1)$ civil calendar math, `CalTime`, `CalDateTime`, `CalDuration`, `CalPeriod`, `UtcOffset`).
  - Compact flat `ComponentArena` with interned `ComponentKind`, `PropertyName`, and `ParamName` enums covering RFC 5545, RFC 7986 (`NAME`, `COLOR`, `CONFERENCE`, `REFRESH-INTERVAL`, `IMAGE`, `SOURCE`), RFC 9073 (`VLOCATION`, `VRESOURCE`, `PARTICIPANT`), RFC 7953 (`VAVAILABILITY`, `AVAILABLE`), and `X-WR-*` vendor properties.
  - `Strict` and `Lenient` parsing modes with `ParseWarning` diagnostics, multi-calendar `Calendar::parse_all`, and configurable DoS bounds (`max_depth`, `max_components`, `max_line_bytes`).
  - RFC 5545 UTF-8-safe 75-octet line-folding serializer (`to_ical`).
- **Timezone & `VTIMEZONE` Engine (`icaloxide-core::tz`)**
  - Embedded IANA TZDB via `jiff` (`tzdb-bundle-always`) requiring no external `tzdata` at runtime.
  - Unicode CLDR Windows/Outlook TZID mapping (90+ entries), vendor prefix stripping (`/mozilla.org/...`), and POSIX-aware fixed-offset parsing (`UTC+03:00`, `Etc/GMT+3`).
  - RFC 5545 `VTIMEZONE` (`STANDARD` / `DAYLIGHT` + `RRULE` / `RDATE`) compiler (`CompiledVTimezone`) with $O(\log n)$ transition table and DST gap/fold disambiguation (`Compatible`, `Earlier`, `Later`, `Reject`).
  - `Calendar::add_missing_timezones()` generator.
- **Recurrence & Query Engine (`icaloxide-rrule`)**
  - Complete RFC 5545 `RRULE` parser and $O(1)$ fast-forward expander supporting all 7 frequencies, `INTERVAL`, `COUNT`, `UNTIL`, `WKST`, `BY*`, and `BYSETPOS`, guarded by `IterationBudget`.
  - Dual-key `EXDATE`, `RDATE`, `RECURRENCE-ID` overrides (`SEQUENCE` precedence), `RANGE=THISANDFUTURE`, `STATUS:CANCELLED` filtering, and DST wall-clock preservation.
  - Interval-pruned `CalendarQuery` (`at_date`, `at_instant`, `between`, `count_between`, and $O(S)$-memory `BinaryHeap` streaming `after`) across `VEVENT`, `VTODO`, `VJOURNAL`, `VFREEBUSY`, and `VALARM`.
- **Python Bindings & Compatibility Layer (`icaloxide-py`, `icaloxide.compat`)**
  - `abi3-py39` PyO3 bindings releasing the GIL during parse/query/serialize (`allow_threads`) and exposing lazy `Component` / `Occurrence` proxies plus `OccurrenceIterator` and PEP 561 `.pyi` stubs.
  - Drop-in `icaloxide.compat` shims for `icalendar` (`Calendar`, `Event`, `Todo`, `Alarm`, `vDDDTypes`, `vText`) and `recurring_ical_events` (`of()`, `at()`, `between()`, `after()`, `count()`).
- **Verification & Benchmarks**
  - Differential test suite verifying parity against `icalendar 7.3.2` + `recurring-ical-events 3.8.2`, plus 250-seed fuzz/stress suite and release benchmarks (`benchmarks/RESULTS.md`).
