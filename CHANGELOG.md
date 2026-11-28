# Changelog

All notable changes to `icaloxide` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-11-28

### Bug Fixes (Comprehensive Audit & Extended Differential Verification)

- **RRULE Filter Completeness (`icaloxide-rrule`)**:
  - Fixed `FREQ=YEARLY;BYMONTHDAY=...` (without `BYMONTH`) expanding across all 12 months instead of only `DTSTART`'s month.
  - Fixed `FREQ=WEEKLY` with `BYMONTHDAY` or `BYYEARDAY` (and omitted `BYDAY`) wrongly filtering dates to `DTSTART`'s weekday.
- **`UNTIL` Date & Naive Comparison Semantics (`icaloxide-rrule`)**:
  - Unified `resolve_until_utc` across both `between()` and streaming `after()`: `UNTIL=YYYYMMDD` on a timed `DTSTART` resolves to `00:00:00` of that date, and naive `DATE-TIME` `UNTIL` on a `TZID` `DTSTART` compares against floating UTC wall clock per RFC 5545 / upstream parity.
- **Duplicate Master `SEQUENCE` Selection & Absolute `VALARM` Deduplication (`icaloxide-rrule`)**:
  - Updated `classify_component_group` to select the winning master component by highest `SEQUENCE` (instead of blindly taking the first component).
  - Extracted absolute `DATE-TIME` `VALARM` triggers once per winning master/override instead of re-emitting per recurrence instance or losing alarms whose parent falls outside the relative pad window (respecting `skip_cancelled`).
- **`VJOURNAL` & Undated `VTODO` Occurrence Serialization (`icaloxide-py`, `icaloxide.compat`)**:
  - Prevented synthetic `DTEND` emission on `VJOURNAL` occurrences in `PyOccurrence::to_ical`, `_ensure_materialized`, `decoded()`, and `get()`, and omitted synthetic `DTSTART`/`DUE` on undated `VTODO::to_ical()`.
- **jCal, Custom `VTIMEZONE` `tzinfo`, and `add_missing_timezones()` Parity (`icaloxide.compat`)**:
  - Fixed jCal `REQUEST-STATUS` array serialization/deserialization and dropped redundant `ENCODING` param on binary `jCal` properties (`ATTACH`) for full `icalendar.Calendar.from_jcal()` interop.
  - Replaced OS-locale `.timestamp()` in `_CompiledVTimezoneTzInfo` with OS-timezone-independent epoch math supporting DST fall-back `fold=0`/`fold=1`.
  - Updated `Calendar.add_missing_timezones()` to mutate in-place (for both lazy and materialized calendars) and return `self`.
- **Verification**: Added `tests/test_audit_v09.py` covering all regression cases plus an 80-case extended differential fuzzer (`MINUTELY`, `SECONDLY`, `BYSETPOS`, leap-day, extreme timezones, long fast-forward jumps).

## [0.8.0] - 2026-11-28

### Bug Fixes (Uncovered via Differential Fuzzing & Torture Testing)

- **DST Fall-Back `fold=1` & Spring-Forward Wall-Clock Parity (`icaloxide-py`)**:
  - Added `cal_datetime_to_py_with_utc` so occurrences landing in a DST fall-back fold preserve `fold=1` (preventing negative `end < start` durations during repeated wall-clock hours in IANA zones and computing exact post-transition offsets for custom `VTIMEZONE`s), while preserving wall-clock `fold=0` `ZoneInfo` construction across spring-forward gaps.
- **`YEARLY;INTERVAL>1;BYWEEKNO` Cross-Year Spillover (`icaloxide-rrule`)**:
  - Added `byweekno_dates_in_year` so `FREQ=YEARLY` with `BYWEEKNO` includes late-December week-1 spillover and early-January previous-year final-week spillover strictly inside each stepped calendar year, even for `INTERVAL > 1`.
- **Final `UNTIL` Period Draining (`icaloxide-rrule`)**:
  - Fixed `RRuleIter::next()` and `AfterIter` (`AdvanceRuleCursor` / `AdvanceSplitCursor`) checking `finished` before draining remaining queued candidates in `pending`, ensuring the final `UNTIL` period emits every matching occurrence on both `between()` and streaming `after()`.
- **`pickle` & `copy.deepcopy` Support (`icaloxide-py`, `icaloxide.compat`)**:
  - Added `module = "icaloxide._icaloxide"` on PyO3 classes, `_unpickle_calendar` / `__reduce__` on `Calendar` and `Component` (Rust), and `__deepcopy__` / `__reduce__` on `compat.Component` (preserving zero-copy lazy cloning with isolated mutation and full pickle roundtripping).
