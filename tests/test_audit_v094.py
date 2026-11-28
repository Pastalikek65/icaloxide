"""v0.9.4 Round-5 deep-audit regression tests and multi-timezone DST differential fuzzer."""

from __future__ import annotations

import datetime
import random

import icalendar as up_ical
import icaloxide
import recurring_ical_events as up_rie
from icaloxide.compat.icalendar import Calendar, Event
from icaloxide.compat.recurring_ical_events import of as compat_of

UTC = datetime.timezone.utc


def _norm_occs(evts: list[object]) -> list[tuple[str, str, str, str | None, str | None]]:
    out: list[tuple[str, str, str, str | None, str | None]] = []
    for e in evts:
        s = e["DTSTART"].dt if "DTSTART" in e else (e["DUE"].dt if "DUE" in e else None)  # type: ignore[index, operator]
        en = e["DTEND"].dt if "DTEND" in e else (e["DUE"].dt if "DUE" in e else s)  # type: ignore[index, operator]

        def _fmt(x: object) -> str | None:
            if x is None:
                return None
            if isinstance(x, datetime.datetime) and x.tzinfo is not None:
                return x.astimezone(UTC).isoformat()
            return x.isoformat()  # type: ignore[attr-defined]

        out.append((e.name, str(e.get("UID", "")), str(e.get("SUMMARY", "")), _fmt(s), _fmt(en)))  # type: ignore[attr-defined]
    out.sort(key=lambda r: (r[3] or "", r[4] or "", r[1], r[2]))
    return out


def test_non_recurring_exdate_equals_dtstart_parity() -> None:
    """Non-recurring VEVENTs with matching EXDATE are excluded across all query APIs and VALARM."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:ex-utc\r\nDTSTART:20260701T090000Z\r\nDURATION:PT1H\r\n"
        b"EXDATE:20260701T090000Z\r\nSUMMARY:ExUTC\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:ShouldNotFire\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:ex-tz\r\nDTSTART;TZID=America/New_York:20260701T090000\r\nDURATION:PT1H\r\n"
        b"EXDATE;TZID=Europe/Berlin:20260701T150000\r\nSUMMARY:ExCrossTZ\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:ex-date\r\nDTSTART;VALUE=DATE:20260704\r\nDURATION:P1D\r\n"
        b"EXDATE;VALUE=DATE:20260704\r\nSUMMARY:ExDate\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:kept\r\nDTSTART:20260705T090000Z\r\nDURATION:PT1H\r\n"
        b"EXDATE:20260706T090000Z\r\nSUMMARY:Kept\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2026, 6, 25, tzinfo=UTC)
    we = datetime.datetime(2026, 7, 15, tzinfo=UTC)

    uq = up_rie.of(up_ical.Calendar.from_ical(ics))
    oq = compat_of(Calendar.from_ical(ics))

    assert _norm_occs(oq.between(ws, we)) == _norm_occs(uq.between(ws, we))
    assert [str(e["UID"]) for e in oq.between(ws, we)] == ["kept"]
    assert [str(e["UID"]) for e in oq.at(datetime.date(2026, 7, 1))] == []
    assert [str(e["UID"]) for e in oq.at(datetime.date(2026, 7, 4))] == []
    assert [str(e["UID"]) for e in oq.after(ws)] == ["kept"]
    assert compat_of(Calendar.from_ical(ics), components=["VALARM"]).between(ws, we) == []
    assert icaloxide.Calendar.from_ical(ics).query().count(ws, we) == 1


def test_modify_occurrence_updates_existing_override_in_place() -> None:
    """Calling modify_occurrence twice for the same RECURRENCE-ID updates in place without duplicating."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//M//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:m1\r\nDTSTART:20260701T090000Z\r\nDTEND:20260701T100000Z\r\n"
        b"RRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Base\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    rid = datetime.datetime(2026, 7, 2, 9, 0, tzinfo=UTC)

    ov1 = cal.modify_occurrence("m1", rid, summary="Edit1", location="RoomA")
    assert ov1.decoded("SEQUENCE") == 1
    assert len([e for e in cal.events if "RECURRENCE-ID" in e]) == 1

    ov2 = cal.modify_occurrence(
        "m1",
        rid,
        start=datetime.datetime(2026, 7, 2, 11, 0, tzinfo=UTC),
        summary="Edit2",
    )
    assert ov2 is ov1
    assert len([e for e in cal.events if "RECURRENCE-ID" in e]) == 1
    assert ov2.decoded("SEQUENCE") == 2
    assert ov2.decoded("LOCATION") == "RoomA"
    assert ov2.decoded("DTSTART") == datetime.datetime(2026, 7, 2, 11, 0, tzinfo=UTC)
    assert ov2.decoded("DTEND") == datetime.datetime(2026, 7, 2, 12, 0, tzinfo=UTC)

    # Also keep already-shifted start if third call only changes another field.
    ov3 = cal.modify_occurrence("m1", rid, summary="Edit3")
    assert ov3.decoded("SEQUENCE") == 3
    assert ov3.decoded("DTSTART") == datetime.datetime(2026, 7, 2, 11, 0, tzinfo=UTC)
    assert ov3.decoded("LOCATION") == "RoomA"


