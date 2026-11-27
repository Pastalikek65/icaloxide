# icaloxide

**Blazing-fast, low-memory iCalendar (`RFC 5545` / `7986` / `9073` / `7953`) parser, writer, and recurring event engine written in Rust for Python.**

`icaloxide` is a ground-up Rust replacement for the Python `icalendar` + `recurring-ical-events` stack, built with a compact arena AST, SIMD line scanning, zero-regex byte-level temporal parsing, embedded IANA/Windows/VTIMEZONE timezone compilation, $O(1)$ fast-forward `RRULE` expansion, and lazy PyO3 proxy objects.

## Measured Performance (`--release` vs `icalendar 7.3.2` + `recurring-ical-events 3.8.2`)

| Workload | Upstream Python | `icaloxide` (Native) | Speedup | Python Heap (`upstream` vs `icaloxide`) |
| --- | ---: | ---: | ---: | ---: |
| Parse 10,000 events (3.74 MB) | 1,530.38 ms | **16.08 ms** | **95.2x** | 61.1 MB vs **0.16 KB** |
| Recurring `between()` 1y (200 series, 9,133 occs) | 434.91 ms | **5.68 ms** | **76.6x** | 32.9 MB vs **1.57 MB** |
| Recurring `count()` 5y (200 series) | 1,536.50 ms | **14.86 ms** | **103.4x** | 132.1 MB vs **0.69 KB** |
| Streaming `after()` next 20 (200 series) | 266.69 ms | **0.47 ms** | **568.8x** | 8.2 MB vs **4.43 KB** |
| Serialize 5,000 events (`to_ical`) | 497.03 ms | **5.27 ms** | **94.4x** | 16.5 MB vs **1.94 MB** |

Process RSS memory for parsing a 10,000-event calendar drops from **61.07 MB** (`icalendar`) to **21.77 MB** (`icaloxide`, **64.4% reduction**). See [benchmarks/RESULTS.md](benchmarks/RESULTS.md).

## Quick Start

### Modern Rust-First API

```python
import datetime
from zoneinfo import ZoneInfo
import icaloxide

cal = icaloxide.Calendar.from_ical(ics_bytes)
query = cal.query()

# Fast interval-pruned range query (GIL-free in Rust)
occurrences = query.between(
    datetime.date(2026, 1, 1),
    datetime.date(2026, 12, 31),
)
for occ in occurrences:
    print(occ.summary, occ.start, occ.end)

# Streaming chronological iterator over infinite RRULEs
for occ in query.after(datetime.datetime.now(datetime.timezone.utc), limit=10):
    print(occ.summary, occ.start)
```

### Drop-In Compatibility API (`icalendar` & `recurring_ical_events`)

```python
import datetime
from icaloxide.compat import Calendar, of

cal = Calendar.from_ical(ics_bytes)
events = of(cal).between(datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))
for ev in events:
    print(ev["SUMMARY"], ev["DTSTART"].dt)
```

## Architecture Highlights

- **Zero-Copy / Single-Pass Lexer**: Uses `memchr` SIMD scanning and fast-path line unfolding that borrows straight from input slices on unfolded lines while correctly unfolding UTF-8 sequences split across fold boundaries.
- **Compact Arena AST**: Interned `PropertyName`, `ParamName`, and `ComponentKind` enums eliminate thousands of heap string allocations; components live in a cache-friendly flat `Vec<ComponentNode>` arena.
- **Embedded IANA + CLDR Windows + `VTIMEZONE` Compiler**: Bundled `jiff` TZDB guarantees consistent timezone resolution on Windows, Linux, and macOS without external `tzdata`, plus compiled transition tables for custom `VTIMEZONE`s.
- **Interval-Pruned $O(1)$ Fast-Forward `RRULE` Engine**: Jumps directly to target windows when `COUNT` is absent, handles `RDATE`, dual-key `EXDATE`, `RECURRENCE-ID` (`RANGE=THISANDFUTURE`), `VEVENT`/`VTODO`/`VJOURNAL`/`VFREEBUSY`/`VALARM`, and enforces DoS iteration budgets.
- **Lazy Python Proxies**: `Calendar.from_ical(...)` parses in Rust with the GIL released (`allow_threads`) and exposes lightweight lazy `Component` / `Occurrence` handles.

## Roadmap Status

- [x] **Phase 0**: Workspace, CI/CD, licensing, dual-crate Rust + Maturin (`abi3-py39`) build
- [x] **Phase 1**: `icaloxide-core` (Lexer, RFC 5545/7986/9073/7953 AST, zero-regex temporals, Strict/Lenient parser, 75-octet writer, lazy PyO3 bindings)
- [x] **Phase 2**: IANA TZDB + CLDR Windows TZID mapping + `VTIMEZONE` compiler & transition cache
- [x] **Phase 3**: `icaloxide-rrule` (`RRULE`, `RDATE`, `EXDATE`, `RECURRENCE-ID`, `THISANDFUTURE`, interval pruning index, `at`/`between`/`after`/`count`)
- [x] **Phase 4**: Full modern Python API + `icalendar` / `recurring_ical_events` drop-in `compat` layer
- [x] **Phase 5**: Differential verification against `icalendar` + `recurring-ical-events` & fuzz/stress tests
- [x] **Phase 6**: Speed & peak RSS memory benchmarks (`benchmarks/RESULTS.md`)
- [x] **Phase 7**: Final release workflow, `CHANGELOG.md`, and GitHub publication

## Documentation

- [Architecture Deep-Dive](docs/architecture.md)
- [Migration Guide (`icalendar` & `recurring-ical-events`)](docs/migration-guide.md)
- [Benchmark Report](benchmarks/RESULTS.md)
- [Roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)

## Development

```bash
# Rust checks & tests
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace

# Python virtual environment, release build, lint & tests
uv venv
uv pip install -e ".[dev]"
uv run maturin develop --release
uv run ruff check python tests benchmarks
uv run pytest -v
```

## License

Dual-licensed under either [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE) at your option.
