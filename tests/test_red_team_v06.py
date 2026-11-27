"""Red-team v0.6.0 regression suite: Rust sorted to_ical, full setters/deleters, and compat parity."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import icalendar as up_ical
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat.errors import ICalParsingError
from icaloxide.compat.icalendar import (
    Alarm,
    Availability,
    Available,
    Calendar,
    Component,
    Event,
    Journal,
    Parameters,
    TimezoneDaylight,
    TimezoneStandard,
    Todo,
    vBinary,
    vPeriod,
    vRecur,
    vText,
    vUTCOffset,
)
from icaloxide.compat.recurring_ical_events import OccurrenceID
from icaloxide.compat.recurring_ical_events import of as compat_of


def test_unmaterialized_to_ical_sorted_runs_in_rust() -> None:
    """Calling cal.to_ical(sorted=True) on an unmaterialized Calendar sorts properties in Rust."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"Z-LAST:1\r\n"
        b"A-FIRST:2\r\n"
        b"UID:sort-rust\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"SUMMARY:S\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    assert cal.is_lazy()
    sorted_lines = [
        line
        for line in cal.to_ical(sorted=True).decode("utf-8").splitlines()
        if line.startswith(("A-FIRST", "Z-LAST"))
    ]
    assert sorted_lines == ["A-FIRST:2", "Z-LAST:1"]

    unsorted_lines = [
        line
        for line in cal.to_ical(sorted=False).decode("utf-8").splitlines()
        if line.startswith(("A-FIRST", "Z-LAST"))
    ]
    assert unsorted_lines == ["Z-LAST:1", "A-FIRST:2"]


def test_property_setters_and_deleters_across_components() -> None:
    """icalendar 7.x ergonomic getters, setters, and deleters work across component types."""
    cal = Calendar.new(prodid="-//T//T//EN")
    cal.calendar_name = "Work Cal"
    cal.method = "PUBLISH"
    assert cal.calendar_name == "Work Cal"
    assert cal.method == "PUBLISH"
    del cal.method
    assert cal.method is None

    ev = Event.new(uid="p1", summary="Init", start=datetime.date(2026, 7, 1))
    ev.summary = "Updated"
    ev.location = "Istanbul"
    ev.status = "CONFIRMED"
    ev.priority = 2
    ev.sequence = 3
    ev.categories = ["Rust", "Python"]
    assert ev.summary == "Updated"
    assert ev.location == "Istanbul"
    assert ev.status == "CONFIRMED"
    assert ev.priority == 2
    assert ev.sequence == 3
    assert ev.categories == ["Rust", "Python"]
    del ev.location
    del ev.status
    assert ev.location is None
    assert ev.status is None

    td = Todo.new(uid="t1", summary="Task")
    td.duration = datetime.timedelta(minutes=30)
    assert td.duration == datetime.timedelta(minutes=30)
    del td.duration
    assert td.duration is None

    jn = Journal.new(uid="j1", summary="Note")
    jn.descriptions = ["First", "Second"]
    assert jn.descriptions == ["First", "Second"]
    del jn.descriptions
    assert jn.descriptions == []

    al = Alarm.new_display(datetime.timedelta(minutes=-10), "Ping")
    al.action = "AUDIO"
    al.repeat = 2
    assert al.action == "AUDIO"
    assert al.repeat == 2

    std = TimezoneStandard()
    std.DTSTART = datetime.datetime(2020, 10, 25, 3, 0)
    std.TZOFFSETFROM = vUTCOffset(datetime.timedelta(hours=3))
    std.TZOFFSETTO = vUTCOffset(datetime.timedelta(hours=2))
    std.rrules = [vRecur(FREQ="YEARLY", BYMONTH=10, BYDAY="-1SU")]
    assert len(std.rrules) == 1
    assert std.TZOFFSETTO == datetime.timedelta(hours=2)

    dl = TimezoneDaylight()
    dl.TZOFFSETTO = vUTCOffset(datetime.timedelta(hours=3))
    assert dl.TZOFFSETTO == datetime.timedelta(hours=3)

    av = Availability.new(uid="av1")
    av.busy_type = "BUSY"
    assert av.busy_type == "BUSY"
    assert Available.example().summary == "Morning Slot"


