# icaloxide Benchmark Results

Measured in release mode (`uv run maturin develop --release`) on Windows (CPython 3.14.7, `rustc 1.99.1`), comparing `icaloxide` (`0.5.0`) against `icalendar` (`7.3.2`) + `recurring-ical-events` (`3.8.2`).

## Speed & Python Heap Allocation (`tracemalloc`)

| Workload | `icalendar` / `recurring-ical-events` | `icaloxide` (Native) | `icaloxide.compat` | Speedup (Native / Compat) | Peak Python Heap (`upstream` vs measured `icaloxide`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Parse 1,000 events (380 KB) | 186.28 ms | **1.88 ms** | **1.80 ms** | **99.3x / 103.3x** | 6,114 KB vs **0.16 KB** |
| Parse 10,000 events (3.74 MB) | 1,924.47 ms | **18.55 ms** | **19.27 ms** | **103.8x / 99.9x** | 61,054 KB vs **0.16 KB** |
| Recurring `between()` 1y (200 series -> 9,133 occs) | 500.45 ms | **7.56 ms** | **16.68 ms** | **66.2x / 30.0x** | 32,912 KB vs **1,570 KB** |
| Recurring `count()` 5y (200 series) | 1,786.30 ms | **16.00 ms** | **15.88 ms** | **111.6x / 112.5x** | 132,125 KB vs **0.69 KB** |
| Streaming `after()` next 20 (200 series) | 280.73 ms | **0.46 ms** | **0.46 ms** | **613.9x / 608.8x** | 8,234 KB vs **4.24 KB** |
| Serialize 5,000 events (`to_ical`) | 482.31 ms | **5.89 ms** | **5.73 ms** | **81.9x / 84.2x** | 16,459 KB vs **1,935 KB** |
| Parse + Mutate 1,000 events + `to_ical` (`compat`) | 243.42 ms | — | **39.97 ms** | **— / 6.1x** | 7,832 KB vs **5,070 KB** |

## Process RSS Memory Delta (`psutil`, 10,000-event 3.74 MB calendar)

- `icalendar.Calendar.from_ical`: **+61.31 MB** RSS
- `icaloxide.Calendar.from_ical`: **+21.73 MB** RSS (**64.6% lower total process memory**)
- `icaloxide.compat.Calendar.from_ical`: **+25.50 MB** RSS (**58.4% lower total process memory**)

## Reproduce

```bash
uv run maturin develop --release
uv run --no-sync python benchmarks/bench.py
```