- **Query Bounds & Extreme Timezone Semantics (`icaloxide-rrule`, `icaloxide.compat`)**:
  - Aligned `compat.recurring_ical_events.between()` / `count()` exclusive date stop bounds with upstream `recurring-ical-events` while keeping `at(date)` supporting single-day tuples/strings (`(y, m, d)`, `"YYYYMMDD"`).
  - Fixed `CalendarQuery::between` / `overlaps_window` for point-in-time queries (`start == end`).
  - Updated naive `CalendarQuery::at_date()` to query a `[-15h, +15h]` padded UTC window and filter by component local wall clock so extreme offsets (`Pacific/Kiritimati` `+14:00`, `Pacific/Niue` `-11:00`) match accurately.
- **Tolerant Malformed Series & Windows `X-WR-TIMEZONE` (`icaloxide-rrule`, `icaloxide.compat`)**:
  - Supported `RRULE:COUNT=0` (emitting master `DTSTART`), normalized backwards `DTEND < DTSTART` components without dropping/crashing, added `include_undated: bool` opt-in on `Calendar.query()` (default `False`; enabled by `compat.recurring_ical_events` for undated `VTODO`s with `start=None`/`end=None`), and exported `resolve_tzid()` so Windows CLDR names in `X-WR-TIMEZONE` resolve to canonical `ZoneInfo`.
- **jCal `period` Serialization (`icaloxide.compat`)**:
  - Fixed `_prop_to_jcal` and `vPeriod.from_jcal` so `RDATE;VALUE=PERIOD` and `FREEBUSY` period lists emit RFC 7265 `"period"` entries and parse both string (`"s/e"`) and 2-element list (`["s", "e"]`) jCal periods.
- **Verification**: Added relentless torture & 150-case differential fuzzing suite in `tests/test_torture_v08.py`.

## [0.7.0] - 2026-11-27

### Performance & Parity

- **PyO3 & Compat Hot-Path Acceleration (`icaloxide-py`, `icaloxide.compat`)**:
  - Cached `ZoneInfo` and fixed-offset `datetime.timezone` instances inside `PyDateTimeTypes` so repeated timezone conversions reuse cached Python tzinfo objects.
  - Added `Component.raw_properties_compact()` (returning `None` for param-less properties) and `fold_ical_lines()` backed by Rust's RFC 5545 75-octet folder.
  - Eliminated eager `list`/`_TrackedSubcomponents`/`Parameters` allocations on lazy `Component`/`Occurrence` construction via `_wrap_rust_occurrence_fast` and lazy `None` parameters; cut `compat.between()` (9,133 occurrences) from 16.68 ms to **10.51 ms** (**48.4x** vs upstream) and `Parse + Mutate 1,000 events + to_ical` from 39.97 ms to **21.81 ms** (**13.5x** vs upstream).
- **RFC 5545 Span Semantics & Full `icalendar 7.x` Constructors (`icaloxide.compat.icalendar`)**:
  - Fixed `Event.end` and `Event.duration` when `DTEND`/`DURATION` are omitted (`start + 1 day` / `timedelta(days=1)` for all-day `VALUE=DATE`; `start` / `timedelta(0)` for timed events), and enforced mutual exclusion between `DTEND`/`DUE` and `DURATION` on `.end` / `.duration` / `.set_end()` / `.set_duration()`.
  - Expanded `.new(...)` constructors across `Component`, `Calendar`, `Event`, `Todo`, `Journal`, `Alarm`, `FreeBusy`, `Availability`, and `Available` to accept `icalendar 7.x` keyword arguments (`organization`/`language` PRODID synthesis, `uuid.UUID`, `categories`, `status`, `stamp`, `subcomponents`, `components`, etc.).
  - Added `vCalAddress.new(...)` plus `email`, `name`, `CN`, `CUTYPE`, `DELEGATED_FROM`, `DELEGATED_TO`, `DIR`, `PARTSTAT`, `ROLE`, `RSVP`, `SENT_BY`; `vUri` param properties & `uri`; `vWeekday` (`relative`, `weekday`, `week_days`); `vDatetime.is_utc()` / `vTime.is_utc()`; `vMonth.leap`; `vPeriod` params & `ical_value`; `vDDDTypes.dts` / `to_property_type()`; `vBoolean.BOOL_MAP` and `.ical_value` across scalar/compound value wrappers.
- **`recurring_ical_events` Adapter & Occurrence Parity (`icaloxide.compat.recurring_ical_events`)**:
  - Added tuple/equality/hash support (`count`, `index`, `__iter__`, `__eq__`, `__hash__`) to `OccurrenceID`.
  - Added `component_name`, `uid`, `id`, `has_alarm`, `recurrence_ids`, `is_in_span`, and recurrence-stripping `as_component()` to `Occurrence`.
  - Expanded `ComponentAdapter(component)` instance API (`ATTRIBUTES_TO_DELETE_ON_COPY`, `start`, `end`, `raw_start`, `raw_end`, `duration`, `uid`, `sequence`, `rrules`, `rdates`, `exdates`, `span`, `alarms`, `has_recurrence_rules()`, `is_modification()`, `this_and_future()`, `is_in_span()`, `as_component()`, `collect_series_from()`) and exposed `CalendarQuery.ComponentsWithName`.
