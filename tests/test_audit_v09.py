"""Comprehensive v0.9.0 audit regressions and extended 80-case differential fuzzer."""

from __future__ import annotations

import datetime
import random
from typing import Any

import icalendar as up_ical
import icaloxide
import recurring_ical_events as up_rie
from icaloxide.compat.icalendar import Calendar, Timezone
from icaloxide.compat.recurring_ical_events import of as compat_of


def _norm_utc_occs(items: list[Any]) -> list[tuple[Any, ...]]:
    out = []
    for c in items:
        s = c.decoded("DTSTART", None)
        e = c.decoded("DUE", None) if c.name == "VTODO" else c.decoded("DTEND", None)
        if e is None:
            e = c.decoded("DTEND", None)
        if isinstance(s, datetime.datetime) and s.tzinfo is not None:
            s_key: Any = ("dt", s.astimezone(datetime.timezone.utc))
        elif isinstance(s, datetime.datetime):
            s_key = ("ndt", s)
        elif isinstance(s, datetime.date):
            s_key = ("d", s)
        else:
            s_key = None
        if isinstance(e, datetime.datetime) and e.tzinfo is not None:
            e_key: Any = ("dt", e.astimezone(datetime.timezone.utc))
        elif isinstance(e, datetime.datetime):
            e_key = ("ndt", e)
        elif isinstance(e, datetime.date):
            e_key = ("d", e)
        else:
            e_key = None
        out.append((c.name, str(c.decoded("UID", "")), str(c.decoded("SUMMARY", "")), s_key, e_key))
    return sorted(out)


def test_weekly_bymonthday_byyearday_and_yearly_bymonthday_parity() -> None:
    """WEEKLY+BYMONTHDAY/BYYEARDAY and YEARLY+BYMONTHDAY match upstream dateutil/rie."""
    cases = [
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\nBEGIN:VEVENT\r\nUID:wmd1\r\n"
            b"DTSTART:20260101T100000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=WEEKLY;BYMONTHDAY=1,15;COUNT=6\r\n"
            b"SUMMARY:WMD1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            (2026, 1, 1),
            (2027, 1, 1),
        ),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\nBEGIN:VEVENT\r\nUID:wmd2\r\n"
            b"DTSTART:20260101T100000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=WEEKLY;INTERVAL=2;BYMONTHDAY=1,15;COUNT=5\r\n"
            b"SUMMARY:WMD2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            (2026, 1, 1),
            (2028, 1, 1),
        ),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\nBEGIN:VEVENT\r\nUID:wyd\r\n"
            b"DTSTART:20260101T100000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=WEEKLY;BYYEARDAY=1,10,20,30;COUNT=4\r\n"
            b"SUMMARY:WYD\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            (2026, 1, 1),
            (2027, 1, 1),
        ),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\nBEGIN:VEVENT\r\nUID:ymdn\r\n"
            b"DTSTART:20260101T100000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=YEARLY;BYMONTHDAY=15;COUNT=5\r\n"
            b"SUMMARY:YMDN\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            (2026, 1, 1),
            (2032, 1, 1),
        ),
    ]
    for ics, s_t, e_t in cases:
        ws = datetime.datetime(*s_t, tzinfo=datetime.timezone.utc)
        we = datetime.datetime(*e_t, tzinfo=datetime.timezone.utc)
        up = _norm_utc_occs(up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we))
        ox_q = compat_of(Calendar.from_ical(ics))
        assert _norm_utc_occs(ox_q.between(ws, we)) == up
        assert _norm_utc_occs(list(ox_q.after(ws))) == up


def test_until_date_on_timed_dtstart_and_naive_until_on_tzid() -> None:
    """UNTIL=DATE on timed DTSTART stops at midnight and naive UNTIL compares UTC wall clock."""
    cases = [
        ("DTSTART;TZID=America/New_York:20260701T100000", "20260705"),
        ("DTSTART;TZID=America/New_York:20260701T100000", "20260705T100000"),
        ("DTSTART;TZID=Asia/Tokyo:20260701T100000", "20260705"),
        ("DTSTART;TZID=Asia/Tokyo:20260701T100000", "20260705T100000"),
        ("DTSTART:20260701T100000Z", "20260705"),
        ("DTSTART:20260701T100000", "20260705"),
    ]
    ws = datetime.datetime(2026, 6, 30, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
    for dts, until in cases:
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//U//EN\r\nBEGIN:VEVENT\r\nUID:u1\r\n"
            f"{dts}\r\nDURATION:PT1H\r\nRRULE:FREQ=DAILY;UNTIL={until}\r\nSUMMARY:U\r\n"
            f"END:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode()
        up = _norm_utc_occs(up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we))
        ox_q = compat_of(Calendar.from_ical(ics))
        assert _norm_utc_occs(ox_q.between(ws, we)) == up
        assert _norm_utc_occs(list(ox_q.after(ws))) == up


