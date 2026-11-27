# icaloxide Architecture

`icaloxide` is organized as a three-crate Rust workspace paired with a thin Python facade and a drop-in compatibility layer.

```text
+---------------------------------------------------------------+
| Python API: icaloxide (native)  |  icaloxide.compat (drop-in) |
+---------------------------------------------------------------+
| crates/icaloxide-py   (PyO3 abi3-py39, lazy proxies, GIL-free)|
+-------------------------------+-------------------------------+
| crates/icaloxide-rrule        | crates/icaloxide-core         |
| - RRULE parser & O(1) ff      | - memchr SIMD byte unfolder   |
| - RDATE / EXDATE / OVERRIDES  | - Zero-regex temporals        |
| - prefix_max_end & BinaryHeap | - Flat ComponentArena AST     |
|   streaming AfterIter         | - Embedded jiff + VTIMEZONE   |
+-------------------------------+-------------------------------+
```

## 1. Zero-Copy / Single-Pass Lexer (`icaloxide-core::lexer`)

- `UnfoldIter` scans physical line breaks (`\r\n`, `\n`, `\r`) via `memchr::memchr2`.
- Non-folded lines decode directly from `&[u8]` to `Cow::Borrowed(&str)` with zero heap allocation.
- Folded lines concatenate raw byte slices *before* UTF-8 validation so multi-byte UTF-8 sequences split across fold lines by non-compliant clients still decode without error.
- `parse_content_line` handles quoted parameter values (`:`, `;`, `,` inside `"`), multi-valued parameters, and RFC 6868 (`^n`, `^^`, `^'`) escapes in a single pass.

## 2. Compact Arena AST (`icaloxide-core::model`)

- Instead of nested hash maps per property and component, components live in a flat `Vec<ComponentNode>` arena indexed by `ComponentId(u32)`.
- Standard names (`ComponentKind`, `PropertyName`, `ParamName`, `ValueType`) are parsed case-insensitively on a 64-byte stack buffer into compact enum variants with zero heap allocation.
- `Calendar` enforces configurable DoS limits (`max_depth`, `max_components`, `max_line_bytes`) in `ParseOptions`.

## 3. Timezone & `VTIMEZONE` Engine (`icaloxide-core::tz`)

- **Embedded IANA TZDB**: Built with `jiff` (`tzdb-bundle-always`), eliminating external `tzdata` dependencies on Windows and containers.
- **CLDR Windows & Vendor Normalization**: Maps Outlook/Exchange Windows TZIDs (`Turkey Standard Time`, `W. Europe Standard Time`, etc.) and Mozilla/Thunderbird prefixes (`/mozilla.org/...`) to canonical IANA identifiers, and handles POSIX-inverted `Etc/GMT+N` offsets.
- **Compiled `VTIMEZONE`**: Compiles `STANDARD`/`DAYLIGHT` observances (`DTSTART`, `RDATE`, `RRULE`) into a sorted `Vec<TzTransition>` for $O(\log n)$ offset and DST gap/fold resolution.

## 4. `RRULE` & Query Engine (`icaloxide-rrule`)

- **$O(1)$ Fast-Forward**: When `COUNT` is absent, `RRuleIter` computes the target period index mathematically (with a 1-period safety margin for `WKST`/`BYSETPOS`/timezone boundaries) instead of stepping from `DTSTART`.
- **DoS Iteration Budget**: `IterationBudget` bounds total inspected periods (`max_periods`) and consecutive empty periods (`max_empty_periods`) so unsatisfiable rules like `BYMONTH=2;BYMONTHDAY=30` abort deterministically.
- **Interval Pruning & Streaming `after()`**:
  - Single-instance components are sorted by `(start_utc, end_utc)` with a `prefix_max_end` array for $O(\log n)$ range pruning.
  - `AfterIter` uses a `BinaryHeap<Reverse<...>>` min-heap across singles and active series cursors, yielding occurrences chronologically in $O(S)$ memory across $S$ recurring series.

## 5. Lazy PyO3 Boundary (`icaloxide-py`)

- `Calendar.from_ical`, `CalendarQuery.at/between/count`, and `to_ical` release the Python GIL (`py.allow_threads`).
- `Component` and `Occurrence` are lightweight frozen handles (`Arc<Calendar>` + `ComponentId`) that materialize Python `datetime`, `list`, and `str` objects only for properties the caller reads.
