"""Red-team regression suite and real-world multi-client differential tests for v0.4.0."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat.icalendar import Calendar, Event, vCalAddress, vText, vUri
from icaloxide.compat.recurring_ical_events import of as compat_of


def test_red_team_bug1_multi_property_duplication_and_per_instance_params() -> None:
    """Materializing multi-valued properties must preserve count N (not N^2) and per-instance params."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:att-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"DTEND:20260701T110000Z\r\n"
        b"ATTENDEE;CN=Alice;ROLE=REQ-PARTICIPANT:mailto:alice@example.com\r\n"
        b"ATTENDEE;CN=Bob;PARTSTAT=ACCEPTED:mailto:bob@example.com\r\n"
        b"EXDATE;TZID=America/New_York:20260708T060000\r\n"
        b"EXDATE;TZID=Europe/Berlin:20260715T120000\r\n"
        b"SUMMARY:Multi Attendee\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = Calendar.from_ical(ics)
    ev = cal.events[0]

    # Trigger materialization via in-place mutation
    ev["LOCATION"] = "Room 101"
    raw = ev.to_ical()
    assert raw.count(b"ATTENDEE;") == 2
    assert raw.count(b"EXDATE;") == 2

    reparsed = Calendar.from_ical(cal.to_ical())
    rev = reparsed.events[0]
    standalone_ev = Event.from_ical(raw)
    assert len(standalone_ev["ATTENDEE"]) == 2
    attendees = rev["ATTENDEE"]
    assert isinstance(attendees, list)
    assert len(attendees) == 2
    assert attendees[0].params["CN"] == "Alice"
    assert attendees[0].params["ROLE"] == "REQ-PARTICIPANT"
    assert attendees[1].params["CN"] == "Bob"
    assert attendees[1].params["PARTSTAT"] == "ACCEPTED"

    # Also verify lazy unmaterialized get() preserves per-instance params
    lazy_cal = Calendar.from_ical(ics)
    lazy_atts = lazy_cal.events[0]["ATTENDEE"]
    assert len(lazy_atts) == 2
    assert lazy_atts[0].params["CN"] == "Alice"
    assert lazy_atts[1].params["CN"] == "Bob"


def test_red_team_bug2_live_child_mutation_propagates_to_parent_calendar() -> None:
    """Mutating a child component or its property .params must persist in Calendar.to_ical()."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:mut-1\r\n"
        b"DTSTART;TZID=America/New_York:20260701T090000\r\n"
        b"DTEND;TZID=America/New_York:20260701T100000\r\n"
        b"ATTENDEE;CN=Alice:mailto:alice@example.com\r\n"
        b"SUMMARY:Original\r\n"
        b"BEGIN:VALARM\r\n"
        b"ACTION:DISPLAY\r\n"
        b"TRIGGER:-PT15M\r\n"
        b"DESCRIPTION:OrigAlarm\r\n"
        b"END:VALARM\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = Calendar.from_ical(ics)
    cal.events[0]["SUMMARY"] = "Mutated Summary"
    cal.events[0].subcomponents[0]["DESCRIPTION"] = "UpdatedAlarm"
    cal.events[0]["ATTENDEE"].params["CN"] = "Alice Updated"

    out = cal.to_ical()
    assert b"SUMMARY:Mutated Summary" in out
    assert b"DESCRIPTION:UpdatedAlarm" in out
    assert b'CN="Alice Updated"' in out or b"CN=Alice Updated" in out

    # Also test Calendar.events[0]["DTSTART"].params mutation before Calendar materialization
    cal2 = Calendar.from_ical(ics)
    dt = cal2.events[0]["DTSTART"]
    dt.params["X-EDIT"] = "yes"
    out2 = cal2.to_ical()
    assert b"X-EDIT=yes" in out2


def test_red_team_bug3_root_vcalendar_properties_preserved_on_materialization() -> None:
    """Materializing Calendar keeps METHOD, CALSCALE, COLOR, X-*, etc. on root VCALENDAR."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Acme//Cal//EN\r\n"
        b"METHOD:REQUEST\r\n"
        b"CALSCALE:GREGORIAN\r\n"
        b"X-CUSTOM-CAL;X-FLAG=1:keep-me\r\n"
        b"COLOR:crimson\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:root-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"SUMMARY:Invite\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = Calendar.from_ical(ics)
    cal.add_component(Event.new(uid="root-2", summary="Added", start=datetime.date(2026, 7, 2)))
    out = cal.to_ical()

    assert b"METHOD:REQUEST" in out
    assert b"CALSCALE:GREGORIAN" in out
    assert b"COLOR:crimson" in out
    assert b"X-CUSTOM-CAL;X-FLAG=1:keep-me" in out
    assert b"SUMMARY:Added" in out


