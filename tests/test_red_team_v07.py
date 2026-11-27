"""Red-team v0.7.0 regression suite: PyO3 speedups, RFC 5545 spans, 7.x .new(), and RIE adapters."""

from __future__ import annotations

import datetime
import uuid
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
import pytest
from icaloxide.compat.icalendar import (
    Alarm,
    Availability,
    Available,
    Calendar,
    Component,
    Event,
    FreeBusy,
    Journal,
    Timezone,
    Todo,
    vBoolean,
    vCalAddress,
    vCategory,
    vDatetime,
    vDDDTypes,
    vDuration,
    vFloat,
    vGeo,
    vInt,
    vMonth,
    vPeriod,
    vRecur,
    vTime,
    vUri,
    vWeekday,
)
from icaloxide.compat.recurring_ical_events import (
    CalendarQuery,
    ComponentAdapter,
    ComponentsWithName,
    Occurrence,
    OccurrenceID,
)


def test_rust_fold_ical_lines_raw_props_compact_and_zoneinfo_cache() -> None:
    """fold_ical_lines handles UTF-8 75-octet folding and compact props cache ZoneInfo."""
    ascii_and_utf8 = [
        "BEGIN:VEVENT",
        "UID:fold-1",
        "SUMMARY:" + "Şğüıçö🎉" * 15,
        "END:VEVENT",
    ]
    folded = icaloxide.fold_ical_lines(ascii_and_utf8)
    for seg in folded.split(b"\r\n")[:-1]:
        assert len(seg) <= 75
        seg.decode("utf-8")

    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:compact-1\r\n"
        b"DTSTART;TZID=Europe/Istanbul:20260701T100000\r\n"
        b"DTEND;TZID=Europe/Istanbul:20260701T110000\r\n"
        b"SUMMARY:Plain No Params\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rcal = icaloxide.Calendar.from_ical(ics)
    rev = rcal.events[0]
    compact = dict((k, p) for k, _v, p in rev.raw_properties_compact())
    assert compact["SUMMARY"] is None
    assert compact["DTSTART"] == {"TZID": ["Europe/Istanbul"]}
    # Repeated TZID conversion returns cached ZoneInfo object
    assert rev["DTSTART"].tzinfo is rev["DTEND"].tzinfo

    # Compat lazy empty-param mutation propagates into serialization (even after materialization)
    cal = Calendar.from_ical(ics)
    ev = cal.events[0]
    ev["SUMMARY"].params["LANGUAGE"] = "tr"
    out = cal.to_ical()
    assert b"SUMMARY;LANGUAGE=tr:Plain No Params" in out

    ev2 = Event.new(uid="mat-1", summary="Materialized Plain")
    ev2["SUMMARY"].params["LANGUAGE"] = "en"
    assert b"SUMMARY;LANGUAGE=en:Materialized Plain" in ev2.to_ical()


def test_rfc5545_event_and_todo_span_semantics_parity() -> None:
    """Event/Todo end and duration match upstream icalendar RFC 5545 semantics."""
    allday_ics = b"BEGIN:VEVENT\r\nUID:ad\r\nDTSTART;VALUE=DATE:20260701\r\nEND:VEVENT\r\n"
    up_ad = up_ical.Event.from_ical(allday_ics)
    ox_ad = Event.from_ical(allday_ics)
    assert ox_ad.start == up_ad.start == datetime.date(2026, 7, 1)
    assert ox_ad.end == up_ad.end == datetime.date(2026, 7, 2)
    assert ox_ad.duration == up_ad.duration == datetime.timedelta(days=1)

    timed_ics = b"BEGIN:VEVENT\r\nUID:tm\r\nDTSTART:20260701T100000Z\r\nEND:VEVENT\r\n"
    up_tm = up_ical.Event.from_ical(timed_ics)
    ox_tm = Event.from_ical(timed_ics)
    assert ox_tm.end == ox_tm.start
    assert ox_tm.duration == up_tm.duration == datetime.timedelta(0)

    ev = Event.new(
        uid="excl",
        start=datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=2),
    )
    ev.end = datetime.datetime(2026, 7, 1, 15, 0, tzinfo=datetime.timezone.utc)
    assert "DURATION" not in ev
    assert ev.duration == datetime.timedelta(hours=5)

    ev.duration = datetime.timedelta(hours=1)
    assert "DTEND" not in ev
    assert ev.end == datetime.datetime(2026, 7, 1, 11, 0, tzinfo=datetime.timezone.utc)

    td = Todo.new(
        uid="td-excl",
        start=datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=2),
    )
    td.end = datetime.datetime(2026, 7, 1, 14, 0, tzinfo=datetime.timezone.utc)
    assert "DUE" in td and "DURATION" not in td
    td.duration = datetime.timedelta(minutes=45)
    assert "DUE" not in td and "DURATION" in td


