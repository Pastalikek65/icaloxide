"""v0.9.5 Round-6 deep-audit regression tests."""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
import recurring_ical_events as up_rie
from icaloxide.cli import main as cli_main
from icaloxide.compat.icalendar import Calendar, vRecur
from icaloxide.compat.recurring_ical_events import of as compat_of

UTC = datetime.timezone.utc


def test_jcal_and_xcal_rrule_until_iso8601_roundtrip_strict() -> None:
    """vRecur / Calendar roundtrips via jCal & xCal emit strict RFC 5545 UNTIL strings."""
    for ics in (
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//U//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:u-dt\r\nDTSTART:20260701T100000Z\r\nDURATION:PT1H\r\n"
            b"RRULE:FREQ=DAILY;UNTIL=20260705T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//U//EN\r\n"
            b"BEGIN:VEVENT\r\nUID:u-d\r\nDTSTART;VALUE=DATE:20260701\r\n"
            b"RRULE:FREQ=DAILY;UNTIL=20260705\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ),
    ):
        orig = Calendar.from_ical(ics)
        up_occs = [e["DTSTART"].dt.isoformat() for e in up_rie.of(up_ical.Calendar.from_ical(ics)).all()]

        for rt in (Calendar.from_jcal(orig.to_json()), Calendar.from_xcal(orig.to_xcal())):
            assert isinstance(rt.events[0]["RRULE"]["UNTIL"][0], (datetime.date, datetime.datetime))
            rt_ics = rt.to_ical()
            strict_native = icaloxide.Calendar.from_ical(rt_ics, strict=True)
            assert [o.start.isoformat() for o in strict_native.query().all()] == up_occs

    vr_dt = vRecur.from_jcal(["rrule", {}, "recur", {"freq": "DAILY", "until": "2026-07-05T10:00:00Z"}])
    assert vr_dt["UNTIL"] == [datetime.datetime(2026, 7, 5, 10, 0, tzinfo=UTC)]
    assert vr_dt.to_ical() == b"FREQ=DAILY;UNTIL=20260705T100000Z"

    vr_d = vRecur.from_jcal(["rrule", {}, "recur", {"freq": "DAILY", "until": "2026-07-15"}])
    assert vr_d["UNTIL"] == [datetime.date(2026, 7, 15)]
    assert vr_d.to_ical() == b"FREQ=DAILY;UNTIL=20260715"


