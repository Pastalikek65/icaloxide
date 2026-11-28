"""v0.9.3 Round-4 deep-audit regression tests and 250-case combinatorial RRULE fuzzer."""

from __future__ import annotations

import datetime
import random

import icalendar as up_ical
import icaloxide
import pytest
import recurring_ical_events as up_rie
from icaloxide.compat.icalendar import Calendar, JCalParsingError, Parameters, vText
from icaloxide.compat.recurring_ical_events import of as compat_of
from recurring_ical_events.errors import BadRuleStringFormat


def _norm_utc_occs(events: list[object]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ev in events:
        dt_s = ev["DTSTART"].dt  # type: ignore[index]
        dt_e = ev["DTEND"].dt if "DTEND" in ev else dt_s  # type: ignore[index, operator]
        s_iso = (
            dt_s.astimezone(datetime.timezone.utc).isoformat()
            if isinstance(dt_s, datetime.datetime) and dt_s.tzinfo is not None
            else dt_s.isoformat()
        )
        e_iso = (
            dt_e.astimezone(datetime.timezone.utc).isoformat()
            if isinstance(dt_e, datetime.datetime) and dt_e.tzinfo is not None
            else dt_e.isoformat()
        )
        out.append((s_iso, e_iso))
    out.sort()
    return out


def test_weekly_wkst_bysetpos_count_parity() -> None:
    """WEEKLY + WKST + BYSETPOS + COUNT excludes pre-DTSTART week-0 days to match dateutil."""
    cases = [
        ("20250401T080000Z", "FREQ=WEEKLY;WKST=SA;BYDAY=TH,SA,FR,TU;BYSETPOS=1;COUNT=3"),
        ("20250401T080000Z", "FREQ=WEEKLY;WKST=SA;BYDAY=TH,SA,FR,TU;BYSETPOS=2;COUNT=3"),
        ("20250401T080000Z", "FREQ=WEEKLY;WKST=MO;BYDAY=MO,WE,FR;BYSETPOS=1,-1;COUNT=5"),
        ("20250401T080000Z", "FREQ=WEEKLY;INTERVAL=2;WKST=SA;BYDAY=SA,TU;BYSETPOS=1;COUNT=4"),
    ]
    ws = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    mid = datetime.datetime(2025, 4, 6, tzinfo=datetime.timezone.utc)

    for dtstart, rrule in cases:
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:wbp\r\nDTSTART:{dtstart}\r\nDURATION:PT1H\r\n"
            f"RRULE:{rrule}\r\nSUMMARY:WBP\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")
        up_q = up_rie.of(up_ical.Calendar.from_ical(ics))
        ox_q = compat_of(Calendar.from_ical(ics))
        assert _norm_utc_occs(ox_q.between(ws, we)) == _norm_utc_occs(up_q.between(ws, we)), rrule
        assert _norm_utc_occs(ox_q.after(mid)) == _norm_utc_occs(up_q.after(mid)), rrule


def test_modularly_impossible_rrules_match_or_terminate_cleanly() -> None:
    """Modularly unsatisfiable MONTHLY/DAILY/subdaily RRULEs yield [DTSTART] without DoS budget errors."""
    ws = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)

    # MONTHLY & DAILY modular impossibles: both upstream and icaloxide yield [DTSTART].
    parity_cases = [
        ("20250701T231500Z", "FREQ=MONTHLY;INTERVAL=3;BYMONTH=2,8,11;COUNT=3"),
        ("20250401T080000Z", "FREQ=DAILY;INTERVAL=7;BYDAY=MO;COUNT=5"),
    ]
    for dtstart, rrule in parity_cases:
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:imp-p\r\nDTSTART:{dtstart}\r\nDURATION:PT30M\r\n"
            f"RRULE:{rrule}\r\nSUMMARY:Imp\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")
        up_q = up_rie.of(up_ical.Calendar.from_ical(ics))
        ox_q = compat_of(Calendar.from_ical(ics))
        assert _norm_utc_occs(ox_q.between(ws, we)) == _norm_utc_occs(up_q.between(ws, we)), rrule
        assert _norm_utc_occs(ox_q.after(ws)) == _norm_utc_occs(up_q.after(ws)), rrule

    # Sub-daily modular impossibles: dateutil rejects empty byxxx; icaloxide returns [DTSTART] cleanly.
    subdaily_cases = [
        ("20250701T231500Z", "FREQ=HOURLY;INTERVAL=2;BYHOUR=0,6,12,18;COUNT=5"),
        ("20250701T100000Z", "FREQ=MINUTELY;INTERVAL=15;BYMINUTE=5,20;COUNT=5"),
        ("20250701T100000Z", "FREQ=SECONDLY;INTERVAL=10;BYSECOND=3,7;COUNT=5"),
    ]
    for dtstart, rrule in subdaily_cases:
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:imp-s\r\nDTSTART:{dtstart}\r\nDURATION:PT30M\r\n"
            f"RRULE:{rrule}\r\nSUMMARY:ImpS\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")
        ox_q = compat_of(Calendar.from_ical(ics))
        occs = _norm_utc_occs(ox_q.between(ws, we))
        assert len(occs) == 1, rrule
        assert len(_norm_utc_occs(ox_q.after(ws))) == 1, rrule

    # Calendar-impossible Feb 30 continues to trip the DoS safety budget.
    feb30_ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:feb30\r\nDTSTART:20260101T000000Z\r\nDURATION:PT1H\r\n"
        b"RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(feb30_ics)
    with pytest.raises(ValueError, match="safety budget"):
        cal.query().between(ws, we)