def test_red_team_bug4_text_uri_caladdress_wrappers_have_params() -> None:
    """Parsed text/caladdress/uri properties must expose .params matching icalendar."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"METHOD:PUBLISH\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:p-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"SUMMARY;LANGUAGE=tr-TR:Toplanti\r\n"
        b"ATTENDEE;CN=Mert;PARTSTAT=TENTATIVE:mailto:mert@example.com\r\n"
        b"URL;VALUE=URI:https://example.com/meet\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = Calendar.from_ical(ics)
    ev = cal.events[0]

    summary = ev["SUMMARY"]
    attendee = ev["ATTENDEE"]
    url = ev["URL"]

    assert isinstance(summary, vText)
    assert isinstance(summary, str)
    assert summary == "Toplanti"
    assert summary.params["LANGUAGE"] == "tr-TR"

    assert isinstance(attendee, vCalAddress)
    assert attendee == "mailto:mert@example.com"
    assert attendee.params["CN"] == "Mert"
    assert attendee.params["PARTSTAT"] == "TENTATIVE"

    assert isinstance(url, vUri)
    assert url == "https://example.com/meet"
    assert url.params["VALUE"] == "URI"

    assert isinstance(cal["METHOD"], vText)
    assert cal["METHOD"] == "PUBLISH"


def test_red_team_bug5_historic_and_far_future_all_and_paginate() -> None:
    """Unbounded all(), paginate(), and after(None) must include pre-1900 and post-2100 events."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:hist-1850\r\n"
        b"DTSTART:18500315T090000Z\r\n"
        b"DTEND:18500315T100000Z\r\n"
        b"SUMMARY:Historic\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:fut-2150\r\n"
        b"DTSTART:21500315T090000Z\r\n"
        b"DTEND:21500315T100000Z\r\n"
        b"SUMMARY:Future\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = icaloxide.Calendar.from_ical(ics)
    q = cal.query()
    all_occs = q.all()
    assert [o.summary for o in all_occs] == ["Historic", "Future"]
    assert q.count_all() == 2

    pages = list(q.paginate(10))
    assert len(pages) == 1
    assert [o.summary for o in pages[0]] == ["Historic", "Future"]

    after_all = list(q.after())
    assert [o.summary for o in after_all] == ["Historic", "Future"]

    compat_cal = Calendar.from_ical(ics)
    compat_q = compat_of(compat_cal)
    assert [e.summary for e in compat_q.all()] == ["Historic", "Future"]
    compat_pages = list(compat_q.paginate(10))
    assert [e.summary for e in compat_pages[0]] == ["Historic", "Future"]


def test_red_team_bug6_until_before_dtstart_emits_zero() -> None:
    """RRULE with UNTIL < DTSTART must emit 0 occurrences, matching upstream."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:bad-until\r\n"
        b"DTSTART:20260510T090000Z\r\n"
        b"DTEND:20260510T100000Z\r\n"
        b"RRULE:FREQ=DAILY;UNTIL=20260501T000000Z\r\n"
        b"SUMMARY:Never\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    up_cal = up_ical.Calendar.from_ical(ics)
    up_res = up_rie.of(up_cal).between(datetime.date(2026, 5, 1), datetime.date(2026, 6, 1))
    assert len(up_res) == 0

    ox_cal = icaloxide.Calendar.from_ical(ics)
    ox_q = ox_cal.query()
    assert ox_q.between(datetime.date(2026, 5, 1), datetime.date(2026, 6, 1)) == []
    assert ox_q.all() == []
    assert ox_q.count_all() == 0
    assert ox_q.first() is None


def test_red_team_bug7_cross_timezone_dtstart_and_dtend() -> None:
    """Flight-style events with different DTSTART/DTEND TZIDs must preserve both zones."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//Test//Test//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:flight-jfk-ist\r\n"
        b"DTSTART;TZID=America/New_York:20260701T180000\r\n"
        b"DTEND;TZID=Europe/Istanbul:20260702T110000\r\n"
        b"SUMMARY:Flight JFK-IST\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    cal = icaloxide.Calendar.from_ical(ics)
    occs = cal.query().between(datetime.date(2026, 7, 1), datetime.date(2026, 7, 3))
    assert len(occs) == 1
    occ = occs[0]

    assert occ.start == datetime.datetime(
        2026, 7, 1, 18, 0, tzinfo=ZoneInfo("America/New_York")
    )
    assert occ.end == datetime.datetime(
        2026, 7, 2, 11, 0, tzinfo=ZoneInfo("Europe/Istanbul")
    )
    assert occ.end.utcoffset() == datetime.timedelta(hours=3)


