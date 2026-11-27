# icaloxide Benchmark Results

Measured in release mode (`uv run maturin develop --release`) on Windows (CPython 3.14.7, `rustc 1.99.1`), comparing `icaloxide` (`0.7.0`) against `icalendar` (`7.3.2`) + `recurring-ical-events` (`3.8.2`).

## Speed & Python Heap Allocation (`tracemalloc`)

| Workload | `icalendar` / `recurring-ical-events` | `icaloxide` (Native) | `icaloxide.compat` | Speedup (Native / Compat) | Peak Python Heap (`upstream` vs measured `icaloxide`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Parse 1,000 events (380 KB) | 205.21 ms | **2.55 ms** | **1.99 ms** | **80.6x / 103.3x** | 6,114 KB vs **0.16 KB** |
| Parse 10,000 events (3.74 MB) | 2,287.92 ms | **16.69 ms** | **19.20 ms** | **137.1x / 119.1x** | 61,054 KB vs **0.16 KB** |
| Recurring `between()` 1y (200 series -> 9,133 occs) | 508.43 ms | **7.25 ms** | **10.51 ms** | **70.1x / 48.4x** | 32,902 KB vs **1,570 KB** |
| Recurring `count()` 5y (200 series) | 1,762.67 ms | **17.47 ms** | **17.42 ms** | **100.9x / 101.2x** | 132,114 KB vs **0.38 KB** |
| Streaming `after()` next 20 (200 series) | 302.50 ms | **0.59 ms** | **0.52 ms** | **513.5x / 579.9x** | 8,224 KB vs **4.51 KB** |
| Serialize 5,000 events (`to_ical`) | 557.16 ms | **6.93 ms** | **8.26 ms** | **80.4x / 67.4x** | 16,459 KB vs **1,935 KB** |
| Parse + Mutate 1,000 events + `to_ical` (`compat`) | 293.31 ms | — | **21.81 ms** | **— / 13.5x** | 7,832 KB vs **4,163 KB** |

## Process RSS Memory Delta (`psutil`, 10,000-event 3.74 MB calendar)

- `icalendar.Calendar.from_ical`: **+61.32 MB** RSS
- `icaloxide.Calendar.from_ical`: **+21.76 MB** RSS (**64.5% lower total process memory**)
- `icaloxide.compat.Calendar.from_ical`: **+25.62 MB** RSS (**58.2% lower total process memory**)

## Reproduce

```bash
uv run maturin develop --release
uv run --no-sync python benchmarks/bench.py
```
