"""Speed and memory benchmark suite: icaloxide vs icalendar + recurring-ical-events."""

from __future__ import annotations

import datetime
import gc
import itertools
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Callable

import icalendar as up_ical
import icaloxide
import recurring_ical_events as up_rie
from icaloxide.compat import Calendar as OxCompatCalendar
from icaloxide.compat import of as ox_of


def generate_flat_ics(num_events: int) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//icaloxide//bench//EN",
        "NAME:Benchmark Calendar",
    ]
    base = datetime.datetime(2026, 1, 1, 8, 0, tzinfo=datetime.timezone.utc)
    for i in range(num_events):
        dt = base + datetime.timedelta(hours=i)
        dt_end = dt + datetime.timedelta(minutes=45)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:bench-flat-{i}@icaloxide",
                f"DTSTAMP:{dt.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART;TZID=Europe/Istanbul:{dt.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=Europe/Istanbul:{dt_end.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:Engineering Sync #{i} - Planning & Architecture Review",
                f"DESCRIPTION:Detailed agenda item number {i} covering Rust, Python, and RFC 5545.",
                "LOCATION:Istanbul / Room 404",
                "CATEGORIES:Engineering,Performance,Rust",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def generate_recurring_ics(num_series: int) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//icaloxide//bench-rrule//EN",
    ]
    tzs = ["America/New_York", "Europe/Berlin", "Europe/Istanbul", "UTC"]
    for i in range(num_series):
        tz = tzs[i % len(tzs)]
        day = (i % 27) + 1
        hour = 8 + (i % 10)
        freq = "WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR" if i % 2 == 0 else "MONTHLY;BYDAY=-1FR"
        tz_param = "" if tz == "UTC" else f";TZID={tz}"
        z_suffix = "Z" if tz == "UTC" else ""
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:series-{i}@icaloxide",
                f"DTSTART{tz_param}:201601{day:02d}T{hour:02d}0000{z_suffix}",
                "DURATION:PT1H",
                f"RRULE:FREQ={freq}",
                f"EXDATE{tz_param}:202603{day:02d}T{hour:02d}0000{z_suffix}",
                f"SUMMARY:Recurring Series #{i}",
                "END:VEVENT",
                "BEGIN:VEVENT",
                f"UID:series-{i}@icaloxide",
                f"RECURRENCE-ID{tz_param}:202606{day:02d}T{hour:02d}0000{z_suffix}",
                f"DTSTART{tz_param}:202606{day:02d}T{hour + 1:02d}3000{z_suffix}",
                "DURATION:PT90M",
                f"SUMMARY:Recurring Series #{i} (Moved)",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def bench_time_and_pyheap(fn: Callable[[], object], repeats: int = 5) -> tuple[float, int]:
    fn()  # warmup
    times: list[float] = []
    for _ in range(repeats):
        gc.collect()
        t0 = time.perf_counter()
        _ = fn()
        times.append((time.perf_counter() - t0) * 1000.0)

    gc.collect()
    tracemalloc.start()
    _res = fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del _res
    return statistics.median(times), peak


def measure_subprocess_rss(ics_path: Path, mode: str) -> float:
    code = f"""
import gc, os, psutil
from pathlib import Path
data = Path({str(ics_path)!r}).read_bytes()
proc = psutil.Process(os.getpid())
gc.collect()
base = proc.memory_info().rss
if {mode!r} == "upstream":
    import icalendar
    cal = icalendar.Calendar.from_ical(data)
elif {mode!r} == "icaloxide":
    import icaloxide
    cal = icaloxide.Calendar.from_ical(data)
else:
    from icaloxide.compat import Calendar
    cal = Calendar.from_ical(data)
gc.collect()
after = proc.memory_info().rss
print(max(0, after - base))
"""
    out = subprocess.check_output([sys.executable, "-c", code], text=True)
    return int(out.strip()) / (1024 * 1024)