def test_duplicate_master_sequence_and_absolute_valarm_dedup() -> None:
    """Duplicate master VEVENT picks highest SEQUENCE; absolute VALARM fires once."""
    dup_seq_first_low = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//S//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:s1\r\nSEQUENCE:1\r\nDTSTART:20260701T100000Z\r\nDURATION:PT1H\r\nSUMMARY:Old\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:s1\r\nSEQUENCE:2\r\nDTSTART:20260701T110000Z\r\nDURATION:PT1H\r\nSUMMARY:New\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    dup_seq_first_high = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//S//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:s2\r\nSEQUENCE:5\r\nDTSTART:20260701T120000Z\r\nDURATION:PT1H\r\nSUMMARY:Winner\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:s2\r\nSEQUENCE:2\r\nDTSTART:20260701T100000Z\r\nDURATION:PT1H\r\nSUMMARY:Stale\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2026, 6, 30, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 5, tzinfo=datetime.timezone.utc)
    for ics in (dup_seq_first_low, dup_seq_first_high):
        up = _norm_utc_occs(up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we))
        ox = _norm_utc_occs(compat_of(Calendar.from_ical(ics)).between(ws, we))
        assert ox == up

    alarm_ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//A//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:al-1\r\nDTSTART:20260710T100000Z\r\nDTEND:20260710T110000Z\r\n"
        b"RRULE:FREQ=DAILY;COUNT=4\r\nSUMMARY:Recurring\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:AbsOnce\r\nTRIGGER;VALUE=DATE-TIME:20260702T080000Z\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:al-can\r\nSTATUS:CANCELLED\r\nDTSTART:20260703T100000Z\r\nDTEND:20260703T110000Z\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:CancelledAbs\r\nTRIGGER;VALUE=DATE-TIME:20260702T090000Z\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    # Parent events are after Jul 3..10, while absolute alarms fire on Jul 2
    a_ws = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    a_we = datetime.datetime(2026, 7, 3, tzinfo=datetime.timezone.utc)
    rust_q = icaloxide.Calendar.from_ical(alarm_ics).query(components=["VALARM"], skip_cancelled=True)
    between_al = rust_q.between(a_ws, a_we)
    after_al = [a for a in rust_q.after(a_ws) if a.start < a_we]
    assert len(between_al) == 1
    assert between_al[0].start == datetime.datetime(2026, 7, 2, 8, 0, tzinfo=datetime.timezone.utc)
    assert len(after_al) == 1
    assert after_al[0].start == datetime.datetime(2026, 7, 2, 8, 0, tzinfo=datetime.timezone.utc)


