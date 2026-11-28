"""v0.9.1 deep-audit regression tests: occurrence mutations, VALUE=TIME/UTC-OFFSET, CATEGORIES, xCal, props_extra."""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET

import icalendar as up_ical
import icaloxide
from icaloxide.compat.icalendar import Calendar, vUTCOffset
from icaloxide.compat.props_extra import vAdr, vN, vOrg
from icaloxide.compat.recurring_ical_events import of as compat_of
from icaloxide.compat.xcal import XCAL_NS


def test_modify_and_complete_occurrence_preserve_rid_span_and_hyphen_keys() -> None:
    """modify_occurrence / complete_occurrence keep occurrence on recurrence_id, shift DTEND, and hyphenate PERCENT-COMPLETE."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//M//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:ad1\r\nDTSTART;VALUE=DATE:20260701\r\nDTEND;VALUE=DATE:20260702\r\nRRULE:FREQ=DAILY;COUNT=4\r\nSUMMARY:Base\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:tm1\r\nDTSTART:20260701T090000Z\r\nDTEND:20260701T103000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Meet\r\nEND:VEVENT\r\n"
        b"BEGIN:VTODO\r\nUID:td1\r\nDTSTART:20260701T090000Z\r\nDUE:20260701T110000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Task\r\nEND:VTODO\r\n"
        b"END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    cal.cancel_occurrence("ad1", datetime.date(2026, 7, 2))
    cal.modify_occurrence("ad1", datetime.date(2026, 7, 3), summary="ChangedDay")
    cal.modify_occurrence("tm1", datetime.datetime(2026, 7, 2, 9, 0, tzinfo=datetime.timezone.utc), summary="ChangedMeet")
    cal.complete_occurrence("td1", datetime.datetime(2026, 7, 2, 9, 0, tzinfo=datetime.timezone.utc))

    ad_occs = [(e.decoded("DTSTART"), e.decoded("DTEND"), e.decoded("SUMMARY")) for e in compat_of(cal).between(datetime.date(2026, 7, 1), datetime.date(2026, 7, 6)) if e.uid == "ad1"]
    assert ad_occs == [
        (datetime.date(2026, 7, 1), datetime.date(2026, 7, 2), "Base"),
        (datetime.date(2026, 7, 3), datetime.date(2026, 7, 4), "ChangedDay"),
        (datetime.date(2026, 7, 4), datetime.date(2026, 7, 5), "Base"),
    ]

    tm_occs = [(e.decoded("DTSTART"), e.decoded("DTEND"), e.decoded("SUMMARY")) for e in compat_of(cal).between(datetime.date(2026, 7, 1), datetime.date(2026, 7, 6)) if e.uid == "tm1"]
    assert tm_occs == [
        (datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc), datetime.datetime(2026, 7, 1, 10, 30, tzinfo=datetime.timezone.utc), "Meet"),
        (datetime.datetime(2026, 7, 2, 9, 0, tzinfo=datetime.timezone.utc), datetime.datetime(2026, 7, 2, 10, 30, tzinfo=datetime.timezone.utc), "ChangedMeet"),
        (datetime.datetime(2026, 7, 3, 9, 0, tzinfo=datetime.timezone.utc), datetime.datetime(2026, 7, 3, 10, 30, tzinfo=datetime.timezone.utc), "Meet"),
    ]

    td_ov = [c for c in cal.todos if "RECURRENCE-ID" in c][0]
    assert td_ov.decoded("PERCENT-COMPLETE") == 100
    assert "PERCENT_COMPLETE" not in td_ov
    assert td_ov.decoded("DTSTART") == datetime.datetime(2026, 7, 2, 9, 0, tzinfo=datetime.timezone.utc)
    assert td_ov.decoded("DUE") == datetime.datetime(2026, 7, 2, 11, 0, tzinfo=datetime.timezone.utc)


def test_value_time_utc_leap_second_and_value_utc_offset() -> None:
    """VALUE=TIME Z preserves tzinfo=UTC across roundtrips; VALUE=UTC-OFFSET wraps as vUTCOffset."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:vt1\r\nDTSTART:20260701T100000Z\r\n"
        b"X-TIME-Z;VALUE=TIME:143000Z\r\n"
        b"X-TIME-NAIVE;VALUE=TIME:091500\r\n"
        b"X-TIME-LEAP;VALUE=TIME:235960Z\r\n"
        b"X-OFFSET;VALUE=UTC-OFFSET:+053015\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    nat = icaloxide.Calendar.from_ical(ics)
    assert nat.events[0]["X-TIME-Z"] == datetime.time(14, 30, 0, tzinfo=datetime.timezone.utc)
    assert nat.events[0]["X-TIME-NAIVE"] == datetime.time(9, 15, 0)
    assert nat.events[0]["X-TIME-LEAP"] == datetime.time(23, 59, 59, tzinfo=datetime.timezone.utc)
    assert b"X-TIME-Z;VALUE=TIME:143000Z" in nat.to_ical()

    cal = Calendar.from_ical(ics)
    ev = cal.events[0]
    assert isinstance(ev.get("X-OFFSET"), vUTCOffset)
    ev["X-TOUCH"] = "1"  # materialize
    rt_bytes = cal.to_ical()
    assert b"X-OFFSET;VALUE=UTC-OFFSET:+053015" in rt_bytes
    assert b"PT5H30M15S" not in rt_bytes
    assert b"X-TIME-Z;VALUE=TIME:143000Z" in rt_bytes

    up_rt = up_ical.Calendar.from_ical(rt_bytes)
    assert up_rt.events[0]["X-TIME-Z"].dt == datetime.time(14, 30, 0, tzinfo=datetime.timezone.utc)
    assert up_rt.events[0]["X-OFFSET"].td == datetime.timedelta(hours=5, minutes=30, seconds=15)

    cal_jc = Calendar.from_jcal(cal.to_jcal())
    assert cal_jc.events[0].decoded("X-TIME-Z") == datetime.time(14, 30, 0, tzinfo=datetime.timezone.utc)
    cal_xc = Calendar.from_xcal(cal.to_xcal())
    assert cal_xc.events[0].decoded("X-TIME-Z") == datetime.time(14, 30, 0, tzinfo=datetime.timezone.utc)


