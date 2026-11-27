from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import icalendar as up_ical
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat import (
    Alarm,
    Alarms,
    Calendar,
    Component,
    ComponentAdapter,
    ComponentFactory,
    Event,
    ICalParsingError,
    JCalParsingError,
    Parameters,
    Timezone,
    TypesFactory,
    of,
    vAdr,
    vBinary,
    vBoolean,
    vCategory,
    vDate,
    vDatetime,
    vDDDLists,
    vDDDTypes,
    vDuration,
    vFloat,
    vGeo,
    vInt,
    vN,
    vOrg,
    vPeriod,
    vRecur,
    vText,
    vTime,
    vUTCOffset,
)


def test_byweekno_cross_year_matches_upstream_rie() -> None:
    for ics in [
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:w1\r\nDTSTART:20250101T090000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO;COUNT=5\r\nSUMMARY:W1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:wl\r\nDTSTART:20251228T090000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=YEARLY;BYWEEKNO=-1;BYDAY=SU;COUNT=4\r\nSUMMARY:WL\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:ws\r\nDTSTART:20250101T090000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=YEARLY;BYWEEKNO=1;WKST=SU;BYDAY=SU;COUNT=4\r\nSUMMARY:WS\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    ]:
        start = datetime.datetime(2024, 12, 25, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2030, 1, 15, tzinfo=datetime.timezone.utc)
        up_starts = sorted(e.decoded("DTSTART") for e in up_rie.of(up_ical.Calendar.from_ical(ics)).between(start, end))
        lo_starts = sorted(e.decoded("DTSTART") for e in of(Calendar.from_ical(ics)).between(start, end))
        assert lo_starts == up_starts


def test_vutcoffset_vbinary_vperiod_and_all_v_from_to_jcal() -> None:
    # Fix vUTCOffset (+0300 not PT3H) & Timezone.from_tzinfo fixed offset round-trip
    off = vUTCOffset(datetime.timedelta(hours=3))
    assert off.to_ical() == b"+0300"
    assert vUTCOffset.from_ical(b"-0530") == datetime.timedelta(hours=-5, minutes=-30)
    assert off.to_jcal("tzoffsetto")[2:] == ["utc-offset", "+03:00"]

    tz_fixed = Timezone.from_tzinfo(datetime.timezone(datetime.timedelta(hours=3)))
    tz_ics = tz_fixed.to_ical()
    assert b"TZOFFSETFROM:+0300" in tz_ics
    assert b"TZOFFSETTO:+0300" in tz_ics
    assert b"PT3H" not in tz_ics
    cal_tz = Calendar.new()
    cal_tz.add_component(tz_fixed)
    up_parsed = up_ical.Calendar.from_jcal(cal_tz.to_json())
    assert len(up_parsed.timezones) == 1

    # Fix vBinary base64 encoding/decoding
    vb = vBinary(b"hello world")
    assert vb.to_ical() == b"aGVsbG8gd29ybGQ="
    assert vBinary.from_ical(b"aGVsbG8gd29ybGQ=") == b"hello world"
    assert vBinary.from_jcal(vb.to_jcal()).to_ical() == b"aGVsbG8gd29ybGQ="

    # All v* classes support from_ical / to_jcal / from_jcal
    dt = datetime.datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert vDDDTypes.from_ical(b"20260501T100000", params={"TZID": "Europe/Istanbul"}) == dt
    assert vDDDTypes.from_jcal(vDDDTypes(dt).to_jcal()).dt == dt
    assert vText.from_ical(b"Hi\\, team\\; go\\nnow") == "Hi, team; go\nnow"
    assert vText.from_jcal(vText("Hello").to_jcal()) == "Hello"
    assert vGeo.from_ical(b"41.0;29.0") == (41.0, 29.0)
    assert vGeo.from_jcal(vGeo((41.0, 29.0)).to_jcal()).latitude == 41.0
    assert vBoolean.from_ical(b"TRUE") is True
    assert bool(vBoolean.from_jcal(vBoolean(False).to_jcal())) is False
    assert vInt.from_ical(b"42") == 42
    assert int(vInt.from_jcal(vInt(7).to_jcal())) == 7
    assert vFloat.from_ical(b"3.5") == 3.5
    assert float(vFloat.from_jcal(vFloat(2.25).to_jcal())) == 2.25
    assert vDuration.from_ical(b"-PT15M") == datetime.timedelta(minutes=-15)
    assert vDuration.from_jcal(vDuration(datetime.timedelta(hours=2)).to_jcal()).dt == datetime.timedelta(hours=2)
    per = vPeriod((datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc), datetime.timedelta(hours=1)))
    assert vPeriod.from_ical(per.to_ical()) == (per.start, per.end_or_dur)
    assert vPeriod.from_jcal(per.to_jcal()).start == per.start
    assert vCategory.from_ical(b"Work,R\\&D\\,Core") == ["Work", "R\\&D,Core"]
    assert len(vCategory.from_jcal(vCategory(["A", "B"]).to_jcal()).cats) == 2
    assert vDate(datetime.date(2026, 5, 1)).to_jcal()[2:] == ["date", "2026-05-01"]
    assert vDatetime(dt).to_jcal()[2] == "date-time"
    assert vTime.from_ical(b"103000Z") == datetime.time(10, 30, 0, tzinfo=datetime.timezone.utc)
    assert vDDDLists.from_jcal(vDDDLists([datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)]).to_jcal()).dts[0].dt == datetime.date(2026, 1, 1)
    assert vRecur.from_jcal(vRecur({"FREQ": "WEEKLY", "COUNT": 2}).to_jcal())["COUNT"] == [2]

    # vAdr / vN / vOrg & Factories
    adr = vAdr(("", "", "123 Main", "Istanbul", "TR", "34000", "Turkey"))
    assert vAdr.from_ical(adr.to_ical()).locality == "Istanbul"
    assert vN.from_jcal(vN(("Doe", "Jane", "", "Dr.", "")).to_jcal()).fields.given == "Jane"
    assert vOrg.from_ical(vOrg(["Acme", "R&D"]).to_ical()) == ("Acme", "R&D")
    assert ComponentFactory().get_component_class("VEVENT") is Event
    assert TypesFactory().for_property("SUMMARY") is vText