def main() -> None:
    medium_ics = generate_flat_ics(1_000)
    large_ics = generate_flat_ics(10_000)
    serial_ics = generate_flat_ics(5_000)
    rrule_ics = generate_recurring_ics(200)

    with tempfile.TemporaryDirectory() as tmp:
        large_path = Path(tmp) / "large_10k.ics"
        large_path.write_bytes(large_ics)

        up_rss_mb = measure_subprocess_rss(large_path, "upstream")
        ox_rss_mb = measure_subprocess_rss(large_path, "icaloxide")
        oxc_rss_mb = measure_subprocess_rss(large_path, "compat")

    results: list[dict[str, object]] = []

    # 1. Parse 1,000 events
    t_up, h_up = bench_time_and_pyheap(lambda: up_ical.Calendar.from_ical(medium_ics), repeats=7)
    t_ox, h_ox = bench_time_and_pyheap(lambda: icaloxide.Calendar.from_ical(medium_ics), repeats=7)
    t_oxc, h_oxc = bench_time_and_pyheap(
        lambda: OxCompatCalendar.from_ical(medium_ics), repeats=7
    )
    results.append(
        {
            "workload": f"Parse 1,000 events ({len(medium_ics) / 1024:.0f} KB)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 2. Parse 10,000 events
    t_up, h_up = bench_time_and_pyheap(lambda: up_ical.Calendar.from_ical(large_ics), repeats=3)
    t_ox, h_ox = bench_time_and_pyheap(lambda: icaloxide.Calendar.from_ical(large_ics), repeats=5)
    t_oxc, h_oxc = bench_time_and_pyheap(lambda: OxCompatCalendar.from_ical(large_ics), repeats=5)
    results.append(
        {
            "workload": f"Parse 10,000 events ({len(large_ics) / (1024 * 1024):.2f} MB)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 3. Recurring between (1-year window over 200 10-year series)
    up_rcal = up_ical.Calendar.from_ical(rrule_ics)
    ox_rcal = icaloxide.Calendar.from_ical(rrule_ics)
    oxc_rcal = OxCompatCalendar.from_ical(rrule_ics)

    w_start = datetime.date(2026, 1, 1)
    w_end = datetime.date(2026, 12, 31)

    up_q = up_rie.of(up_rcal)
    ox_q = ox_rcal.query()
    oxc_q = ox_of(oxc_rcal)

    up_cnt = len(up_q.between(w_start, w_end))
    ox_cnt = len(ox_q.between(w_start, w_end))
    oxc_cnt = len(oxc_q.between(w_start, w_end))
    assert up_cnt == ox_cnt == oxc_cnt, f"mismatch: {up_cnt} vs {ox_cnt} vs {oxc_cnt}"

    t_up, h_up = bench_time_and_pyheap(lambda: up_rie.of(up_rcal).between(w_start, w_end), repeats=3)
    t_ox, h_ox = bench_time_and_pyheap(lambda: ox_rcal.query().between(w_start, w_end), repeats=5)
    t_oxc, h_oxc = bench_time_and_pyheap(lambda: ox_of(oxc_rcal).between(w_start, w_end), repeats=5)
    results.append(
        {
            "workload": f"Recurring between 1y (200 series -> {ox_cnt} occs)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 4. Recurring count (5-year window)
    c_start = datetime.date(2026, 1, 1)
    c_end = datetime.date(2030, 12, 31)
    t_up, h_up = bench_time_and_pyheap(
        lambda: len(up_rie.of(up_rcal).between(c_start, c_end)), repeats=3
    )
    t_ox, h_ox = bench_time_and_pyheap(lambda: ox_q.count(c_start, c_end), repeats=7)
    t_oxc, h_oxc = bench_time_and_pyheap(lambda: oxc_q.count(c_start, c_end), repeats=7)
    results.append(
        {
            "workload": "Recurring count 5y (200 series)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 5. Streaming after(dt) next 20 occurrences
    anchor = datetime.datetime(2026, 6, 1, 0, 0, tzinfo=datetime.timezone.utc)
    t_up, h_up = bench_time_and_pyheap(
        lambda: list(itertools.islice(up_rie.of(up_rcal).after(anchor), 20)), repeats=5
    )
    t_ox, h_ox = bench_time_and_pyheap(
        lambda: list(ox_q.after(anchor, limit=20)), repeats=7
    )
    t_oxc, h_oxc = bench_time_and_pyheap(
        lambda: list(itertools.islice(oxc_q.after(anchor), 20)), repeats=7
    )
    results.append(
        {
            "workload": "Streaming after() next 20 (200 series)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 6. Serialize 5,000 events
    up_ser = up_ical.Calendar.from_ical(serial_ics)
    ox_ser = icaloxide.Calendar.from_ical(serial_ics)
    oxc_ser = OxCompatCalendar.from_ical(serial_ics)
    t_up, h_up = bench_time_and_pyheap(up_ser.to_ical, repeats=3)
    t_ox, h_ox = bench_time_and_pyheap(ox_ser.to_ical, repeats=5)
    t_oxc, h_oxc = bench_time_and_pyheap(oxc_ser.to_ical, repeats=5)
    results.append(
        {
            "workload": "Serialize 5,000 events (to_ical)",
            "upstream_ms": t_up,
            "ox_ms": t_ox,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_ox / 1024,
        }
    )

    # 7. Parse + Mutate 1,000 events + to_ical (compat vs upstream)
    def _up_mutate_1k() -> bytes:
        c = up_ical.Calendar.from_ical(medium_ics)
        for idx, ev in enumerate(c.events):
            ev["SUMMARY"] = f"Updated #{idx}"
        return c.to_ical()

    def _oxc_mutate_1k() -> bytes:
        c = OxCompatCalendar.from_ical(medium_ics)
        for idx, ev in enumerate(c.events):
            ev["SUMMARY"] = f"Updated #{idx}"
        return c.to_ical()

    t_up, h_up = bench_time_and_pyheap(_up_mutate_1k, repeats=3)
    t_oxc, h_oxc = bench_time_and_pyheap(_oxc_mutate_1k, repeats=5)
    results.append(
        {
            "workload": "Parse + Mutate 1,000 events + to_ical",
            "upstream_ms": t_up,
            "ox_ms": None,
            "oxc_ms": t_oxc,
            "up_heap_kb": h_up / 1024,
            "ox_heap_kb": h_oxc / 1024,
        }
    )

    summary = {
        "rss_10k_mb": {
            "upstream": round(up_rss_mb, 2),
            "icaloxide": round(ox_rss_mb, 2),
            "compat": round(oxc_rss_mb, 2),
        },
        "results": results,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
