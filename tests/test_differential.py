"""Differential verification comparing icaloxide against upstream icalendar + recurring-ical-events."""

from __future__ import annotations

import datetime
import itertools

import icalendar as up_ical
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat import Calendar as OxCalendar
from icaloxide.compat import of as ox_of


def _normalize_dt(val: object) -> object:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is not None:
            return val.astimezone(datetime.timezone.utc)
        return val
    if isinstance(val, datetime.date):
        return val
    return val


def _extract_signature(comp: object) -> tuple[str, object, object]:
    summary = str(comp.get("SUMMARY", ""))  # type: ignore[attr-defined]
    dtstart_raw = comp["DTSTART"].dt  # type: ignore[index]
    dtend_prop = comp.get("DTEND")  # type: ignore[attr-defined]
    dtend_raw = dtend_prop.dt if dtend_prop is not None else dtstart_raw
    return (summary, _normalize_dt(dtstart_raw), _normalize_dt(dtend_raw))


CASES: list[tuple[str, bytes, datetime.date, datetime.date]] = [
    (
        "weekly_interval_wkst_su",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//1//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:w1\r\n"
            b"DTSTART:20260107T100000Z\r\nDURATION:PT1H\r\n"
            b"RRULE:FREQ=WEEKLY;INTERVAL=2;WKST=SU;BYDAY=SU,WE;COUNT=8\r\n"
            b"SUMMARY:Biweekly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        datetime.date(2026, 1, 1),
        datetime.date(2026, 3, 31),
    ),
    (
        "monthly_bysetpos_last_workday",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//2//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:m1\r\n"
            b"DTSTART;TZID=Europe/Berlin:20260101T160000\r\nDURATION:PT30M\r\n"
            b"RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=6\r\n"
            b"SUMMARY:Month Close\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        datetime.date(2026, 1, 1),
        datetime.date(2026, 7, 1),
    ),
    (
        "dst_spring_forward_and_overrides",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//3//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:ny1\r\n"
            b"DTSTART;TZID=America/New_York:20260306T090000\r\nDURATION:PT45M\r\n"
            b"RRULE:FREQ=DAILY;COUNT=7\r\n"
            b"EXDATE;TZID=America/New_York:20260307T090000\r\n"
            b"SUMMARY:NY Daily\r\nEND:VEVENT\r\n"
            b"BEGIN:VEVENT\r\nUID:ny1\r\n"
            b"RECURRENCE-ID;TZID=America/New_York:20260309T090000\r\n"
            b"DTSTART;TZID=America/New_York:20260309T113000\r\nDURATION:PT1H\r\n"
            b"SUMMARY:NY Rescheduled\r\nEND:VEVENT\r\n"
            b"END:VCALENDAR\r\n"
        ),
        datetime.date(2026, 3, 5),
        datetime.date(2026, 3, 15),
    ),
    (
        "yearly_bymonth_last_sunday_and_rdate",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//4//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:yr1\r\n"
            b"DTSTART:20240331T080000Z\r\nDURATION:PT2H\r\n"
            b"RRULE:FREQ=YEARLY;BYMONTH=3,9;BYDAY=-1SU;UNTIL=20270401T000000Z\r\n"
            b"RDATE:20260615T080000Z\r\n"
            b"SUMMARY:Biannual\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        datetime.date(2024, 1, 1),
        datetime.date(2027, 12, 31),
    ),
    (
        "all_day_recurring_and_exdate_date",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//5//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:ad1\r\n"
            b"DTSTART;VALUE=DATE:20260501\r\n"
            b"RRULE:FREQ=WEEKLY;COUNT=5\r\n"
            b"EXDATE;VALUE=DATE:20260515\r\n"
            b"SUMMARY:Friday AllDay\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        datetime.date(2026, 5, 1),
        datetime.date(2026, 6, 15),
    ),
    (
        "turkish_unicode_escaped_text",
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//6//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:tr1\r\n"
            b"DTSTART;TZID=Europe/Istanbul:20260201T140000\r\nDURATION:PT1H\r\n"
            b"RRULE:FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3\r\n"
            b"SUMMARY:G\xc3\xbcnl\xc3\xbck\\, Ayl\xc4\xb1k \\; \xc4\x9f\xc3\xbc\xc5\x9f\xc4\xb1\xc3\xb6\xc3\xa7\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        datetime.date(2026, 1, 1),
        datetime.date(2026, 6, 1),
    ),
]


@pytest.mark.parametrize(("name", "ics", "start", "end"), CASES, ids=[c[0] for c in CASES])
def test_differential_between_and_after(
    name: str,
    ics: bytes,
    start: datetime.date,
    end: datetime.date,
) -> None:
    _ = name
    up_cal = up_ical.Calendar.from_ical(ics)
    ox_cal = OxCalendar.from_ical(ics)

    up_res = up_rie.of(up_cal).between(start, end)
    ox_res = ox_of(ox_cal).between(start, end)

    up_sigs = sorted((_extract_signature(e) for e in up_res), key=lambda x: (str(x[1]), x[0]))
    ox_sigs = sorted((_extract_signature(e) for e in ox_res), key=lambda x: (str(x[1]), x[0]))
    assert ox_sigs == up_sigs

    # Compare streaming after() from start datetime
    anchor = datetime.datetime(start.year, start.month, start.day, 0, 0, tzinfo=datetime.timezone.utc)
    up_after = [_extract_signature(e) for e in itertools.islice(up_rie.of(up_cal).after(anchor), 10)]
    ox_after = [_extract_signature(e) for e in itertools.islice(ox_of(ox_cal).after(anchor), 10)]
    assert ox_after == up_after


def test_differential_vtodo_and_vjournal() -> None:
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//diff//todo//EN\r\n"
        b"BEGIN:VTODO\r\nUID:t1\r\n"
        b"DTSTART:20260401T090000Z\r\nDUE:20260401T120000Z\r\n"
        b"RRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Weekly Task\r\nEND:VTODO\r\n"
        b"BEGIN:VJOURNAL\r\nUID:j1\r\n"
        b"DTSTART:20260405T080000Z\r\nSUMMARY:Sprint Log\r\nEND:VJOURNAL\r\n"
        b"END:VCALENDAR\r\n"
    )
    up_cal = up_ical.Calendar.from_ical(ics)
    ox_cal = OxCalendar.from_ical(ics)

    start = datetime.date(2026, 4, 1)
    end = datetime.date(2026, 4, 30)

    up_res = up_rie.of(up_cal, components=["VTODO", "VJOURNAL"]).between(start, end)
    ox_res = ox_of(ox_cal, components=["VTODO", "VJOURNAL"]).between(start, end)

    up_sigs = sorted((_extract_signature(c) for c in up_res), key=lambda x: (str(x[1]), x[0]))
    ox_sigs = sorted((_extract_signature(c) for c in ox_res), key=lambda x: (str(x[1]), x[0]))
    assert ox_sigs == up_sigs
