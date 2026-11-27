from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from icaloxide.cli import main as cli_main
from icaloxide.compat import (
    CLASS,
    PARTSTAT,
    STATUS,
    TRANSP,
    Alarm,
    Alarms,
    Availability,
    Available,
    Calendar,
    Event,
    FreeBusy,
    InvalidCalendar,
    Journal,
    Parameters,
    Timezone,
    of,
)


def test_pages_cursor_date_args_count_multical_and_busy_intervals() -> None:
    c1 = Calendar()
    c1.add_component(
        Event.new(
            uid="e1",
            summary="Daily",
            start=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
            duration=datetime.timedelta(hours=1),
        )
    )
    c1.events[0].add("RRULE", {"FREQ": "DAILY", "COUNT": 5})

    c2 = Calendar()
    c2.add_component(
        Event.new(
            uid="e2",
            summary="Transparent",
            start=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc),
            duration=datetime.timedelta(hours=2),
        )
    )
    c2.events[0]["TRANSP"] = TRANSP.TRANSPARENT.value

    # Multi-calendar of([c1, c2])
    q = of([c1, c2])
    assert q.count() == 6  # unbounded count_all across both calendars

    # DateArgument variants: int year, str date, 4-tuple hour, 6-tuple second
    assert len(q.at(2026)) == 6
    assert len(q.at("20260101")) == 2
    assert len(q.at((2026, 1, 1, 9))) == 1
    assert len(q.at((2026, 1, 1, 12, 30, 0))) == 1

    # Paginate with Pages/Page, latest_start, and cursor next_page_id resumption
    pages = q.paginate(page_size=2, latest_start=datetime.date(2026, 1, 4))
    p1 = next(pages)
    assert len(p1) == 2
    assert p1.has_next_page()
    resumed = q.paginate(page_size=2, latest_start=datetime.date(2026, 1, 4), next_page_id=p1.next_page_id)
    p2 = resumed.generate_next_page()
    assert len(p2) == 2
    assert p2.components[0].start == p1.components[0].start or p2.components[0].start >= p1.components[-1].start

    # Busy vs free intervals ignore TRANSPARENT
    busy = q.busy_intervals(
        datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 1, 2, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert busy == [
        (
            datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc),
        )
    ]
    free = q.free_intervals(
        datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 1, 2, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert len(free) == 2


def test_timezone_helpers_component_from_ical_rfc9074_alarms_and_mutations() -> None:
    # Timezone.from_tzid, get_transitions, to_tz, from_tzinfo
    vtz = Timezone.from_tzid("America/New_York", datetime.date(2024, 1, 1), datetime.date(2027, 1, 1))
    assert vtz.tz_name == "America/New_York"
    times, infos = vtz.get_transitions()
    assert len(times) >= 4
    assert len(infos) == len(times)
    py_tz = vtz.to_tz(lookup_tzid=False)
    winter = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=py_tz)
    summer = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=py_tz)
    assert winter.utcoffset() == datetime.timedelta(hours=-5)
    assert summer.utcoffset() == datetime.timedelta(hours=-4)

    vtz_from_info = Timezone.from_tzinfo(ZoneInfo("Europe/Istanbul"))
    assert vtz_from_info.tz_name == "Europe/Istanbul"

    # Standalone Component.from_ical without VCALENDAR
    solo_ev = Event.from_ical(b"BEGIN:VEVENT\r\nUID:solo\r\nSUMMARY:Solo\r\nDTSTART:20260501T100000Z\r\nEND:VEVENT\r\n")
    assert isinstance(solo_ev, Event)
    assert solo_ev.summary == "Solo"

    # RFC 9074 Alarms with lowercase RELATED=start (#2197) + acknowledge + snooze
    ev = Event.new(
        uid="al-ev",
        summary="Meeting",
        start=datetime.datetime(2026, 6, 1, 10, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=1),
    )
    al = Alarm.new_display(datetime.timedelta(minutes=-15), "Soon")
    al["TRIGGER"].params["RELATED"] = "start"
    ev.add_component(al)
    calc = Alarms(ev)
    assert len(calc.times) == 1
    assert len(calc.active) == 1
    calc.acknowledge_until(datetime.datetime(2026, 6, 1, 9, 50, tzinfo=datetime.timezone.utc))
    assert len(calc.active) == 0
    calc.snooze_until(datetime.datetime(2026, 6, 1, 9, 55, tzinfo=datetime.timezone.utc))
    assert len(calc.active) == 1

    # Occurrence mutation helpers: cancel_occurrence, modify_occurrence, complete_occurrence
    cal = Calendar.new(name="Team")
    master = Event.new(
        uid="series-1",
        summary="Standup",
        start=datetime.datetime(2026, 2, 1, 9, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(minutes=30),
    )
    master.add("RRULE", {"FREQ": "WEEKLY", "COUNT": 4})
    cal.add_component(master)
    cal.cancel_occurrence("series-1", datetime.datetime(2026, 2, 8, 9, 0, tzinfo=datetime.timezone.utc))
    cal.modify_occurrence(
        "series-1",
        datetime.datetime(2026, 2, 15, 9, 0, tzinfo=datetime.timezone.utc),
        summary="Moved Standup",
        start=datetime.datetime(2026, 2, 15, 11, 0, tzinfo=datetime.timezone.utc),
    )
    occs = of(cal).between(datetime.date(2026, 2, 1), datetime.date(2026, 2, 28))
    assert len(occs) == 3
    assert any(o.summary == "Moved Standup" for o in occs)

    # New enums/errors/classes sanity
    assert issubclass(InvalidCalendar, ValueError)
    assert CLASS.PUBLIC == "PUBLIC"
    assert STATUS.CONFIRMED == "CONFIRMED"
    assert PARTSTAT.ACCEPTED == "ACCEPTED"
    assert isinstance(Journal.new(summary="Note"), Journal)
    assert isinstance(FreeBusy.new(uid="fb1"), FreeBusy)
    assert isinstance(Availability.new(uid="av1"), Availability)
    assert isinstance(Available.new(uid="w1"), Available)


def test_xcal_jcal_params_rfc9253_and_cli(tmp_path: Path) -> None:
    cal = Calendar.new(prodid="-//icaloxide//v03//EN")
    ev = Event.new(
        uid="xcal-1",
        summary="Architecture Review",
        start=datetime.datetime(2026, 7, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        duration=datetime.timedelta(hours=1),
    )
    ev.add("ATTENDEE", "mailto:alice@example.com", parameters={"MEMBER": ["mailto:team@example.com", "mailto:eng@example.com"]})
    ev.add("LINK", "https://example.com/spec", parameters={"LINKREL": "describedby"})
    ev.add("CONCEPT", "https://example.com/topic")
    ev.add("REFID", "sprint-42")
    ev.stamp()
    cal.add_component(ev)

    # RFC 9253 accessors & Parameters helpers
    reparsed = Calendar.from_ical(cal.to_ical())
    ev2 = reparsed.events[0]
    assert [str(x) for x in ev2.links] == ["https://example.com/spec"]
    assert [str(x) for x in ev2.concepts] == ["https://example.com/topic"]
    assert ev2.refids == ["sprint-42"]
    p = Parameters.from_ical("TZID=Europe/Istanbul;VALUE=DATE-TIME")
    assert p.tzid() == "Europe/Istanbul"
    assert p.value() == "DATE-TIME"

    # RFC 6321 xCal round-trip including multi-valued MEMBER (#2076)
    xcal_str = reparsed.to_xcal()
    assert "urn:ietf:params:xml:ns:icalendar-2.0" in xcal_str
    from_xml = Calendar.from_xcal(xcal_str)
    assert from_xml.events[0].summary == "Architecture Review"

    # CLI commands
    ics_file = tmp_path / "sample.ics"
    ics_file.write_bytes(reparsed.to_ical())
    assert cli_main(["validate", str(ics_file)]) == 0
    assert cli_main(["view", str(ics_file)]) == 0
    assert cli_main(["query", str(ics_file), "--between", "2026-07-01", "2026-07-02", "--busy"]) == 0
    assert cli_main(["convert", str(ics_file), "--to", "xcal"]) == 0
