from __future__ import annotations

import datetime
import itertools
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
from icaloxide.compat import Alarm, Calendar, Event, Todo, of, vRecur


def test_vrecur_dict_roundtrip_and_jcal_parity() -> None:
    cal = Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "-//icaloxide//v02//EN")

    ev = Event.new(
        uid="ev-v02-1",
        summary="Weekly Architecture",
        start=datetime.datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        duration=datetime.timedelta(minutes=45),
        location="Istanbul",
    )
    ev.add("RRULE", {"FREQ": "WEEKLY", "COUNT": 3, "BYDAY": ["FR"]})
    al = Alarm.new_display(datetime.timedelta(minutes=-10), "Starts in 10m")
    ev.add_component(al)
    cal.add_component(ev)

    raw = cal.to_ical()
    reparsed = Calendar.from_ical(raw)
    ev2 = reparsed.events[0]
    assert isinstance(ev2["RRULE"], vRecur)
    assert ev2["RRULE"]["FREQ"] == ["WEEKLY"]
    assert ev2["RRULE"]["COUNT"] == [3]
    assert ev2.summary == "Weekly Architecture"
    assert ev2.duration == datetime.timedelta(minutes=45)
    assert ev2.alarms[0].action == "DISPLAY"

    # Full dict protocol checks
    assert "SUMMARY" in ev2
    assert set(ev2.keys()) >= {"UID", "SUMMARY", "DTSTART", "DURATION", "RRULE"}
    assert ev2.has_key("SUMMARY")
    assert len(ev2.property_items()) >= 6
    copied = ev2.copy()
    assert copied.pop("LOCATION") == "Istanbul"
    assert "LOCATION" not in copied
    assert copied.setdefault("STATUS", "CONFIRMED") == "CONFIRMED"

    # RFC 7265 jCal round-trip and structure check against upstream
    jcal_json = reparsed.to_json()
    from_jcal = Calendar.from_jcal(jcal_json)
    assert from_jcal.events[0].summary == "Weekly Architecture"
    assert isinstance(from_jcal.events[0]["RRULE"], vRecur)
    up_parsed_jcal = up_ical.Calendar.from_jcal(jcal_json)
    assert up_parsed_jcal.events[0]["SUMMARY"] == "Weekly Architecture"


def test_tzid_inspection_validation_and_with_uid() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//inspect//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:target-uid\r\n"
        "DTSTART;TZID=Europe/Berlin:20260601T090000\r\n"
        "SUMMARY:Berlin Event\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    assert cal.get_used_tzids() == ["Europe/Berlin"]
    assert cal.get_missing_tzids() == ["Europe/Berlin"]
    assert len(cal.with_uid("target-uid")) == 1
    assert any("DTSTAMP" in msg for _, msg in cal.validate())

    enriched = cal.add_missing_timezones()
    assert enriched.get_missing_tzids() == []


def test_skip_bad_series_keep_recurrence_paginate_first_all() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//compat-query//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:good-1\r\n"
        "DTSTART:20260101T090000Z\r\n"
        "DURATION:PT1H\r\n"
        "RRULE:FREQ=DAILY;COUNT=7\r\n"
        "SUMMARY:Good Series\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:bad-1\r\n"
        "DTSTART:20260101T090000Z\r\n"
        "RRULE:FREQ=NOT_VALID\r\n"
        "SUMMARY:Bad Series\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    q = of(cal, keep_recurrence_attributes=True, skip_bad_series=True)
    assert len(q.suppressed_errors) == 1
    assert q.first is not None
    assert q.first["DTSTART"].dt == datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc)
    assert "RRULE" in q.first
    assert b"RRULE:" in q.first.to_ical()

    all_occs = q.all()
    assert len(all_occs) == 7

    pages = list(q.paginate(3))
    assert [len(p) for p in pages] == [3, 3, 1]


def test_rdate_period_thisandfuture_valarm_after_and_builder() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//v02-adv//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ev-adv\r\n"
        "DTSTART:20260301T100000Z\r\n"
        "DURATION:PT1H\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
        "RDATE;VALUE=PERIOD:20260303T150000Z/PT4H\r\n"
        "SUMMARY:Adv Base\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-PT15M\r\n"
        "DESCRIPTION:Ring\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ev-adv\r\n"
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260315T100000Z\r\n"
        "DTSTART:20260315T120000Z\r\n"
        "DURATION:PT2H\r\n"
        "SUMMARY:Adv Shifted\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    occs = cal.query().all()
    assert len(occs) == 5
    # Mar 1 (1h), Mar 3 (4h period RDATE), Mar 8 (1h), Mar 15 (shifted 12:00Z 2h), Mar 22 (shifted 12:00Z 2h)
    assert occs[1].end - occs[1].start == datetime.timedelta(hours=4)
    assert occs[3].start == datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc)
    assert occs[4].start == datetime.datetime(2026, 3, 22, 12, 0, tzinfo=datetime.timezone.utc)

    alarms = list(
        itertools.islice(
            cal.query(components=["VALARM"]).after(
                datetime.datetime(2026, 3, 1, 0, 0, tzinfo=datetime.timezone.utc)
            ),
            4,
        )
    )
    assert len(alarms) == 4
    assert alarms[0].start == datetime.datetime(2026, 3, 1, 9, 45, tzinfo=datetime.timezone.utc)
    assert alarms[1].start == datetime.datetime(2026, 3, 3, 14, 45, tzinfo=datetime.timezone.utc)

    # Native Rust CalendarBuilder
    builder = icaloxide.CalendarBuilder(prodid="-//icaloxide//builder//EN", name="Built")
    builder.add_event(
        uid="b-1",
        summary="Built Event",
        start=datetime.datetime(2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        duration=datetime.timedelta(hours=1),
        rrule="FREQ=DAILY;COUNT=3",
        categories=["Rust", "Speed"],
    )
    builder.add_todo(
        uid="t-1",
        summary="Built Todo",
        due=datetime.date(2026, 7, 5),
        status="NEEDS-ACTION",
    )
    built_cal = builder.build()
    assert built_cal.name == "Built"
    assert len(built_cal.query().all()) == 3
    assert len(Todo.new(uid="x", summary="y", due=datetime.date(2026, 7, 5)).to_ical()) > 0