def test_flattened_exdates_rdates_and_decoded_keyerror() -> None:
    """ev.exdates/rdates flatten to upstream shapes and decoded(missing) raises KeyError."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:rec-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"RDATE:20260710T100000Z,20260711T100000Z\r\n"
        b"RDATE;VALUE=PERIOD:20260715T100000Z/PT1H\r\n"
        b"EXDATE:20260702T100000Z\r\n"
        b"SUMMARY:Rec\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    up_ev = up_ical.Calendar.from_ical(ics).events[0]
    ox_ev = Calendar.from_ical(ics).events[0]

    assert ox_ev.exdates == [
        datetime.datetime(2026, 7, 2, 10, 0, tzinfo=datetime.timezone.utc)
    ]
    assert ox_ev.exdates == up_ev.exdates
    assert ox_ev.rdates == up_ev.rdates

    with pytest.raises(KeyError):
        ox_ev.decoded("NO_SUCH_PROP")
    assert ox_ev.decoded("NO_SUCH_PROP", None) is None


def test_calendar_from_ical_standalone_component_and_errors() -> None:
    """Calendar.from_ical accepts bare VEVENT when lenient and exposes errors."""
    vev = b"BEGIN:VEVENT\r\nUID:solo\r\nDTSTART:20260701T100000Z\r\nSUMMARY:Solo\r\nEND:VEVENT\r\n"
    ev = Calendar.from_ical(vev)
    assert isinstance(ev, Event)
    assert ev.summary == "Solo"

    with pytest.raises(ICalParsingError):
        Calendar.from_ical(vev, strict=True)

    malformed = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:1\r\n"
        b"DTSTART:NOTADATE\r\n"
        b"END:VEVENT\r\n"
    )
    cal = Calendar.from_ical(malformed)
    assert len(cal.errors) >= 1


def test_x_wr_timezone_floating_localization_at_kwarg_and_v_helpers() -> None:
    """X-WR-TIMEZONE localizes floating occurrences and v*/OccurrenceID helpers match upstream."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"X-WR-TIMEZONE:Europe/Berlin\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:xwr-1\r\n"
        b"DTSTART:20260701T090000\r\n"
        b"DTEND:20260701T100000\r\n"
        b"SUMMARY:BerlinFloat\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2026, 7, 1, 6, 30, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 1, 8, 30, tzinfo=datetime.timezone.utc)
    up_hits = up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we)
    ox_hits = compat_of(Calendar.from_ical(ics)).between(ws, we)
    assert len(ox_hits) == 1
    assert ox_hits[0].decoded("DTSTART") == up_hits[0].decoded("DTSTART")
    assert ox_hits[0].decoded("DTSTART") == datetime.datetime(
        2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")
    )

    at_hits = compat_of(Calendar.from_ical(ics)).at(date=datetime.date(2026, 7, 1))
    assert len(at_hits) == 1

    # OccurrenceID roundtrip
    oid = OccurrenceID.from_occurrence(ox_hits[0])
    oid2 = OccurrenceID.from_string(oid.to_string())
    assert oid2.uid == "xwr-1"
    assert oid2.start == oid.start

    # Parameters, vPeriod, vRecur, vBinary, vUTCOffset, vText helpers
    assert hasattr(Parameters, "canonical_order")
    p1 = vPeriod(
        (
            datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc),
            datetime.timedelta(hours=2),
        )
    )
    p2 = vPeriod(
        (
            datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc),
            datetime.timedelta(hours=1),
        )
    )
    assert p1.overlaps(p2)
    assert p1.dt == (p1.start, p1.end_or_dur)

    rec = vRecur({"FREQ": "DAILY", "COUNT": 3})
    assert rec.has_key("FREQ")
    assert rec.sorted_keys() == ["COUNT", "FREQ"]
    rec.move_to_end("COUNT", last=True)
    assert list(rec.keys())[-1] == "COUNT"

    assert vBinary(b"Hello").base64data == "SGVsbG8="
    assert vUTCOffset(datetime.timedelta(hours=3)).format() == "+0300"
    assert vText("Merhaba", params={"LANGUAGE": "tr"}).LANGUAGE == "tr"
    assert Component.errors.fget is not None