def test_icalendar_7x_new_constructors_across_all_components() -> None:
    """All component .new() constructors accept icalendar 7.x keyword arguments."""
    uid_val = uuid.UUID("11111111-2222-3333-4444-555555555555")
    now = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc)
    ev = Event.new(
        uid=uid_val,
        summary="Kickoff",
        start=now,
        end=now + datetime.timedelta(hours=1),
        categories=["Work", "Rust"],
        status="CONFIRMED",
        classification="PRIVATE",
        transparency="TRANSPARENT",
        priority=1,
        sequence=2,
        color="crimson",
        comments=["Note A", "Note B"],
        stamp=now,
        attendees=[vCalAddress.new("dev@example.com", cn="Dev")],
    )
    assert ev.uid == str(uid_val)
    assert ev.categories == ["Work", "Rust"]
    assert ev.status == "CONFIRMED"
    assert ev.classification == "PRIVATE"
    assert ev.transparency == "TRANSPARENT"
    assert ev.priority == 1
    assert ev.sequence == 2
    assert ev.color == "crimson"
    assert len(ev.attendees) == 1

    cal = Calendar.new(
        organization="Acme",
        name="Ops",
        language="TR",
        method="PUBLISH",
        categories=["Ops"],
        subcomponents=[ev],
    )
    assert cal.prodid == "-//Acme//Ops//TR"
    assert cal.method == "PUBLISH"
    assert cal.calendar_name == "Ops"
    assert len(cal.events) == 1

    slot = Available.new(uid="slot-1", summary="AM", start=now, duration=datetime.timedelta(hours=2))
    avail = Availability.new(uid="av-1", busy_type="BUSY", components=[slot], categories=["Hours"])
    assert len(avail.available) == 1
    assert avail.busy_type == "BUSY"

    td = Todo.new(uid="t1", summary="Do", end=now, priority=3, stamp=now)
    jn = Journal.new(uid="j1", summary="Log", description=["L1", "L2"], stamp=now)
    al = Alarm.new(action="EMAIL", trigger=datetime.timedelta(minutes=-10), uid="a1", summary="Ping")
    fb = FreeBusy.new(uid="fb1", start=now, end=now + datetime.timedelta(hours=1), organizer="mailto:a@b.c")
    raw_c = Component.new(stamp=now, comments="Hello", X_CUSTOM="42")
    assert td.end == now
    assert jn.descriptions == ["L1", "L2"]
    assert al.uid == "a1"
    assert fb.organizer == "mailto:a@b.c"
    assert raw_c.decoded("X-CUSTOM") == "42"


