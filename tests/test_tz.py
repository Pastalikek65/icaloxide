import datetime

import icaloxide


def test_windows_outlook_tzid_and_default_timezone() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN\r\n"
        "X-WR-TIMEZONE:Turkey Standard Time\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:win-1\r\n"
        "DTSTART;TZID=\"Turkey Standard Time\":20261127T140000\r\n"
        "DTEND;TZID=\"W. Europe Standard Time\":20260715T150000\r\n"
        "SUMMARY:Outlook Sync\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    assert cal.default_timezone == "Turkey Standard Time"

    ev = cal.events[0]
    dtstart = ev["DTSTART"]
    dtend = ev["DTEND"]
    assert dtstart.utcoffset() == datetime.timedelta(hours=3)
    assert dtend.utcoffset() == datetime.timedelta(hours=2)

    assert cal.resolve_utc_offset(
        "Eastern Standard Time", datetime.datetime(2026, 1, 15, 12, 0)
    ) == datetime.timedelta(hours=-5)
    assert cal.resolve_utc_offset(
        "Eastern Standard Time", datetime.datetime(2026, 7, 15, 12, 0)
    ) == datetime.timedelta(hours=-4)


def test_custom_vtimezone_summer_and_winter_offsets() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//custom-vtz//EN\r\n"
        "BEGIN:VTIMEZONE\r\n"
        "TZID:X-FICTIONAL-ZONE\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "TZOFFSETFROM:+0200\r\n"
        "TZOFFSETTO:+0300\r\n"
        "TZNAME:FXD\r\n"
        "DTSTART:20200329T020000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n"
        "END:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\n"
        "TZOFFSETFROM:+0300\r\n"
        "TZOFFSETTO:+0200\r\n"
        "TZNAME:FXS\r\n"
        "DTSTART:20201025T030000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
        "END:STANDARD\r\n"
        "END:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:w1\r\n"
        "DTSTART;TZID=X-FICTIONAL-ZONE:20260110T090000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:s1\r\n"
        "DTSTART;TZID=X-FICTIONAL-ZONE:20260710T090000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    assert cal.events[0]["DTSTART"].utcoffset() == datetime.timedelta(hours=2)
    assert cal.events[1]["DTSTART"].utcoffset() == datetime.timedelta(hours=3)


def test_add_missing_timezones_roundtrip() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//tzgen//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:g1\r\n"
        "DTSTART;TZID=Europe/Berlin:20260715T100000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    assert len(cal.timezones) == 0

    enriched = cal.add_missing_timezones()
    assert len(cal.timezones) == 0  # original frozen Calendar unchanged
    assert len(enriched.timezones) == 1
    assert enriched.timezones[0]["TZID"] == "Europe/Berlin"

    reparsed = icaloxide.Calendar.from_ical(enriched.to_ical(), strict=True)
    assert len(reparsed.timezones) == 1