def test_calendar_builder_extended_methods() -> None:
    """CalendarBuilder supports set_property, add_journal, and add_alarm."""
    builder = icaloxide.CalendarBuilder(prodid="-//icaloxide//v0.4//EN", name="Work")
    builder.set_property("METHOD", "PUBLISH")
    builder.set_property("X-WR-CALNAME", "Engineering")

    builder.add_event(
        uid="ev-1",
        summary="Release Sync",
        start=datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=1),
    )
    builder.add_alarm(
        trigger=datetime.timedelta(minutes=-15),
        description="Starting soon",
        duration=datetime.timedelta(minutes=5),
        repeat=2,
    )
    builder.add_journal(
        uid="jn-1",
        summary="Release Notes",
        start=datetime.date(2026, 8, 1),
        description="v0.4.0 shipped",
    )

    cal = builder.build()
    assert len(cal.events) == 1
    assert len(cal.journals) == 1
    assert cal.root["METHOD"] == "PUBLISH"
    assert cal.root["X-WR-CALNAME"] == "Engineering"
    assert len(cal.events[0].subcomponents) == 1
    alarm = cal.events[0].subcomponents[0]
    assert alarm.name == "VALARM"
    assert alarm["TRIGGER"] == datetime.timedelta(minutes=-15)
    assert alarm["REPEAT"] == 2

    with pytest.raises(ValueError, match="no parent component"):
        empty_builder = icaloxide.CalendarBuilder()
        empty_builder.add_alarm(trigger=datetime.timedelta(minutes=-5))


def _norm_dt(val: object) -> object:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is not None:
            return val.astimezone(datetime.timezone.utc)
        return val
    return val