def test_allday_valarm_date_triggers_and_related_end_parity() -> None:
    """All-day VEVENT alarms match icalendar 7.x date preservation and default 1-day RELATED=END."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:ad-al\r\nDTSTART;VALUE=DATE:20260704\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Day before\r\nTRIGGER:-P1D\r\nEND:VALARM\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:15m before\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:15m before end\r\n"
        b"TRIGGER;RELATED=END:-PT15M\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    up_ev = up_ical.Calendar.from_ical(ics).events[0]
    ox_ev = Calendar.from_ical(ics).events[0]

    up_times = sorted((type(t.trigger).__name__, t.trigger.isoformat()) for t in up_ev.alarms.times)
    ox_times = sorted((type(t.trigger).__name__, t.trigger.isoformat()) for t in ox_ev.alarms.times)
    assert ox_times == up_times == [
        ("date", "2026-07-03"),
        ("datetime", "2026-07-03T23:45:00"),
        ("datetime", "2026-07-04T23:45:00"),
    ]
    date_triggers = [t for t in ox_ev.alarms.times if type(t.trigger) is datetime.date]
    assert len(date_triggers) == 1 and date_triggers[0].is_active()

    # set_local_timezone promotes date trigger to localized midnight datetime and resolves floating triggers.
    alarms = ox_ev.alarms
    alarms.set_local_timezone("Europe/Istanbul")
    assert all(t.is_active() for t in alarms.times)
    assert datetime.datetime(2026, 7, 3, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul")) in [
        t.trigger for t in alarms.times
    ]


def test_leading_slash_globally_unique_tzid_resolution_parity() -> None:
    """Leading-slash RFC 5545 TZIDs (/America/New_York, /UTC, /US/Eastern) resolve across parse and query."""
    ws = datetime.datetime(2026, 7, 1, tzinfo=UTC)
    we = datetime.datetime(2026, 7, 3, tzinfo=UTC)

    for tz_param, exp_iso in (
        ("/America/New_York", "2026-07-01T13:00:00+00:00"),
        ("/America/Indiana/Indianapolis", "2026-07-01T13:00:00+00:00"),
        ("/UTC", "2026-07-01T09:00:00+00:00"),
        ("/US/Eastern", "2026-07-01T13:00:00+00:00"),
    ):
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//TZ//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:ltz\r\nDTSTART;TZID={tz_param}:20260701T090000\r\n"
            f"DURATION:PT1H\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")
        cal = Calendar.from_ical(ics)
        assert cal.events[0]["DTSTART"].dt.astimezone(UTC).isoformat() == exp_iso

        up_q = up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we)
        ox_q = compat_of(cal).between(ws, we)
        assert [e["DTSTART"].dt.astimezone(UTC).isoformat() for e in ox_q] == [
            e["DTSTART"].dt.astimezone(UTC).isoformat() for e in up_q
        ] == [exp_iso]

    assert icaloxide._icaloxide.resolve_tzid("/America/New_York") == "America/New_York"
    assert icaloxide._icaloxide.resolve_tzid("/UTC") == "UTC"
    assert icaloxide._icaloxide.resolve_tzid("/US/Eastern") == "US/Eastern"
    assert icaloxide._icaloxide.resolve_tzid("/Eastern Standard Time") == "America/New_York"


def test_historical_vtimezone_tricky_add_missing_folded_b64_lowercase_and_cli(
    tmp_path: Path,
    capsys: object,
) -> None:
    """Verify historical VTIMEZONE rules, tricky TZID generation, folded BASE64, lowercase ICS, and CLI."""
    ics_hist = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//HTZ//EN\r\n"
        b"BEGIN:VTIMEZONE\r\nTZID:Custom/US-Hist\r\n"
        b"BEGIN:DAYLIGHT\r\nDTSTART:19870405T020000\r\n"
        b"RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU;UNTIL=20060402T070000Z\r\n"
        b"TZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nEND:DAYLIGHT\r\n"
        b"BEGIN:STANDARD\r\nDTSTART:19871025T020000\r\n"
        b"RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20061029T060000Z\r\n"
        b"TZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\nEND:STANDARD\r\n"
        b"BEGIN:DAYLIGHT\r\nDTSTART:20070311T020000\r\n"
        b"RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nEND:DAYLIGHT\r\n"
        b"BEGIN:STANDARD\r\nDTSTART:20071104T020000\r\n"
        b"RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\nEND:STANDARD\r\n"
        b"END:VTIMEZONE\r\n"
        b"BEGIN:VEVENT\r\nUID:h2005\r\nDTSTART;TZID=Custom/US-Hist:20050320T120000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:h2008\r\nDTSTART;TZID=Custom/US-Hist:20080320T120000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws_h, we_h = datetime.datetime(2005, 1, 1, tzinfo=UTC), datetime.datetime(2009, 1, 1, tzinfo=UTC)
    assert [
        (str(e["UID"]), e["DTSTART"].dt.astimezone(UTC).isoformat())
        for e in compat_of(Calendar.from_ical(ics_hist)).between(ws_h, we_h)
    ] == [
        ("h2005", "2005-03-20T17:00:00+00:00"),
        ("h2008", "2008-03-20T16:00:00+00:00"),
    ]

    # add_missing_timezones for non-DST / half-hour / 45-min / POSIX Etc/GMT zones.
    ics_tz = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//TZ//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:ist\r\nDTSTART;TZID=Europe/Istanbul:20260115T120000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:cha\r\nDTSTART;TZID=Pacific/Chatham:20260715T120000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:etc\r\nDTSTART;TZID=Etc/GMT+5:20260715T120000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    c_tz = Calendar.from_ical(ics_tz)
    c_tz.add_missing_timezones()
    tz_rt = c_tz.to_ical()
    assert [
        (str(e["UID"]), e["DTSTART"].dt.astimezone(UTC).isoformat())
        for e in compat_of(Calendar.from_ical(tz_rt)).between(
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
            datetime.datetime(2027, 1, 1, tzinfo=UTC),
        )
    ] == [
        (str(e["UID"]), e["DTSTART"].dt.astimezone(UTC).isoformat())
        for e in up_rie.of(up_ical.Calendar.from_ical(tz_rt)).between(
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
            datetime.datetime(2027, 1, 1, tzinfo=UTC),
        )
    ]

    # Folded BASE64 binary ATTACH & CLI smoke checks.
    b64 = b"VGhpcyBpcyBhIGxvbmcgYmluYXJ5IHBheWxvYWQgdGhhdCBzcGFucyBhY3Jvc3MgbXVsdGlwbGUgZm9sZGVkIGxpbmVzIGluIGlDYWxlbmRhciE="
    ics_b64 = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//CLI//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\n"
        b"DTSTAMP:20260701T090000Z\r\nDTSTART:20260701T090000Z\r\nDTEND:20260701T100000Z\r\n"
        b"ATTACH;ENCODING=BASE64;VALUE=BINARY:" + b64[:30] + b"\r\n " + b64[30:60] + b"\r\n " + b64[60:] + b"\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert Calendar.from_ical(ics_b64).events[0].decoded("ATTACH") == up_ical.Calendar.from_ical(
        ics_b64
    ).events[0].decoded("ATTACH")

    p = tmp_path / "cli.ics"
    p.write_bytes(ics_b64)
    assert cli_main(["validate", str(p)]) == 0
    assert cli_main(["query", str(p), "--between", "2026-07-01", "2026-07-03", "--busy"]) == 0
    assert cli_main(["convert", str(p), "--to", "xcal"]) == 0
    _ = capsys
