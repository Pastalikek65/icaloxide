"""Red-team v0.5.0 regression suite and compat API/submodule parity tests."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat import cal as compat_cal_mod
from icaloxide.compat import icalendar as compat_ical
from icaloxide.compat import param as compat_param_mod
from icaloxide.compat import prop as compat_prop_mod
from icaloxide.compat.icalendar import (
    Alarm,
    Availability,
    Calendar,
    Component,
    Event,
    FreeBusy,
    Journal,
    Timezone,
    Todo,
    q_join,
    q_split,
    use_pytz,
    vBinary,
)
from icaloxide.compat.recurring_ical_events import of as compat_of


def test_cross_timezone_rdate_uses_own_tzid() -> None:
    """RDATE with its own TZID must resolve in that TZID, not DTSTART's timezone."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:rd-tokyo\r\n"
        b"DTSTART;TZID=America/New_York:20260701T090000\r\n"
        b"DTEND;TZID=America/New_York:20260701T100000\r\n"
        b"RDATE;TZID=Asia/Tokyo:20260705T150000\r\n"
        b"SUMMARY:TokyoRDate\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2026, 7, 5, 5, 0, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 5, 7, 0, tzinfo=datetime.timezone.utc)
    ws_wrong = datetime.datetime(2026, 7, 5, 18, 0, tzinfo=datetime.timezone.utc)
    we_wrong = datetime.datetime(2026, 7, 5, 20, 0, tzinfo=datetime.timezone.utc)

    up_hits = up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we)
    ox_hits = compat_of(Calendar.from_ical(ics)).between(ws, we)
    ox_wrong = compat_of(Calendar.from_ical(ics)).between(ws_wrong, we_wrong)

    assert len(up_hits) == 1
    assert len(ox_hits) == 1
    assert ox_wrong == []
    assert ox_hits[0].decoded("DTSTART") == datetime.datetime(
        2026, 7, 5, 15, 0, tzinfo=ZoneInfo("Asia/Tokyo")
    )


def test_dst_spring_forward_hourly_does_not_drop_occurrence() -> None:
    """FREQ=HOURLY across spring-forward gap must preserve COUNT without colliding on UTC instant."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:spring-hourly\r\n"
        b"DTSTART;TZID=Europe/Berlin:20260329T003000\r\n"
        b"DURATION:PT30M\r\n"
        b"RRULE:FREQ=HOURLY;COUNT=5\r\n"
        b"SUMMARY:SpringHourly\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    up_dts = [
        e.decoded("DTSTART").isoformat()
        for e in up_rie.of(up_ical.Calendar.from_ical(ics)).between(
            datetime.date(2026, 3, 28), datetime.date(2026, 3, 31)
        )
    ]
    ox_dts = [
        e.decoded("DTSTART").isoformat()
        for e in compat_of(Calendar.from_ical(ics)).between(
            datetime.date(2026, 3, 28), datetime.date(2026, 3, 31)
        )
    ]
    assert len(ox_dts) == 5
    assert ox_dts == up_dts


def test_finite_count_rrule_after_2100_included_in_all() -> None:
    """Finite COUNT RRULEs beyond 2100 must be returned by CalendarQuery.all()."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:cnt-2150\r\n"
        b"DTSTART:21500101T090000Z\r\n"
        b"DTEND:21500101T100000Z\r\n"
        b"RRULE:FREQ=DAILY;COUNT=3\r\n"
        b"SUMMARY:FutureCount\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    q = icaloxide.Calendar.from_ical(ics).query()
    all_occs = q.all()
    assert len(all_occs) == 3
    assert q.count_all() == 3