def test_multiline_categories_request_status_fastpath_xcal_and_props_extra() -> None:
    """Multi-line CATEGORIES flatten; xCal PERIOD/REQUEST-STATUS follow RFC 6321; vAdr/vN/vOrg avoid double-unescape."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//C//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:cat-1\r\nDTSTART:20260701T100000Z\r\n"
        b"CATEGORIES:Alpha,Beta\\,Two\r\nCATEGORIES:Gamma\r\n"
        b"REQUEST-STATUS:2.0;Success;Extra\\;Info\r\n"
        b"RDATE;VALUE=PERIOD:20260702T100000Z/PT1H,20260703T100000Z/20260703T120000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    ev = cal.events[0]
    assert ev.categories == ["Alpha", "Beta,Two", "Gamma"]

    ev["X-MAT"] = "1"
    raw = cal.to_ical()
    assert b"REQUEST-STATUS:2.0;Success;Extra\\;Info" in raw
    assert up_ical.Calendar.from_ical(raw).events[0].decoded("REQUEST-STATUS") == "2.0;Success;Extra;Info"

    xc = cal.to_xcal()
    root = ET.fromstring(xc)
    assert root.find(f".//{{{XCAL_NS}}}request-status/{{{XCAL_NS}}}code") is not None
    assert root.find(f".//{{{XCAL_NS}}}request-status/{{{XCAL_NS}}}description") is not None
    assert root.find(f".//{{{XCAL_NS}}}request-status/{{{XCAL_NS}}}data") is not None
    assert root.find(f".//{{{XCAL_NS}}}rdate/{{{XCAL_NS}}}period/{{{XCAL_NS}}}start") is not None
    assert root.find(f".//{{{XCAL_NS}}}rdate/{{{XCAL_NS}}}period/{{{XCAL_NS}}}duration") is not None
    assert root.find(f".//{{{XCAL_NS}}}rdate/{{{XCAL_NS}}}period/{{{XCAL_NS}}}end") is not None

    cal_xc = Calendar.from_xcal(xc)
    assert cal_xc.events[0].decoded("REQUEST-STATUS") == "2.0;Success;Extra;Info"
    assert len(cal_xc.events[0].decoded("RDATE")) == 2

    # props_extra single-pass unescaping: literal \\n must NOT become \ + newline
    adr = vAdr.from_ical(r";;Line1\nLine2;Literal\\nPath;Region\;1;12345;Country\,A")
    assert adr.street == "Line1\nLine2"
    assert adr.locality == r"Literal\nPath"
    assert adr.region == "Region;1"
    assert adr.country == "Country,A"
    assert vN.from_ical(r"Doe\\n;Jane\;").family == r"Doe\n"
    assert vOrg.from_ical(r"Acme\\n;R\&D\;1") == (r"Acme\n", r"R\&D;1")
