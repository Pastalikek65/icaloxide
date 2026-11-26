# icaloxide Architecture & 7-Phase Development Roadmap

## Core Design Principles

1. **Compact Arena Memory Layout**: Instead of Python dictionaries of dictionaries per property and component, `icaloxide-core` stores components in a flat `Vec<ComponentNode>` arena (`ComponentId(u32)`), properties in contiguous slices, and standard names (`DTSTART`, `SUMMARY`, `TZID`, `VEVENT`, etc.) as `u16` enum variants with zero heap allocation.
2. **Zero-Regex Temporal Parsing**: `CalDate`, `CalTime`, `CalDateTime`, `CalDuration`, `CalPeriod`, and `UtcOffset` are parsed directly from ASCII bytes using Howard Hinnant's O(1) civil calendar algorithms.
3. **FFI Boundary Efficiency**: `Calendar.from_ical` accepts `bytes`, `bytearray`, `memoryview` (buffer protocol), `str`, or `os.PathLike`. Parsing runs in Rust with the GIL released. `Component` in Python is a thin frozen `(Arc<Calendar>, ComponentId)` handle that resolves fields lazily on demand.
4. **DoS & Adversarial Input Guards**: Configurable `max_depth`, `max_components`, and `max_line_bytes` limits in `ParseOptions` (plus `max_iterations` in `icaloxide-rrule`) prevent stack overflow and OOM attacks.

## Phase Breakdown

### Phase 0: Repository & Toolchain Scaffold
- Cargo workspace (`icaloxide-core`, `icaloxide-rrule`, `icaloxide-py`)
- Maturin `abi3-py39` packaging, strict Clippy pedantic lints, GitHub Actions CI

### Phase 1: `icaloxide-core` (Parser, Arena AST, Writer)
- `memchr` SIMD line scanning and raw-byte RFC 5545 unfolding (handles multi-byte UTF-8 characters split across fold lines before UTF-8 validation)
- Content-line tokenizer with quoted parameter handling and RFC 6868 (`^n`, `^^`, `^'`) parameter unescaping
- RFC 5545 + RFC 7986 + RFC 9073 + RFC 7953 known component/property/parameter interning
- Strict and Lenient parse modes with structured `ParseWarning` diagnostics
- RFC 5545 75-octet UTF-8-safe line folding writer

### Phase 2: Timezone & `VTIMEZONE` Engine
- Embedded IANA TZDB integration
- Unicode CLDR Windows TZID mapping (`Turkey Standard Time`, `W. Europe Standard Time`, etc.)
- `VTIMEZONE` (`STANDARD` / `DAYLIGHT`) compiler with precomputed transition cache
- `Calendar::add_missing_timezones()`

### Phase 3: `icaloxide-rrule` (Recurrence & Query Engine)
- Full RFC 5545 `RRULE` parser/validator and O(1) fast-forward arithmetic
- `RDATE`, `EXDATE`, `RECURRENCE-ID`, `RANGE=THISANDFUTURE`, DST wall-clock preservation
- Interval-pruned `CalendarQuery` (`at`, `between`, `after`, `count`, `all`)
- Support for `VEVENT`, `VTODO`, `VJOURNAL`, `VFREEBUSY`, `VALARM`, `VAVAILABILITY`

### Phase 4: Full Python API & `compat` Layer
- Complete modern Pythonic API + `.pyi` stubs (`py.typed`)
- Drop-in `icaloxide.compat.icalendar` and `icaloxide.compat.recurring_ical_events`

### Phase 5: Differential Verification & Fuzzing
- Cross-check test suite against Python `icalendar` and `recurring-ical-events`
- Property-based tests (`proptest`) and malformed `.ics` corpus

### Phase 6: Performance & Memory Benchmarks
- Rust Criterion microbenchmarks
- Python comparative benchmarks (throughput MB/s, query latency, peak RSS / `tracemalloc`)

### Phase 7: Documentation, Wheels & GitHub Publication
- Multi-platform release workflow, polished docs, `CHANGELOG.md`, GitHub push
