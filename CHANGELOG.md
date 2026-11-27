# Changelog

All notable changes to `icaloxide` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