@pytest.mark.parametrize(
    ("client_name", "ics", "win_start", "win_end"),
    [
        (
            "google_calendar_dst_and_exdate",
            (
                b"BEGIN:VCALENDAR\r\n"
                b"PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
                b"VERSION:2.0\r\n"
                b"CALSCALE:GREGORIAN\r\n"
                b"METHOD:PUBLISH\r\n"
                b"X-WR-CALNAME:Team Calendar\r\n"
                b"X-WR-TIMEZONE:America/New_York\r\n"
                b"BEGIN:VTIMEZONE\r\n"
                b"TZID:America/New_York\r\n"
                b"X-LIC-LOCATION:America/New_York\r\n"
                b"BEGIN:DAYLIGHT\r\n"
                b"TZOFFSETFROM:-0500\r\n"
                b"TZOFFSETTO:-0400\r\n"
                b"TZNAME:EDT\r\n"
                b"DTSTART:19700308T020000\r\n"
                b"RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
                b"END:DAYLIGHT\r\n"
                b"BEGIN:STANDARD\r\n"
                b"TZOFFSETFROM:-0400\r\n"
                b"TZOFFSETTO:-0500\r\n"
                b"TZNAME:EST\r\n"
                b"DTSTART:19701101T020000\r\n"
                b"RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
                b"END:STANDARD\r\n"
                b"END:VTIMEZONE\r\n"
                b"BEGIN:VEVENT\r\n"
                b"DTSTART;TZID=America/New_York:20260301T090000\r\n"
                b"DTEND;TZID=America/New_York:20260301T093000\r\n"
                b"RRULE:FREQ=WEEKLY;WKST=SU;COUNT=5\r\n"
                b"EXDATE;TZID=America/New_York:20260315T090000\r\n"
                b"UID:google-dst-1\r\n"
                b"SUMMARY:Weekly Standup\r\n"
                b"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;CN=Alice:mailto:a@ex.com\r\n"
                b"END:VEVENT\r\n"
                b"END:VCALENDAR\r\n"
            ),
            datetime.date(2026, 3, 1),
            datetime.date(2026, 4, 1),
        ),
        (
            "outlook_windows_tzid_override",
            (
                b"BEGIN:VCALENDAR\r\n"
                b"PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN\r\n"
                b"VERSION:2.0\r\n"
                b"METHOD:REQUEST\r\n"
                b"BEGIN:VTIMEZONE\r\n"
                b"TZID:Turkey Standard Time\r\n"
                b"BEGIN:STANDARD\r\n"
                b"DTSTART:16010101T000000\r\n"
                b"TZOFFSETFROM:+0300\r\n"
                b"TZOFFSETTO:+0300\r\n"
                b"END:STANDARD\r\n"
                b"END:VTIMEZONE\r\n"
                b"BEGIN:VEVENT\r\n"
                b"UID:outlook-tr-1\r\n"
                b"DTSTART;TZID=\"Turkey Standard Time\":20260601T140000\r\n"
                b"DTEND;TZID=\"Turkey Standard Time\":20260601T150000\r\n"
                b"RRULE:FREQ=DAILY;COUNT=4\r\n"
                b"SUMMARY:Sync\r\n"
                b"END:VEVENT\r\n"
                b"BEGIN:VEVENT\r\n"
                b"UID:outlook-tr-1\r\n"
                b"RECURRENCE-ID;TZID=\"Turkey Standard Time\":20260603T140000\r\n"
                b"DTSTART;TZID=\"Turkey Standard Time\":20260603T160000\r\n"
                b"DTEND;TZID=\"Turkey Standard Time\":20260603T170000\r\n"
                b"SUMMARY:Sync Moved\r\n"
                b"SEQUENCE:1\r\n"
                b"END:VEVENT\r\n"
                b"END:VCALENDAR\r\n"
            ),
            datetime.date(2026, 6, 1),
            datetime.date(2026, 6, 6),
        ),
        (
            "apple_calendar_all_day_and_alarm",
            (
                b"BEGIN:VCALENDAR\r\n"
                b"VERSION:2.0\r\n"
                b"PRODID:-//Apple Inc.//macOS Calendar 15.0//EN\r\n"
                b"CALSCALE:GREGORIAN\r\n"
                b"BEGIN:VEVENT\r\n"
                b"UID:apple-allday-1\r\n"
                b"DTSTART;VALUE=DATE:20260901\r\n"
                b"DTEND;VALUE=DATE:20260903\r\n"
                b"RRULE:FREQ=MONTHLY;BYMONTHDAY=1;COUNT=3\r\n"
                b"SUMMARY:Offsite\r\n"
                b"BEGIN:VALARM\r\n"
                b"ACTION:DISPLAY\r\n"
                b"DESCRIPTION:Reminder\r\n"
                b"TRIGGER:-PT9H\r\n"
                b"END:VALARM\r\n"
                b"END:VEVENT\r\n"
                b"END:VCALENDAR\r\n"
            ),
            datetime.date(2026, 9, 1),
            datetime.date(2026, 12, 1),
        ),
        (
            "thunderbird_rdate_mozilla_prefix",
            (
                b"BEGIN:VCALENDAR\r\n"
                b"PRODID:-//Mozilla.org/NONSGML Mozilla Calendar V1.1//EN\r\n"
                b"VERSION:2.0\r\n"
                b"BEGIN:VEVENT\r\n"
                b"UID:tb-1\r\n"
                b"SUMMARY:Quarterly\r\n"
                b"DTSTART;TZID=Europe/Berlin:20260115T100000\r\n"
                b"DURATION:PT45M\r\n"
                b"RDATE;TZID=Europe/Berlin:20260220T100000,20260410T100000\r\n"
                b"END:VEVENT\r\n"
                b"END:VCALENDAR\r\n"
            ),
            datetime.date(2026, 1, 1),
            datetime.date(2026, 5, 1),
        ),
        (
            "lotus_notes_vendor_props",
            (
                b"BEGIN:VCALENDAR\r\n"
                b"X-LOTUS-CHARSET:UTF-8\r\n"
                b"VERSION:2.0\r\n"
                b"PRODID:-//Lotus Development Corporation//NONSGML Notes 9.0//EN\r\n"
                b"METHOD:PUBLISH\r\n"
                b"BEGIN:VEVENT\r\n"
                b"UID:lotus-1\r\n"
                b"DTSTART:20260501T080000Z\r\n"
                b"DTEND:20260501T090000Z\r\n"
                b"RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3\r\n"
                b"X-LOTUS-NOTESVERSION:2\r\n"
                b"SUMMARY:Board Call\r\n"
                b"END:VEVENT\r\n"
                b"END:VCALENDAR\r\n"
            ),
            datetime.date(2026, 5, 1),
            datetime.date(2026, 6, 15),
        ),
    ],
)
def test_real_world_client_corpus_differential_parity(
    client_name: str,
    ics: bytes,
    win_start: datetime.date,
    win_end: datetime.date,
) -> None:
    """Differential parity across real Google/Outlook/Apple/Thunderbird/Lotus payloads."""
    up_cal = up_ical.Calendar.from_ical(ics)
    up_events = up_rie.of(up_cal).between(win_start, win_end)
    up_tuples = sorted(
        (str(e["SUMMARY"]), _norm_dt(e.decoded("DTSTART")), _norm_dt(e.decoded("DTEND")))
        for e in up_events
    )

    ox_cal = Calendar.from_ical(ics)
    ox_events = compat_of(ox_cal).between(win_start, win_end)
    ox_tuples = sorted(
        (str(e["SUMMARY"]), _norm_dt(e.decoded("DTSTART")), _norm_dt(e.decoded("DTEND")))
        for e in ox_events
    )

    assert ox_tuples == up_tuples, f"mismatch for {client_name}"