def test_live_subcomponents_list_and_inplace_value_mutations() -> None:
    """subcomponents list mutations, ev['DTSTART'].dt = ..., and ev['RRULE']['COUNT'] = ... persist."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:mut-live\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"RRULE:FREQ=DAILY;COUNT=3\r\n"
        b"SUMMARY:A\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    c1 = Calendar.from_ical(ics)
    c1.subcomponents.clear()
    assert len(c1.subcomponents) == 0
    assert b"BEGIN:VEVENT" not in c1.to_ical()

    c2 = Calendar.from_ical(ics)
    ev_new = Event.new(uid="mut-2", summary="B", start=datetime.date(2026, 7, 2))
    c2.subcomponents[0] = ev_new
    assert c2.events[0]["UID"] == "mut-2"
    assert b"UID:mut-2" in c2.to_ical()

    c3 = Calendar.from_ical(ics)
    c3.events[0]["DTSTART"].dt = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc)
    c3.events[0]["RRULE"]["COUNT"] = [9]
    out3 = c3.to_ical()
    assert b"DTSTART:20260801T100000Z" in out3
    assert b"COUNT=9" in out3


def test_binary_attach_and_occurrence_tzid_param_parity() -> None:
    """Binary ATTACH returns vBinary/bytes and Occurrence DTSTART/DTEND preserve TZID params."""
    ics_bin = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:bin-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"ATTACH;FMTTYPE=text/plain;ENCODING=BASE64;VALUE=BINARY:SGVsbG8=\r\n"
        b"SUMMARY:Bin\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics_bin)
    ev = cal.events[0]
    assert isinstance(ev["ATTACH"], vBinary)
    assert ev.decoded("ATTACH") == b"Hello"
    ev["LOCATION"] = "Desk"
    assert isinstance(ev["ATTACH"], vBinary)
    assert ev.decoded("ATTACH") == b"Hello"
    assert b"ATTACH;FMTTYPE=text/plain;ENCODING=BASE64;VALUE=BINARY:SGVsbG8=" in cal.to_ical()

    ics_tz = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:occ-tz\r\n"
        b"DTSTART;TZID=America/New_York:20260701T090000\r\n"
        b"DTEND;TZID=America/New_York:20260701T100000\r\n"
        b"RRULE:FREQ=DAILY;COUNT=2\r\n"
        b"SUMMARY:TZ\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    occ = compat_of(Calendar.from_ical(ics_tz)).between(
        datetime.date(2026, 7, 1), datetime.date(2026, 7, 3)
    )[0]
    assert occ["DTSTART"].params["TZID"] == "America/New_York"
    assert occ["DTEND"].params["TZID"] == "America/New_York"
    occ["LOCATION"] = "NY"
    assert occ["DTSTART"].params["TZID"] == "America/New_York"


def test_compat_icalendar_reexports_submodules_and_class_methods() -> None:
    """icaloxide.compat.icalendar and compat.{prop,cal,param} mirror upstream icalendar API."""
    for name in (
        "Alarms",
        "AlarmTime",
        "CLASS",
        "STATUS",
        "TRANSP",
        "PARTSTAT",
        "ROLE",
        "vAdr",
        "vN",
        "vOrg",
        "AdrFields",
        "NFields",
        "ComponentFactory",
        "TypesFactory",
        "InvalidCalendar",
        "IncompleteComponent",
        "q_join",
        "q_split",
        "use_pytz",
        "__version__",
        "version",
        "version_tuple",
    ):
        assert hasattr(compat_ical, name), f"missing {name} on compat.icalendar"

    assert compat_prop_mod.vText is compat_ical.vText
    assert compat_cal_mod.Event is Event
    assert compat_param_mod.Parameters is compat_ical.Parameters

    assert q_join(["a", "b:c"]) == 'a,"b:c"'
    assert q_split('a,"b,c",d') == ["a", '"b,c"', "d"]
    use_pytz()

    ev_parsed = Component.parse(
        b"BEGIN:VEVENT\r\nUID:p1\r\nDTSTART:20260101\r\nSUMMARY:Parsed\r\nEND:VEVENT\r\n"
    )
    assert isinstance(ev_parsed, Event)
    assert ev_parsed.summary == "Parsed"
    assert isinstance(Component.types_factory, compat_ical.TypesFactory)

    for comp_cls in (Event, Todo, Journal, Alarm, Timezone, FreeBusy, Availability):
        sample = comp_cls.example()
        assert sample.to_ical()
        if isinstance(sample, Event):
            assert isinstance(sample.summary, str)
    with pytest.raises(ValueError):
        Timezone.from_tzid("No/SuchZone")
