# icaloxide

**Blazing-fast, low-memory iCalendar (`RFC 5545` / `7986` / `9073` / `7953`) parser, writer, and recurring event engine written in Rust for Python.**

`icaloxide` is a ground-up Rust replacement for the Python `icalendar` + `recurring-ical-events` stack, built with a compact arena AST, SIMD line scanning, zero-regex byte-level temporal parsing, and lazy PyO3 proxy objects.

## Architecture Highlights

- **Zero-Copy / Single-Pass Lexer**: Uses `memchr` SIMD scanning and fast-path line unfolding that borrows straight from input slices on unfolded lines while correctly unfolding UTF-8 sequences split across fold boundaries.
- **Compact Arena AST**: Interned `PropertyName`, `ParamName`, and `ComponentKind` enums eliminate thousands of heap string allocations; components live in a cache-friendly flat `Vec<ComponentNode>` arena.
- **Lazy Python Proxies**: `Calendar.from_ical(...)` parses entirely in Rust with the GIL released (`allow_threads`) and exposes lightweight lazy `Component` handles—Python objects are materialized only for fields you actually read.
- **UTF-8-Safe 75-Octet Serializer**: Generates standards-compliant `\r\n` folded iCalendar streams without ever splitting multi-byte UTF-8 characters across fold boundaries.
- **Strict & Lenient Parsing Modes**: Strict RFC 5545 validation or real-world lenient recovery (Google Calendar, Apple iCloud, Outlook/Exchange, Thunderbird) with structured `ParseWarning` diagnostics.

## Roadmap Status

- [x] **Phase 0**: Workspace, CI/CD, licensing, dual-crate Rust + Maturin (`abi3-py39`) build
- [x] **Phase 1**: `icaloxide-core` (Lexer, RFC 5545/7986/9073/7953 AST, zero-regex temporals, Strict/Lenient parser, 75-octet writer, lazy PyO3 bindings)
- [ ] **Phase 2**: IANA TZDB + CLDR Windows TZID mapping + `VTIMEZONE` compiler & transition cache
- [ ] **Phase 3**: `icaloxide-rrule` (`RRULE`, `RDATE`, `EXDATE`, `RECURRENCE-ID`, `THISANDFUTURE`, interval pruning index, `at`/`between`/`after`/`count`)
- [ ] **Phase 4**: Full modern Python API + `icalendar` / `recurring_ical_events` drop-in `compat` layer
- [ ] **Phase 5**: Differential verification against `icalendar` + `recurring-ical-events` & fuzzing
- [ ] **Phase 6**: Criterion & Python speed / peak RSS memory benchmarks
- [ ] **Phase 7**: Documentation polish, wheels release & GitHub publication

See [docs/ROADMAP.md](docs/ROADMAP.md) for the complete technical design.

## Development

```bash
# Rust checks & tests
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace

# Python virtual environment & extension build
uv venv
uv pip install maturin pytest
uv run maturin develop
uv run pytest -v
```

## License

Dual-licensed under either [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE) at your option.
