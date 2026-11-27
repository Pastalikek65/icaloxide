import datetime
import itertools
from zoneinfo import ZoneInfo

import icalendar as upstream_ical
from icaloxide.compat import Alarm, Calendar, Event, Todo, of


def test_compat_builder_and_roundtrip() -> None:
    cal = Calendar()
    cal.add("PRODID", "-//icaloxide//compat-builder//EN")
    cal.add("VERSION", "2.0")

    ev = Event()
    ev.add("UID", "compat-ev-1")
    ev.add("SUMMARY", "Design Review, Phase 4")
    ev.add("DTSTART", datetime.datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")))
    ev.add("DTEND", datetime.datetime(2026, 6, 1, 11, 30, tzinfo=ZoneInfo("Europe/Istanbul")))
    ev.add("CATEGORIES", ["Rust", "Python"])

    alarm = Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("TRIGGER", datetime.timedelta(minutes=-15))
    alarm.add("DESCRIPTION", "Reminder")
    ev.add_component(alarm)
    cal.add_component(ev)

    todo = Todo()
    todo.add("UID", "compat-todo-1")
    todo.add("DUE", datetime.date(2026, 6, 2))
    todo.add("SUMMARY", "Ship wheels")
    cal.add_component(todo)

    raw = cal.to_ical()
    reparsed = Calendar.from_ical(raw)
    assert isinstance(reparsed, Calendar)
    assert len(reparsed.events) == 1
    assert len(reparsed.todos) == 1

    ev2 = reparsed.walk("VEVENT")[0]
    assert ev2["SUMMARY"] == "Design Review, Phase 4"
    assert ev2["DTSTART"].dt == datetime.datetime(
        2026, 6, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert ev2.decoded("CATEGORIES") == ["Rust", "Python"]
    assert ev2.walk("VALARM")[0]["TRIGGER"].dt == datetime.timedelta(minutes=-15)


def test_compat_multiple_calendars_and_upstream_interop() -> None:
    multi = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//a//1//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:1\r\nDTSTART:20260101T100000Z\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//b//2//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:2\r\nDTSTART:20260102T100000Z\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    cals = Calendar.from_ical(multi, multiple=True)
    assert isinstance(cals, list)
    assert len(cals) == 2
    assert cals[0].events[0]["UID"] == "1"
    assert cals[1].events[0]["UID"] == "2"

    # Pass an upstream icalendar.Calendar object directly to icaloxide.compat.of()
    up_cal = upstream_ical.Calendar()
    up_cal.add("VERSION", "2.0")
    up_cal.add("PRODID", "-//upstream//test//EN")
    up_ev = upstream_ical.Event()
    up_ev.add("UID", "up-1")
    up_ev.add("SUMMARY", "Weekly Sync")
    up_ev.add("DTSTART", datetime.datetime(2026, 3, 2, 9, 0, tzinfo=datetime.timezone.utc))
    up_ev.add("DURATION", datetime.timedelta(hours=1))
    up_ev.add("RRULE", {"FREQ": "WEEKLY"})
    up_cal.add_component(up_ev)

    query = of(up_cal)
    assert len(query.at((2026, 3))) == 5
    assert len(query.at((2026, 3, 9))) == 1
    assert len(query.between(datetime.date(2026, 3, 1), datetime.timedelta(days=14))) == 2
    assert query.count(datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)) == 5

    next_two = list(
        itertools.islice(
            query.after(datetime.datetime(2026, 4, 1, 0, 0, tzinfo=datetime.timezone.utc)),
            2,
        )
    )
    assert [e["DTSTART"].dt.date() for e in next_two] == [
        datetime.date(2026, 4, 6),
        datetime.date(2026, 4, 13),
    ]
    assert b"DTSTART:20260406T090000Z" in next_two[0].to_ical()