def test_single_item_exdate_rdate_jcal_and_xcal_roundtrip_parity() -> None:
    """Single-element EXDATE and RDATE preserve list shape across jCal and xCal roundtrips."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//JX//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:jx1\r\nDTSTART:20260701T100000Z\r\nDURATION:PT1H\r\n"
        b"EXDATE;TZID=America/New_York:20260702T090000\r\n"
        b"EXDATE;VALUE=DATE:20260704\r\n"
        b"RDATE;VALUE=PERIOD:20260705T100000Z/PT1H\r\n"
        b"RDATE;VALUE=DATE:20260707\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VFREEBUSY\r\nUID:fb1\r\nDTSTART:20260701T000000Z\r\nDTEND:20260702T000000Z\r\n"
        b"FREEBUSY:20260701T090000Z/PT1H\r\n"
        b"END:VFREEBUSY\r\n"
        b"END:VCALENDAR\r\n"
    )
    orig = Calendar.from_ical(ics)
    from_jcal = Calendar.from_jcal(orig.to_json())
    from_xcal = Calendar.from_xcal(orig.to_xcal())

    for rt in (from_jcal, from_xcal):
        assert rt.events[0].decoded("EXDATE") == orig.events[0].decoded("EXDATE")
        assert rt.events[0].decoded("RDATE") == orig.events[0].decoded("RDATE")
        assert rt.freebusy[0].decoded("FREEBUSY") == orig.freebusy[0].decoded("FREEBUSY")


def test_calendar_new_language_uppercase_prodid_without_bogus_property() -> None:
    """Calendar.new uppercases language inside generated PRODID and avoids non-RFC LANGUAGE property."""
    up_cal = up_ical.Calendar.new(organization="Acme", name="Ops", language="tr")
    ox_cal = Calendar.new(organization="Acme", name="Ops", language="tr")

    assert ox_cal.get("PRODID") == up_cal.get("PRODID") == "-//Acme//Ops//TR"
    assert "LANGUAGE" not in ox_cal
    assert "LANGUAGE" not in up_cal


def test_150_case_multi_tz_dst_exdate_rdate_override_fuzzer() -> None:
    """150 randomized multi-TZ (incl. Lord_Howe 30m, Chatham +12:45) DST/EXDATE/RDATE/Override cases."""
    rng = random.Random(505050)
    tzs = [
        "UTC",
        "America/New_York",
        "Europe/Berlin",
        "Asia/Tokyo",
        "Australia/Lord_Howe",
        "Pacific/Chatham",
        "DATE",
    ]
    ws = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    we = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

    for idx in range(150):
        kind = rng.choice(["VEVENT", "VEVENT", "VEVENT", "VTODO", "VJOURNAL"])
        tz = rng.choice(tzs)
        mo = rng.choice([2, 3, 4, 9, 10, 11])
        dy = rng.randint(1, 25)
        hr = rng.choice([0, 1, 2, 3, 9, 15, 23])
        if tz in ("America/New_York", "Europe/Berlin", "Australia/Lord_Howe", "Pacific/Chatham") and hr in (1, 2, 3):
            hr = 9
        mn = rng.choice([0, 30])

        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//F5//EN", f"BEGIN:{kind}", f"UID:f5-{idx}"]
        if tz == "DATE":
            lines.append(f"DTSTART;VALUE=DATE:2026{mo:02d}{dy:02d}")
            if kind != "VJOURNAL":
                lines.append("DURATION:P1D")
        elif tz == "UTC":
            lines.append(f"DTSTART:2026{mo:02d}{dy:02d}T{hr:02d}{mn:02d}00Z")
            if kind != "VJOURNAL":
                lines.append("DURATION:PT1H")
        else:
            lines.append(f"DTSTART;TZID={tz}:2026{mo:02d}{dy:02d}T{hr:02d}{mn:02d}00")
            if kind != "VJOURNAL":
                lines.append("DURATION:PT1H")

        freq = rng.choice(["DAILY", "WEEKLY", "MONTHLY"])
        iv = rng.choice([1, 2])
        cnt = rng.randint(4, 12)
        lines.append(f"RRULE:FREQ={freq};INTERVAL={iv};COUNT={cnt}")
        lines.append(f"SUMMARY:Base {idx}")

        if freq == "DAILY" and iv == 1 and rng.random() < 0.6:
            ex_d = datetime.date(2026, mo, dy) + datetime.timedelta(days=2)
            if tz == "DATE":
                lines.append(f"EXDATE;VALUE=DATE:{ex_d:%Y%m%d}")
            elif tz == "UTC":
                lines.append(f"EXDATE:{ex_d:%Y%m%d}T{hr:02d}{mn:02d}00Z")
            else:
                lines.append(f"EXDATE;TZID={tz}:{ex_d:%Y%m%d}T{hr:02d}{mn:02d}00")

        if rng.random() < 0.5:
            rd_d = datetime.date(2026, mo, dy) + datetime.timedelta(days=20)
            if tz == "DATE":
                lines.append(f"RDATE;VALUE=DATE:{rd_d:%Y%m%d}")
            elif tz == "UTC":
                lines.append(f"RDATE:{rd_d:%Y%m%d}T{hr:02d}{mn:02d}00Z")
            else:
                lines.append(f"RDATE;TZID={tz}:{rd_d:%Y%m%d}T{hr:02d}{mn:02d}00")

        lines.append(f"END:{kind}")

        if freq == "DAILY" and iv == 1 and rng.random() < 0.6:
            ov_d = datetime.date(2026, mo, dy) + datetime.timedelta(days=3)
            mv_d = ov_d + datetime.timedelta(days=1)
            lines.extend([f"BEGIN:{kind}", f"UID:f5-{idx}"])
            if tz == "DATE":
                lines.append(f"RECURRENCE-ID;VALUE=DATE:{ov_d:%Y%m%d}")
                lines.append(f"DTSTART;VALUE=DATE:{mv_d:%Y%m%d}")
                if kind != "VJOURNAL":
                    lines.append("DURATION:P2D")
            elif tz == "UTC":
                lines.append(f"RECURRENCE-ID:{ov_d:%Y%m%d}T{hr:02d}{mn:02d}00Z")
                lines.append(f"DTSTART:{mv_d:%Y%m%d}T{(hr + 2) % 24:02d}{mn:02d}00Z")
                if kind != "VJOURNAL":
                    lines.append("DURATION:PT2H")
            else:
                lines.append(f"RECURRENCE-ID;TZID={tz}:{ov_d:%Y%m%d}T{hr:02d}{mn:02d}00")
                lines.append(f"DTSTART;TZID={tz}:{mv_d:%Y%m%d}T{(hr + 2) % 24:02d}{mn:02d}00")
                if kind != "VJOURNAL":
                    lines.append("DURATION:PT2H")
            lines.append(f"SUMMARY:Moved {idx}")
            lines.append(f"END:{kind}")

        lines.append("END:VCALENDAR")
        ics = ("\r\n".join(lines) + "\r\n").encode("utf-8")
        comps = [kind]
        assert _norm_occs(compat_of(Calendar.from_ical(ics), components=comps).between(ws, we)) == _norm_occs(
            up_rie.of(up_ical.Calendar.from_ical(ics), components=comps).between(ws, we)
        ), f"fuzz idx={idx} kind={kind} tz={tz}"


def test_double_thisandfuture_zero_dur_paginate_ties_and_rfc6868_roundtrip() -> None:
    """Verify chained THISANDFUTURE splits, PT0S window edges, paginate tie-break, and RFC 6868 RT."""
    ics_taf = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:taf2\r\nDTSTART:20260101T100000Z\r\nDURATION:PT1H\r\n"
        b"RRULE:FREQ=DAILY;COUNT=10\r\nSUMMARY:V1\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:taf2\r\nRECURRENCE-ID;RANGE=THISANDFUTURE:20260104T100000Z\r\n"
        b"DTSTART:20260104T080000Z\r\nDURATION:PT1H\r\nSUMMARY:V2\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:taf2\r\nRECURRENCE-ID;RANGE=THISANDFUTURE:20260107T100000Z\r\n"
        b"DTSTART:20260107T140000Z\r\nDURATION:PT2H\r\nSUMMARY:V3\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:taf2\r\nRECURRENCE-ID:20260108T100000Z\r\n"
        b"DTSTART:20260108T180000Z\r\nDURATION:PT30M\r\nSUMMARY:SingleAfterV3\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ws, we = datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 12, 31, tzinfo=UTC)
    assert _norm_occs(compat_of(Calendar.from_ical(ics_taf)).between(ws, we)) == _norm_occs(
        up_rie.of(up_ical.Calendar.from_ical(ics_taf)).between(ws, we)
    )

    # Identical timestamp cursor pagination matches between() without dropping or duplicating items.
    tie_lines = ["BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//P//EN\r\n"]
    for i in range(10):
        tie_lines.append(
            f"BEGIN:VEVENT\r\nUID:tie-{i:02d}\r\nDTSTART:20260101T100000Z\r\n"
            f"DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
        )
    tie_lines.append("END:VCALENDAR\r\n")
    q = icaloxide.Calendar.from_ical("".join(tie_lines)).query()
    p_start, p_end = datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 5, tzinfo=UTC)
    paged = [(o.uid, o.start.isoformat()) for page in q.paginate(3, earliest_end=p_start, latest_start=p_end) for o in page]
    direct = [(o.uid, o.start.isoformat()) for o in q.between(p_start, p_end)]
    assert paged == direct and len(paged) == 20

    # RFC 6868 write/read roundtrip via Python materialized serializer AND Rust lazy serializer.
    c = Calendar.new()
    ev = Event()
    ev.add("UID", "p6868")
    ev.add("DTSTART", datetime.datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    ev.add("SUMMARY", 'Hello, "World";\nLine 2\\End', parameters={"CN": 'O"Brien\nLine2^3;Semi:Colon'})
    ev.add(
        "ATTENDEE",
        "mailto:bob@example.com",
        parameters={"CN": 'Bob "The Boss" ^1\nHQ', "MEMBER": ["mailto:g1@ex.com", "mailto:g2@ex.com"]},
    )
    c.add_component(ev)
    py_ics = c.to_ical()
    rust_rt = Calendar.from_ical(Calendar.from_ical(py_ics).to_ical())
    assert rust_rt.events[0].decoded("SUMMARY") == 'Hello, "World";\nLine 2\\End'
    assert rust_rt.events[0]["SUMMARY"].params["CN"] == 'O"Brien\nLine2^3;Semi:Colon'
    assert rust_rt.events[0]["ATTENDEE"].params["MEMBER"] == ["mailto:g1@ex.com", "mailto:g2@ex.com"]