def test_from_jcal_accepts_bytes_and_bytearray() -> None:
    """Calendar.from_jcal accepts UTF-8 bytes and bytearray, rejecting malformed bytes cleanly."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//J//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:jc1\r\nDTSTART:20260701T100000Z\r\nSUMMARY:ByteJCal\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    raw_bytes = cal.to_json().encode("utf-8")

    cal_b = Calendar.from_jcal(raw_bytes)
    assert isinstance(cal_b, Calendar)
    assert cal_b.events[0].decoded("SUMMARY") == "ByteJCal"

    cal_ba = Calendar.from_jcal(bytearray(raw_bytes))
    assert cal_ba.events[0].decoded("SUMMARY") == "ByteJCal"

    with pytest.raises(JCalParsingError):
        Calendar.from_jcal(b"\xff\xfe{bad")
    with pytest.raises(JCalParsingError):
        Calendar.from_jcal(b"{not-json")


def test_parameters_from_ical_quotes_multivalue_and_rfc6868() -> None:
    """Parameters.from_ical handles quoted semicolons, comma multi-values, and single-pass RFC 6868."""
    raw = 'CN="Smith; John";ALTREP="http://ex.com/?a=1;b=2";X-TEST="O^\'Brien^n2^^3"'
    parsed = Parameters.from_ical(raw)
    assert parsed["CN"] == "Smith; John"
    assert parsed["ALTREP"] == "http://ex.com/?a=1;b=2"
    assert parsed["X-TEST"] == 'O"Brien\n2^3'

    multi = Parameters.from_ical(
        'MEMBER="mailto:a@b.com","mailto:c@d.com";X-LIST=one,two,three;X-EMPTY=""'
    )
    assert multi["MEMBER"] == ["mailto:a@b.com", "mailto:c@d.com"]
    assert multi["X-LIST"] == ["one", "two", "three"]
    assert multi["X-EMPTY"] == ""


def test_vtext_from_ical_single_pass_unescape() -> None:
    """vText.from_ical avoids double-unescaping escaped backslashes before n/N/comma/semi."""
    assert vText.from_ical(r"Hello\\nWorld\,Test\;Semi") == r"Hello\nWorld,Test;Semi"
    assert vText.from_ical(b"Hi\\, team\\; go\\nnow") == "Hi, team; go\nnow"
    assert vText.from_ical(r"A\\\\B\,C") == r"A\\B,C"


def test_250_case_combinatorial_multi_modifier_fuzzer() -> None:
    """250 combinatorial multi-modifier RRULE cases match upstream between() and after()."""
    rng = random.Random(404040)
    days = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    ws = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2028, 12, 31, tzinfo=datetime.timezone.utc)
    mid = datetime.datetime(2025, 8, 1, 12, 0, tzinfo=datetime.timezone.utc)

    for idx in range(250):
        mo = rng.randint(1, 12)
        dy = rng.choice([1, 15, 28, 29, 30, 31])
        if mo == 2 and dy > 28:
            dy = 28
        elif mo in (4, 6, 9, 11) and dy == 31:
            dy = 30
        hr = rng.choice([0, 8, 14, 23])
        mn = rng.choice([0, 15, 45])
        dts = f"DTSTART:2025{mo:02d}{dy:02d}T{hr:02d}{mn:02d}00Z"

        freq = rng.choice(["YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY"])
        parts = [f"FREQ={freq}"]
        if rng.random() < 0.4:
            parts.append(f"INTERVAL={rng.choice([2, 3])}")
        parts.append("WKST=" + rng.choice(["MO", "SU", "SA"]))

        if freq == "YEARLY":
            sub = rng.choice(["wk", "yd", "md"])
            if sub == "wk":
                parts.append("BYWEEKNO=" + ",".join(str(x) for x in rng.sample([1, 10, 26, -1], 2)))
                if rng.random() < 0.6:
                    parts.append("BYDAY=" + ",".join(rng.sample(days, 3)))
            elif sub == "yd":
                parts.append("BYYEARDAY=" + ",".join(str(x) for x in rng.sample([1, 60, 180, -1], 2)))
                if rng.random() < 0.4:
                    parts.append("BYDAY=" + ",".join(rng.sample(days, 4)))
            else:
                parts.append("BYMONTH=" + ",".join(str(x) for x in rng.sample([1, 2, 6, 12], 2)))
                if rng.random() < 0.5:
                    parts.append("BYMONTHDAY=" + ",".join(str(x) for x in rng.sample([1, 15, -1], 2)))
                if rng.random() < 0.5:
                    parts.append("BYDAY=" + ",".join(rng.sample(days, 2)))
            dur = "DURATION:PT1H"
        elif freq == "MONTHLY":
            if rng.random() < 0.35:
                parts.append("BYMONTH=" + ",".join(str(x) for x in rng.sample([2, 5, 8, 11], 3)))
            if rng.random() < 0.5:
                parts.append("BYMONTHDAY=" + ",".join(str(x) for x in rng.sample([1, 13, 15, 31, -1], 2)))
            else:
                # Keep ordinal uniform per rule (dateutil drops candidates when mixing ordinal + bare weekdays).
                ord_pref = rng.choice(["", "1", "-1"])
                parts.append("BYDAY=" + ",".join(ord_pref + d for d in rng.sample(days, 2)))
            dur = "DURATION:PT45M"
        elif freq == "WEEKLY":
            if rng.random() < 0.5:
                parts.append("BYDAY=" + ",".join(rng.sample(days, rng.randint(2, 4))))
            if rng.random() < 0.35:
                parts.append("BYMONTH=" + ",".join(str(x) for x in rng.sample([1, 4, 7, 10], 3)))
            if rng.random() < 0.35:
                parts.append("BYMONTHDAY=" + ",".join(str(x) for x in rng.sample([1, 10, 15, -1], 2)))
            dur = "DURATION:PT30M"
        elif freq == "DAILY":
            if rng.random() < 0.5:
                parts.append("BYDAY=" + ",".join(rng.sample(days, 3)))
            if rng.random() < 0.4:
                parts.append("BYHOUR=9,17")
            if rng.random() < 0.4:
                parts.append("BYMINUTE=0,30")
            dur = "DURATION:PT15M"
        else:
            parts.append("BYHOUR=" + ",".join(str(x) for x in rng.sample([0, 6, 12, 18], 3)))
            parts.append("BYMINUTE=0,30")
            dur = "DURATION:PT10M"

        if "BYDAY" in ";".join(parts) and rng.random() < 0.4:
            parts.append("BYSETPOS=" + rng.choice(["1", "-1", "1,-1"]))
        if rng.random() < 0.65:
            parts.append(f"COUNT={rng.choice([3, 5, 8])}")
        else:
            parts.append("UNTIL=20270615T120000Z")

        rrule = ";".join(parts)
        ics = (
            f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//F4//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:f4-{idx}\r\n{dts}\r\n{dur}\r\n"
            f"RRULE:{rrule}\r\nSUMMARY:F4 {idx}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode("utf-8")

        ox_q = compat_of(Calendar.from_ical(ics))
        try:
            up_q = up_rie.of(up_ical.Calendar.from_ical(ics))
        except BadRuleStringFormat:
            # Modularly impossible sub-daily rule raises ValueError in dateutil; icaloxide returns [DTSTART].
            assert len(ox_q.between(ws, we)) == 1, f"expected [DTSTART] idx={idx}: {dts} {rrule}"
            continue

        assert _norm_utc_occs(ox_q.between(ws, we)) == _norm_utc_occs(up_q.between(ws, we)), (
            f"between mismatch idx={idx}: {dts} {rrule}"
        )
        assert _norm_utc_occs(ox_q.after(mid)) == _norm_utc_occs(up_q.after(mid)), (
            f"after mismatch idx={idx}: {dts} {rrule}"
        )