- **Verification**: Added `tests/test_red_team_v07.py` and refreshed `benchmarks/RESULTS.md` with release measurements.

## [0.6.0] - 2026-11-27

### Fixed & Added

- **Native Rust Sorted Serialization (`icaloxide-core`, `icaloxide-py`)**:
  - Added `write_calendar_sorted` / `write_component_ordered` and `to_ical(sorted=True)` on `Calendar` and `Component` so unmaterialized `compat.Calendar.to_ical(sorted=True)` executes directly in Rust without Python materialization.
- **Full `icalendar 7.x` Property Setters/Deleters & API Parity (`icaloxide.compat`)**:
  - Added property setters and deleters across `Component`, `Event`, `Todo`, `Journal`, `Alarm`, `TimezoneStandard`, `TimezoneDaylight`, `Availability`, and `Calendar` (`summary`, `description`, `location`, `status`, `priority`, `sequence`, `categories`, `attendees`, `rrules`, `rdates`, `exdates`, `calendar_name`, `method`, `TZOFFSETTO`, etc.).
  - Flattened `ev.exdates` and `ev.rdates` to match `icalendar 7.3.2` (`list[date | datetime]` and `list[tuple[date | datetime, timedelta | None]]`).
  - Updated `Component.decoded(name)` without a `default` argument to raise `KeyError` (matching `icalendar 7.3.2`).
  - Enabled `Calendar.from_ical(vevent)` standalone component parsing in lenient mode (`strict=False`), added `Component.errors`, `Available.example()`, `Parameters.canonical_order`, `vPeriod.overlaps`/`dt`, `vRecur` dict helpers, `vDDDTypes.td`/shortcuts, `vText` shortcuts, `vBinary.base64data`, and `vUTCOffset.format()`.
  - Updated `compat.recurring_ical_events` with `at(date=...)`, `OccurrenceID.from_string`/`from_occurrence`, and `X-WR-TIMEZONE` localization for floating occurrences.
- **Verification**: Added `tests/test_red_team_v06.py`.

## [0.5.1] - 2026-11-27

### Fixed & Added

- **100% `icalendar 7.3.2` Top-Level Parity & `compat` Type Fidelity (`icaloxide.compat`)**:
  - Updated `Component.to_ical(sorted: bool = True)` so `to_ical(sorted=True)` / `to_ical(sorted=False)` match `icalendar.Component.to_ical()` instead of raising `TypeError`.
  - Updated `Event.alarms` / `Todo.alarms` to return an `Alarms` object (matching `icalendar 7.3.2` `.times`, `.active`, `.acknowledge_until`, `.snooze_until`), while implementing `__len__`/`__getitem__`/`__iter__` on `Alarms` for backwards-compatible sequence indexing (`ev.alarms[0]`).
  - Wrapped `EXDATE`/`RDATE` lists as `vDDDLists` and `CATEGORIES`/`RESOURCES` lists as `vCategory` (with full `.dts`/`.cats`/`.params`, indexing, list equality, and `decoded()` unwrapping).
  - Fixed RFC 7265 jCal `_prop_to_jcal` so `ATTENDEE`/`ORGANIZER` emit `"cal-address"` and `URL`/`ATTACH`/`LINK`/`CONFERENCE`/`IMAGE` emit `"uri"`.
  - Added `VPROPERTY` and `__version_tuple__` (`84/84` `icalendar.__all__` parity) plus `Occurrence`, `Series`, `AlarmOccurrence`, `AbsoluteAlarmAdapter`, `AlarmSeriesRelativeToStart`, and `AlarmSeriesRelativeToEnd` shims in `icaloxide.compat.recurring_ical_events`.
- **Verification**: Added `tests/test_red_team_v051.py` verifying full jCal property-level equality, `to_ical(sorted=...)`, `ev.alarms`, `vDDDLists`/`vCategory`, `84/84` `__all__` parity, and `recurring_ical_events` inner shims.

## [0.5.0] - 2026-11-27

### Fixed & Added