def test_v_types_and_vcaladdress_7x_parity() -> None:
    """vCalAddress.new/params and all v* properties match icalendar 7.3.2."""
    addr = vCalAddress.new(
        "alice@example.com",
        cn="Alice",
        cutype="INDIVIDUAL",
        partstat="ACCEPTED",
        role="REQ-PARTICIPANT",
        rsvp=True,
        sent_by="mailto:boss@example.com",
    )
    assert str(addr) == "mailto:alice@example.com"
    assert addr.email == "alice@example.com"
    assert addr.name == "Alice"
    assert addr.CN == "Alice"
    assert addr.CUTYPE == "INDIVIDUAL"
    assert addr.PARTSTAT == "ACCEPTED"
    assert addr.ROLE == "REQ-PARTICIPANT"
    assert addr.RSVP is True
    addr.name = "Alice B."
    addr.RSVP = False
    assert addr.CN == "Alice B."
    assert addr.params["RSVP"] == "FALSE"

    uri = vUri("https://example.com/doc", params={"FMTTYPE": "text/html", "LABEL": "Docs"})
    assert uri.uri == "https://example.com/doc"
    assert uri.FMTTYPE == "text/html"
    assert uri.LABEL == "Docs"

    wd = vWeekday("-1FR")
    assert wd.relative == -1
    assert wd.weekday == "FR"
    assert wd.week_days["SU"] == 0 and wd.week_days["SA"] == 6

    assert vDatetime(datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)).is_utc()
    assert not vDatetime(datetime.datetime(2026, 7, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))).is_utc()
    assert vTime(datetime.time(10, 0, tzinfo=datetime.timezone.utc)).is_utc()
    assert vMonth("5L").leap is True
    assert vMonth(5).leap is False
    assert vBoolean.BOOL_MAP["TRUE"] is True and vBoolean(True).ical_value is True
    assert vCategory(["A", "B"]).ical_value == ["A", "B"]
    assert vInt(7).ical_value == 7
    assert vFloat(1.5).ical_value == pytest.approx(1.5)
    assert vGeo((41.0, 29.0)).ical_value == (41.0, 29.0)
    assert vRecur.canonical_order[0] == "FREQ"
    assert vRecur(FREQ="DAILY", COUNT=2).ical_value == {"FREQ": ["DAILY"], "COUNT": [2]}

    per = vPeriod(
        (
            datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc),
            datetime.timedelta(hours=1),
        ),
        params={"FBTYPE": "BUSY"},
    )
    assert per.FBTYPE == "BUSY"
    assert per.ical_value[1] == datetime.timedelta(hours=1)
    assert isinstance(vDDDTypes(datetime.timedelta(minutes=5)).to_property_type(), vDuration)
    assert len(vDDDTypes(datetime.date(2026, 7, 1)).dts) == 1
    assert Timezone.from_tzid("UTC").tz_name == "UTC"


def test_recurring_ical_events_adapter_and_occurrence_parity() -> None:
    """OccurrenceID, Occurrence, ComponentAdapter, and CalendarQuery helpers behave cleanly."""
    dt = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)
    oid1 = OccurrenceID("VEVENT", "u1", dt, dt)
    oid2 = OccurrenceID("VEVENT", "u1", dt, dt)
    assert oid1 == oid2
    assert hash(oid1) == hash(oid2)
    assert tuple(oid1) == ("VEVENT", "u1", dt, dt)
    assert len(oid1) == 4
    assert oid1.count("u1") == 1 and oid1.index("VEVENT") == 0

    ev = Event.new(
        uid="series-1",
        summary="Standup",
        start=dt,
        duration=datetime.timedelta(minutes=30),
    )
    ev.add("RRULE", {"FREQ": "DAILY", "COUNT": 3})
    ev.add_component(Alarm.new_display(datetime.timedelta(minutes=-5), "Soon"))

    occ = Occurrence(ev, start=dt, end=dt + datetime.timedelta(minutes=30), recurrence_id=dt)
    assert occ.component_name == "VEVENT"
    assert occ.uid == "series-1"
    assert occ.id == oid1.__class__("VEVENT", "series-1", dt, dt)
    assert occ.has_alarm is True
    assert occ.is_in_span(dt - datetime.timedelta(minutes=10), dt + datetime.timedelta(minutes=10))
    stripped = occ.as_component(keep_recurrence_attributes=False)
    assert "RRULE" not in stripped and "RRULE" in ev

    adapter = ComponentAdapter(ev)
    assert adapter.uid == "series-1"
    assert adapter.start == dt
    assert adapter.end == dt + datetime.timedelta(minutes=30)
    assert adapter.duration == datetime.timedelta(minutes=30)
    assert adapter.has_recurrence_rules() is True
    assert adapter.is_modification() is False
    assert adapter.this_and_future() is False
    assert len(adapter.alarms) == 1
    assert CalendarQuery.ComponentsWithName is ComponentsWithName
