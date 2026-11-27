# Migration Guide: `icalendar` + `recurring-ical-events` to `icaloxide`

`icaloxide` offers two migration paths:

1. **Drop-In Replacement (`icaloxide.compat`)** — swap imports with minimal code changes.
2. **Modern Rust-First API (`icaloxide`)** — maximum throughput and minimum Python heap allocation.

---

## Option 1: Drop-In Replacement (`icaloxide.compat`)

Replace:

```python
import icalendar
import recurring_ical_events
```

With:

```python
from icaloxide.compat import Calendar, Event, Todo, Alarm, of
```

Common patterns work identically:

```python
cal = Calendar.from_ical(raw_ics_bytes)
for ev in of(cal).between(start_date, end_date):
    print(ev["SUMMARY"], ev["DTSTART"].dt, ev["DTEND"].dt)

# Tuple dates, timedelta durations, and streaming .after()
today_events = of(cal).at((2026, 11, 27))
week_events = of(cal).between(start_date, datetime.timedelta(days=7))
next_five = list(itertools.islice(of(cal).after(now_utc), 5))
```

You can also pass an existing upstream `icalendar.Calendar` directly to `icaloxide.compat.of(up_cal)`.

---

## Option 2: Modern Native API (`icaloxide`)

For maximum speed (75x–560x faster) and lowest memory overhead, use the native `Calendar`, `CalendarQuery`, and `Occurrence` API directly (no `vDDDTypes` wrappers):

```python
import datetime
import icaloxide

cal = icaloxide.Calendar.from_ical(raw_ics_bytes)  # bytes, bytearray, memoryview, str, or Path

# Lazy component inspection
for ev in cal.events:
    print(ev.summary, ev["DTSTART"], ev.params("DTSTART"))

# Indexed recurring queries (GIL-free in Rust)
query = cal.query(components=["VEVENT"])
for occ in query.between(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)):
    print(occ.summary, occ.start, occ.end, occ.is_all_day)

# Fast count without Python object materialization
total = query.count(datetime.date(2026, 1, 1), datetime.date(2030, 12, 31))

# Bounded streaming iterator over infinite RRULEs
for occ in query.after(datetime.datetime.now(datetime.timezone.utc), limit=20):
    print(occ.summary, occ.start)
```