def test_component_parity_uppercase_props_errors_and_custom_adapter() -> None:
    ev1 = Event.new(
        uid="cmp-1",
        summary="Check",
        start=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=1),
    )
    ev1.stamp()
    ev2 = ev1.copy()
    assert ev1 == ev2
    assert ev1.DTSTART == ev2.start
    assert ev1.DURATION == datetime.timedelta(hours=1)
    assert ev1.DTSTAMP is not None
    assert ev1.is_empty() is False

    al = Alarm.new_display(datetime.timedelta(minutes=-10), "Ping")
    al["TRIGGER"].params["RELATED"] = "end"
    assert al.ACTION == "DISPLAY"
    assert al.TRIGGER_RELATED == "END"
    assert Alarms(ev1).active == []
    ev1.add_component(al)
    times = Alarms(ev1).times
    assert times[0].action == "DISPLAY"

    p = Parameters({"CN": "Alice"})
    p.update_tzid_from(datetime.datetime(2026, 1, 1, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul")))
    assert p.tzid() == "Europe/Istanbul"

    with pytest.raises(ICalParsingError):
        Calendar.from_ical(b"BEGIN:VEVENT\r\nEND:VEVENT\r\n", strict=True)
    with pytest.raises(JCalParsingError):
        Component.from_jcal("not-valid-json")

    class CustomXAdapter(ComponentAdapter):
        name = "X-CUSTOM"

    cal = Calendar.example()
    xcomp = Component()
    xcomp.name = "X-CUSTOM"
    xcomp.add("UID", "xc-1")
    xcomp.add("DTSTART", datetime.datetime(2026, 1, 1, 14, 0, tzinfo=datetime.timezone.utc))
    xcomp.add("DURATION", datetime.timedelta(hours=1))
    cal.add_component(xcomp)

    q = of(cal, components=["VEVENT", CustomXAdapter])
    hits = q.between(datetime.date(2026, 1, 1), datetime.date(2026, 1, 2))
    assert any(c.name == "X-CUSTOM" for c in hits)
    assert any(c.name == "VEVENT" for c in hits)