def test_vjournal_no_dtend_and_undated_vtodo_to_ical() -> None:
    """VJOURNAL occurrences omit DTEND; undated VTODO omits synthetic DTSTART/DUE."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//J//EN\r\n"
        b"BEGIN:VJOURNAL\r\nUID:j1\r\nDTSTART;VALUE=DATE:20260701\r\nRRULE:FREQ=DAILY;COUNT=2\r\nSUMMARY:Note\r\nEND:VJOURNAL\r\n"
        b"BEGIN:VTODO\r\nUID:t1\r\nCOMPLETED:20260702T120000Z\r\nSUMMARY:Done\r\nEND:VTODO\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 5, tzinfo=datetime.timezone.utc)
    cal = Calendar.from_ical(ics)
    journals = compat_of(cal, components=["VJOURNAL"]).between(ws, we)
    assert len(journals) == 2
    for j in journals:
        assert "DTEND" not in j
        assert j.decoded("DTEND", None) is None
        assert j.get("DTEND", None) is None
        raw = j.to_ical()
        assert b"DTEND" not in raw
        assert b"DTSTART" in raw

    todos = compat_of(cal, components=["VTODO"]).between(ws, we)
    assert len(todos) == 1
    assert todos[0].decoded("DTSTART", None) is None
    assert todos[0].decoded("DUE", None) is None
    todo_raw = todos[0].to_ical()
    assert b"DTSTART" not in todo_raw
    assert b"DUE" not in todo_raw


def test_compiled_vtimezone_tzinfo_fold_and_add_missing_timezones_inplace() -> None:
    """Compiled VTIMEZONE tzinfo handles fall-back fold=0/1 independent of OS TZ, and add_missing_timezones mutates in place."""
    vtz = Timezone.from_tzid("America/New_York", datetime.date(2025, 1, 1), datetime.date(2027, 12, 31))
    tz = vtz.to_tz(lookup_tzid=False)

    dt_pre_spring = datetime.datetime(2026, 3, 8, 1, 30, tzinfo=tz)
    dt_post_spring = datetime.datetime(2026, 3, 8, 3, 30, tzinfo=tz)
    assert dt_pre_spring.utcoffset() == datetime.timedelta(hours=-5)
    assert dt_post_spring.utcoffset() == datetime.timedelta(hours=-4)

    dt_fold0 = datetime.datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=tz)
    dt_fold1 = datetime.datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=tz)
    assert dt_fold0.utcoffset() == datetime.timedelta(hours=-4)
    assert dt_fold1.utcoffset() == datetime.timedelta(hours=-5)

    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//TZ//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:tz-in\r\n"
        b"DTSTART;TZID=America/New_York:20260701T100000\r\n"
        b"DTEND;TZID=America/New_York:20260701T110000\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    lazy_cal = Calendar.from_ical(ics)
    ret = lazy_cal.add_missing_timezones()
    assert ret is lazy_cal
    assert len(lazy_cal.timezones) == 1
    assert lazy_cal.is_lazy()

    mat_cal = Calendar.from_ical(ics)
    mat_cal.events[0]["SUMMARY"] = "Materialized"
    assert not mat_cal.is_lazy()
    ret2 = mat_cal.add_missing_timezones()
    assert ret2 is mat_cal
    assert len(mat_cal.timezones) == 1
    assert mat_cal.events[0].summary == "Materialized"


def test_rich_jcal_roundtrip_request_status_and_binary_attach() -> None:
    """Rich jCal serialization round-trips via upstream icalendar.Calendar.from_jcal and Calendar.from_jcal."""
    rich_ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Rich//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:rich-1\r\nDTSTAMP:20260701T000000Z\r\n"
        b"DTSTART;TZID=America/New_York:20260701T093000\r\nDURATION:PT1H30M\r\n"
        b"SUMMARY:Audit Check\r\nGEO:41.0082;28.9784\r\n"
        b"ATTACH;FMTTYPE=application/octet-stream;ENCODING=BASE64;VALUE=BINARY:SGVsbG8h\r\n"
        b"REQUEST-STATUS:2.0;Success;Extra\\;Info\r\n"
        b"RDATE;VALUE=PERIOD:20260702T100000Z/PT1H\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ox_cal = Calendar.from_ical(rich_ics)
    jc = ox_cal.to_jcal()
    up_rt = up_ical.Calendar.from_jcal(jc)
    ox_rt = Calendar.from_jcal(jc)
    assert len(up_rt.events) == 1
    assert len(ox_rt.events) == 1
    assert ox_rt.events[0].decoded("REQUEST-STATUS") == "2.0;Success;Extra;Info"
    assert ox_rt.events[0].decoded("ATTACH") == b"Hello!"
    assert up_rt.events[0].decoded("ATTACH") == b"Hello!"


def test_80_case_extended_subdaily_bysetpos_and_fastforward_fuzzer() -> None:
    """80 extended UTC differential cases across MINUTELY, SECONDLY, BYSETPOS, WEEKLY+BYMONTHDAY, and fast-forward."""
    rng = random.Random(20260900)
    fixed_cases = [
        ("DTSTART:20240229T100000Z", "DURATION:PT1H", "FREQ=YEARLY;COUNT=4", 2024, 2040),
        ("DTSTART:20260131T100000Z", "DURATION:PT1H", "FREQ=MONTHLY;COUNT=8", 2026, 2028),
        ("DTSTART:20260101T100000Z", "DURATION:PT1H", "FREQ=YEARLY;BYMONTH=1,6,12;BYDAY=MO,FR;BYSETPOS=1,-1;COUNT=8", 2026, 2032),
        ("DTSTART:20260101T080000Z", "DURATION:PT10M", "FREQ=MINUTELY;INTERVAL=15;BYHOUR=8,9;BYMINUTE=0,30,45;BYSETPOS=1;COUNT=8", 2026, 2026),
        ("DTSTART:20260101T080000Z", "DURATION:PT10S", "FREQ=SECONDLY;INTERVAL=30;BYMINUTE=0,1;BYSECOND=0,30;COUNT=8", 2026, 2026),
        ("DTSTART:19700105T100000Z", "DURATION:PT1H", "FREQ=YEARLY;INTERVAL=3;BYWEEKNO=1,-1;BYDAY=MO,SU", 2025, 2030),
        ("DTSTART:19900101T100000Z", "DURATION:PT1H", "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;UNTIL=20260831T235959Z", 2026, 2027),
        ("DTSTART;TZID=Pacific/Kiritimati:20000101T003000", "DURATION:PT1H", "FREQ=WEEKLY;INTERVAL=2;WKST=SU;BYDAY=SU,MO", 2026, 2026),
        ("DTSTART;TZID=Pacific/Niue:20000101T233000", "DURATION:PT1H", "FREQ=DAILY;INTERVAL=3;BYDAY=MO,FR", 2026, 2026),
    ]
    all_specs: list[tuple[str, str, str, int, int]] = list(fixed_cases)
    freqs = ["MINUTELY", "SECONDLY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
    days = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

    while len(all_specs) < 80:
        f = rng.choice(freqs)
        yr = rng.choice([2024, 2025, 2026])
        mo = rng.randint(1, 12)
        dy = rng.randint(1, 28)
        hr = rng.randint(0, 23)
        mn = rng.choice([0, 15, 30])
        dts = f"DTSTART:{yr:04d}{mo:02d}{dy:02d}T{hr:02d}{mn:02d}00Z"
        parts = [f"FREQ={f}"]
        if f == "MINUTELY":
            parts.append("INTERVAL=" + rng.choice(["10", "15", "30"]))
            parts.append("BYHOUR=8,9,10")
            dur = "DURATION:PT5M"
        elif f == "SECONDLY":
            parts.append("INTERVAL=" + rng.choice(["20", "30", "45"]))
            parts.append("BYSECOND=0,30")
            dur = "DURATION:PT5S"
        elif f == "HOURLY":
            parts.append("BYMINUTE=0,20,40")
            parts.append("BYSETPOS=" + rng.choice(["1", "-1"]))
            dur = "DURATION:PT10M"
        elif f == "DAILY":
            parts.append("BYHOUR=9,17")
            parts.append("BYMINUTE=0,30")
            parts.append("BYSETPOS=" + rng.choice(["1", "-1"]))
            dur = "DURATION:PT15M"
        elif f == "WEEKLY":
            if rng.random() < 0.5:
                parts.append("BYMONTHDAY=1,15,-1")
            else:
                parts.append("BYDAY=" + ",".join(rng.sample(days, 3)))
                parts.append("BYSETPOS=1,-1")
            parts.append("WKST=" + rng.choice(["MO", "SU"]))
            dur = "DURATION:PT1H"
        elif f == "MONTHLY":
            parts.append("BYMONTHDAY=1,13,15,-1")
            if rng.random() < 0.5:
                parts.append("BYDAY=FR,MO")
            dur = "DURATION:PT1H"
        else:
            parts.append("BYMONTHDAY=1,15,-1")
            if rng.random() < 0.5:
                parts.append("BYMONTH=1,2,12")
            dur = "DURATION:PT1H"
        parts.append(f"COUNT={rng.randint(3, 8)}")
        all_specs.append((dts, dur, ";".join(parts), yr, yr + 4))

    for idx, (dts, dur, rrule, sy, ey) in enumerate(all_specs):
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//F2//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:ext-{idx}\r\n{dts}\r\n{dur}\r\nRRULE:{rrule}\r\nSUMMARY:Ext {idx}\r\n"
            f"END:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")
        ws = datetime.datetime(sy, 1, 1, tzinfo=datetime.timezone.utc)
        we = datetime.datetime(ey, 12, 31, 23, 59, tzinfo=datetime.timezone.utc)
        up = _norm_utc_occs(up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we))
        ox = _norm_utc_occs(compat_of(Calendar.from_ical(ics)).between(ws, we))
        assert ox == up, f"Extended fuzzer mismatch idx={idx}: {dts} {rrule}"
