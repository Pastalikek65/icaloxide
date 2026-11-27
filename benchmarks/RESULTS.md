# icaloxide Benchmark Results

Measured in release mode (`uv run maturin develop --release`) on Windows (CPython 3.14.7, `rustc 1.99.1`), comparing `icaloxide` (`0.1.0`) against `icalendar` (`7.3.2`) + `recurring-ical-events` (`3.8.2`).

## Speed & Python Heap Allocation (`tracemalloc`)

| Workload | `icalendar` / `recurring-ical-events` | `icaloxide` (Native) | `icaloxide.compat` | Speedup (Native / Compat) | Peak Python Heap (`upstream` vs `icaloxide`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Parse 1,000 events (380 KB) | 149.18 ms | **1.65 ms** | **1.56 ms** | **90.4x / 95.6x** | 6,114 KB vs **0.16 KB** |
| Parse 10,000 events (3.74 MB) | 1,530.38 ms | **16.08 ms** | **15.99 ms** | **95.2x / 95.7x** | 61,054 KB vs **0.16 KB** |
| Recurring `between()` 1y (200 series -> 9,133 occs) | 434.91 ms | **5.68 ms** | **10.43 ms** | **76.6x / 41.7x** | 32,910 KB vs **1,570 KB** |
| Recurring `count()` 5y (200 series) | 1,536.50 ms | **14.86 ms** | **14.45 ms** | **103.4x / 106.3x** | 132,117 KB vs **0.69 KB** |
| Streaming `after()` next 20 (200 series) | 266.69 ms | **0.47 ms** | **0.40 ms** | **568.8x / 672.6x** | 8,232 KB vs **4.43 KB** |
| Serialize 5,000 events (`to_ical`) | 497.03 ms | **5.27 ms** | **5.25 ms** | **94.4x / 94.7x** | 16,459 KB vs **1,935 KB** |

## Process RSS Memory Delta (`psutil`, 10,000-event 3.74 MB calendar)

- `icalendar.Calendar.from_ical`: **+61.07 MB** RSS
- `icaloxide.Calendar.from_ical`: **+21.77 MB** RSS (**64.4% lower total process memory**)
- `icaloxide.compat.Calendar.from_ical`: **+22.39 MB** RSS (**63.3% lower total process memory**)

## Reproduce

```bash
uv run maturin develop --release
uv run --no-sync python benchmarks/bench.py
```