- **Cross-Timezone `RDATE`, Spring-Forward Hourly DST & Post-2100 `COUNT` `all()` (`icaloxide-rrule`)**:
  - Added per-entry `CalendarTimeZone` resolution (`RDateEntry.tz`) and `cand_tz` propagation through `apply_this_and_future_shift`, `expand_master_window`, `push_master_statics`, and `build_master`, fixing `RDATE;TZID=...` differing from `DTSTART`'s timezone.
  - Updated deduplication in `expand_master_window` and `OwnedSeriesStream` to key on `(start_utc, local_date, local_time)`, preventing dropped `FREQ=HOURLY` occurrences across DST spring-forward transitions.
  - Added `CompiledSeries::is_finite()` and updated `CalendarQuery::all()` so finite `COUNT` `RRULE`s beyond year 2100 return all occurrences cleanly.
- **Live `subcomponents`, `.dt`, `vRecur` Mutations, Binary `ATTACH` & `Occurrence` `TZID` Params (`icaloxide.compat`)**:
  - Replaced detached `list` copies in `Component.subcomponents` with `_TrackedSubcomponents` so `.clear()`, `.remove()`, `.append()`, item assignment, etc. mark parent components dirty and persist to `to_ical()`.
  - Added live `_on_mutate` callbacks to `vDDDTypes.dt` and `vRecur` mutating methods so in-place edits (`ev["DTSTART"].dt = ...`, `ev["RRULE"]["COUNT"] = [9]`) persist directly into serialized output.
  - Decoded `VALUE=BINARY` / `ENCODING=BASE64` properties (`ATTACH`) as `vBinary` / raw `bytes` (not `vUri` / base64 text).
  - Preserved `TZID` and `VALUE=DATE` parameters on `Occurrence` `DTSTART` / `DTEND` / `DUE` wrappers.
- **Full `compat.icalendar` Re-Exports & Submodule Shims (`compat.prop`, `compat.cal`, `compat.param`)**:
  - Re-exported `Alarms`, `AlarmTime`, `enums`, `errors`, `vAdr`, `vN`, `vOrg`, `ComponentFactory`, `TypesFactory`, `q_join`, `q_split`, `use_pytz`, `__version__`, `version`, and `version_tuple` directly on `icaloxide.compat.icalendar`.
  - Added `Component.parse`, `Component.types_factory`, and `.example()` factories across `Event`, `Todo`, `Journal`, `Alarm`, `Timezone`, `FreeBusy`, and `Availability`, plus standalone `compat.prop`, `compat.cal`, and `compat.param` modules.
- **Benchmarks & Verification**:
  - Added `tests/test_red_team_v05.py` covering all 7 red-team bugs and `compat` API parity.
  - Added a real-world `Parse + Mutate 1,000 events + to_ical` workload to `benchmarks/bench.py` and updated `benchmarks/RESULTS.md` with fresh `v0.5.0` Native + `compat` measurements.

## [0.4.0] - 2026-11-27

### Fixed & Added

- **Root-Cause `icaloxide.compat` Materialization & Live Mutations (`icaloxide.compat`)**:
  - Eliminated $N^2$ duplication and per-instance parameter clobbering on multi-valued properties (`ATTENDEE`, `EXDATE`, `RDATE`, etc.) by adding `Component.raw_properties()` and `Component.get_all_with_params()` in `icaloxide-py`.
  - Added live `_parent` backlinks, cached `_children_cache`, and `_mark_dirty()` propagation plus `Parameters` mutation callbacks so in-place edits on child components (`cal.events[0]["SUMMARY"] = ...`) and `.params` (`ev["ATTENDEE"].params["CN"] = ...`) persist cleanly through `cal.to_ical()`.
  - Preserved all root `VCALENDAR` properties (`METHOD`, `CALSCALE`, `COLOR`, `X-*`) during lazy materialization via `Calendar.root`.
  - Wrapped parsed text/URI/CalAddress properties as `vText`, `vCalAddress`, and `vUri` so `.params` matches `icalendar 7.3.2`.
- **Cross-Timezone `DTEND`, `UNTIL < DTSTART`, and Unbounded `all()` / `paginate()` (`icaloxide-rrule`, `icaloxide-py`)**:
  - Preserved distinct `DTEND` `TZID`s (e.g., JFK → IST flights) on `Occurrence.end_local` instead of overwriting with `DTSTART`'s timezone.
  - Emitted 0 occurrences when `RRULE` `UNTIL < DTSTART` (matching RFC 5545 and `recurring-ical-events`).
  - Removed the `1900..2100` cap from finite `CalendarQuery.all()`, argless `after()`, and `paginate()` defaults using `min_start_floor()` and `bounded_extents()`.
- **Extended Rust `CalendarBuilder`**: Added `set_property()`, `add_journal()`, and `add_alarm()`.
- **Verification**: Added `tests/test_red_team_v04.py` covering all red-team regressions and real-world client payloads (Google Calendar, Outlook/Exchange, Apple Calendar, Mozilla Thunderbird, Lotus Notes).

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
