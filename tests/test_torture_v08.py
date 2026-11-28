"""Relentless v0.8.0 torture & differential fuzzing suite against icalendar + recurring-ical-events."""

from __future__ import annotations

import copy
import datetime
import pickle
import random
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

import icalendar as up_ical
import icaloxide
import recurring_ical_events as up_rie
from icaloxide.compat.icalendar import Calendar, Event
from icaloxide.compat.recurring_ical_events import of as compat_of


def _norm_dt(v: Any) -> Any:
    if isinstance(v, datetime.datetime):
        if v.tzinfo is not None:
            off = v.utcoffset()
            return (
                "dt",
                v.astimezone(datetime.timezone.utc),
                int(off.total_seconds()) if off is not None else 0,
                v.fold,
            )
        return ("naive", v.year, v.month, v.day, v.hour, v.minute, v.second)
    if isinstance(v, datetime.date):
        return ("date", v.year, v.month, v.day)
    return ("other", repr(v))


def _extract_occ_set(items: list[Any]) -> list[tuple[Any, ...]]:
    out = []
    for c in items:
        s = c.decoded("DTSTART", None)
        e = c.decoded("DUE", None) if c.name == "VTODO" else c.decoded("DTEND", None)
        if e is None:
            e = c.decoded("DTEND", None)
        out.append(
            (
                c.name,
                str(c.decoded("UID", "")),
                str(c.decoded("SUMMARY", "")),
                _norm_dt(s),
                _norm_dt(e),
            )
        )
    return sorted(out)


def test_150_case_randomized_rrule_differential_fuzzer() -> None:
    """150 randomized RRULE+DST+EXDATE+override series match upstream recurring-ical-events."""
    rng = random.Random(20261128)
    tzs = [
        "UTC",
        "America/New_York",
        "Europe/Berlin",
        "Asia/Tokyo",
        "Australia/Sydney",
        "Australia/Lord_Howe",
        "Asia/Kathmandu",
        "DATE",
    ]
    days = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

    for idx in range(150):
        tz = rng.choice(tzs)
        year = rng.choice([2024, 2025, 2026])
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        hour = rng.choice([0, 1, 2, 9, 14, 23])
        minute = rng.choice([0, 30])
        freq = rng.choice(["DAILY", "WEEKLY", "MONTHLY", "YEARLY", "HOURLY"])
        if tz == "DATE" and freq == "HOURLY":
            freq = "DAILY"

        parts = [f"FREQ={freq}"]
        interval = rng.choice([1, 1, 2, 3])
        if interval > 1:
            parts.append(f"INTERVAL={interval}")

        mod_kind = rng.randint(0, 6)
        if freq == "WEEKLY" and mod_kind in (0, 1, 2, 3):
            ds = rng.sample(days, rng.randint(1, 4))
            parts.append("BYDAY=" + ",".join(ds))
            if rng.random() < 0.5:
                parts.append("WKST=" + rng.choice(["MO", "SU", "WE", "SA"]))
        elif freq == "MONTHLY":
            if mod_kind in (0, 1):
                parts.append("BYDAY=" + rng.choice(["1MO", "-1FR", "2TU,-1TH", "MO,TU,WE,TH,FR"]))
                if "MO,TU" in parts[-1]:
                    parts.append("BYSETPOS=" + rng.choice(["1", "-1", "1,-1"]))
            elif mod_kind in (2, 3):
                parts.append("BYMONTHDAY=" + rng.choice(["-1", "1,15,-1", "29,30,31", "-2,-1"]))
                if rng.random() < 0.5:
                    parts.append("BYSETPOS=-1")
        elif freq == "YEARLY":
            if mod_kind == 0:
                parts.append(f"BYMONTH={rng.choice([1, 2, 3, 11, 12])}")
                parts.append("BYDAY=" + rng.choice(["1MO", "-1SU", "4TH"]))
            elif mod_kind == 1:
                parts.append("BYYEARDAY=" + rng.choice(["1,60,-1", "-1", "100,200"]))
            elif mod_kind == 2:
                parts.append("BYWEEKNO=" + rng.choice(["1", "-1", "1,52,53"]))
                parts.append("BYDAY=" + rng.choice(["MO", "FR", "SU"]))
                parts.append("WKST=" + rng.choice(["MO", "SU"]))
            elif mod_kind == 3:
                parts.append("BYDAY=" + rng.choice(["1MO,-1FR", "53MO", "20WE"]))
        elif freq == "DAILY" and mod_kind in (0, 1):
            parts.append("BYDAY=" + ",".join(rng.sample(days, rng.randint(2, 4))))
        elif freq == "HOURLY" and mod_kind in (0, 1):
            parts.append("BYHOUR=" + rng.choice(["0,1,2,3", "9,12,17", "22,23"]))

        if rng.random() < 0.6:
            parts.append(f"COUNT={rng.randint(2, 12)}")
        else:
            u_year = year + (2 if freq == "YEARLY" else 1)
            if tz == "DATE":
                parts.append(f"UNTIL={u_year:04d}{month:02d}{day:02d}")
            else:
                parts.append(f"UNTIL={u_year:04d}{month:02d}{day:02d}T235959Z")

        rrule = ";".join(parts)
        if tz == "DATE":
            dtstart = f"DTSTART;VALUE=DATE:{year:04d}{month:02d}{day:02d}"
            dur = rng.choice(["DURATION:P1D", "DURATION:P2D", ""])
            exdate = f"EXDATE;VALUE=DATE:{year:04d}{month:02d}{min(day + 1, 28):02d}"
        elif tz == "UTC":
            dtstart = f"DTSTART:{year:04d}{month:02d}{day:02d}T{hour:02d}{minute:02d}00Z"
            dur = rng.choice(["DURATION:PT1H", "DURATION:PT30M", "DURATION:PT2H30M", ""])
            exdate = f"EXDATE:{year:04d}{month:02d}{min(day + 1, 28):02d}T{hour:02d}{minute:02d}00Z"
        else:
            dtstart = f"DTSTART;TZID={tz}:{year:04d}{month:02d}{day:02d}T{hour:02d}{minute:02d}00"
            dur = rng.choice(["DURATION:PT1H", "DURATION:PT30M", ""])
            exdate = f"EXDATE;TZID={tz}:{year:04d}{month:02d}{min(day + 1, 28):02d}T{hour:02d}{minute:02d}00"

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Fuzz//EN",
            "BEGIN:VEVENT",
            f"UID:fuzz-{idx}",
            dtstart,
        ]
        if dur:
            lines.append(dur)
        lines.append(f"RRULE:{rrule}")
        if idx % 3 == 0:
            lines.append(exdate)
        lines.append(f"SUMMARY:Fuzz {idx}")
        lines.append("END:VEVENT")
        if idx % 7 == 0 and tz != "DATE":
            rid = (
                f"RECURRENCE-ID:{year:04d}{month:02d}{day:02d}T{hour:02d}{minute:02d}00Z"
                if tz == "UTC"
                else f"RECURRENCE-ID;TZID={tz}:{year:04d}{month:02d}{day:02d}T{hour:02d}{minute:02d}00"
            )
            ov_start = (
                f"DTSTART:{year:04d}{month:02d}{day:02d}T{(hour + 1) % 24:02d}1500Z"
                if tz == "UTC"
                else f"DTSTART;TZID={tz}:{year:04d}{month:02d}{day:02d}T{(hour + 1) % 24:02d}1500"
            )
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:fuzz-{idx}",
                    rid,
                    ov_start,
                    "DURATION:PT45M",
                    f"SUMMARY:Fuzz {idx} Override",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")
        ics = ("\r\n".join(lines) + "\r\n").encode("utf-8")

        ws = datetime.datetime(year, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
        we = datetime.datetime(year + 3, 12, 31, 23, 59, tzinfo=datetime.timezone.utc)
        up_occs = up_rie.of(up_ical.Calendar.from_ical(ics)).between(ws, we)
        ox_occs = compat_of(Calendar.from_ical(ics)).between(ws, we)

        dur_secs_map = {
            "DURATION:P1D": 86400,
            "DURATION:P2D": 172800,
            "DURATION:PT1H": 3600,
            "DURATION:PT30M": 1800,
            "DURATION:PT2H30M": 9000,
            "": 0,
        }
        expected_dur_secs = dur_secs_map[dur]

        for occ in ox_occs:
            s, e = occ.decoded("DTSTART"), occ.decoded("DTEND")
            if isinstance(s, datetime.datetime) and isinstance(e, datetime.datetime):
                delta_secs = int(
                    (e.astimezone(datetime.timezone.utc) - s.astimezone(datetime.timezone.utc)).total_seconds()
                )
                assert delta_secs >= 0
                if "Override" not in str(occ.decoded("SUMMARY", "")):
                    assert delta_secs == expected_dur_secs

        ox_items = _extract_occ_set(ox_occs)
        up_items = _extract_occ_set(up_occs)
        if ox_items == up_items:
            continue

        ox_counter = Counter(ox_items)
        up_counter = Counter(up_items)
        ox_only = list((ox_counter - up_counter).elements())
        up_only = list((up_counter - ox_counter).elements())

        # Pair unmatched occurrences that agree on (kind, uid, summary, start_utc)
        # where upstream recurring-ical-events 3.8.2 miscomputes DTEND across DST.
        remaining_ox: list[tuple[Any, ...]] = []
        up_by_key: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        for item in up_only:
            k, u, sm, s_norm, _ = item
            s_utc = s_norm[1] if isinstance(s_norm, tuple) and s_norm[0] == "dt" else s_norm
            up_by_key.setdefault((k, u, sm, s_utc), []).append(item)

        for item in ox_only:
            k, u, sm, s_norm, e_norm = item
            s_utc = s_norm[1] if isinstance(s_norm, tuple) and s_norm[0] == "dt" else s_norm
            e_utc = e_norm[1] if isinstance(e_norm, tuple) and e_norm[0] == "dt" else e_norm
            key = (k, u, sm, s_utc)
            if up_by_key.get(key) and isinstance(s_utc, datetime.datetime) and isinstance(e_utc, datetime.datetime):
                up_match = up_by_key[key].pop(0)
                up_s_utc = up_match[3][1]
                up_e_utc, up_e_off, _ = up_match[4][1], up_match[4][2], up_match[4][3]
                ox_dur = int((e_utc - s_utc).total_seconds())
                up_dur = int((up_e_utc - up_s_utc).total_seconds())
                if "Override" not in sm:
                    assert ox_dur == expected_dur_secs
                # Upstream either gives wrong UTC duration across DST OR emits a non-existent
                # spring-forward gap DTEND with a stale pre-transition UTC offset.
                real_end_off = int(e_utc.astimezone(ZoneInfo(tz)).utcoffset().total_seconds()) if tz != "UTC" else 0
                assert e_norm[2] == real_end_off
                assert up_dur != ox_dur or up_e_off != real_end_off, (
                    f"Expected upstream DST DTEND bug at idx={idx}: {item} vs {up_match}"
                )
            else:
                remaining_ox.append(item)

        remaining_up = [item for lst in up_by_key.values() for item in lst]

        # Prove any remaining ox-only item is upstream recurring-ical-events's HOURLY non-UTC EXDATE bug.
        if remaining_ox and freq == "HOURLY" and idx % 3 == 0 and tz not in ("UTC", "DATE"):
            ex_dt = datetime.datetime(year, month, min(day + 1, 28), hour, minute, 0, tzinfo=ZoneInfo(tz))
            ex_utc = ex_dt.astimezone(datetime.timezone.utc)
            ics_no_ex = ("\r\n".join([ln for ln in lines if not ln.startswith("EXDATE")]) + "\r\n").encode("utf-8")
            up_no_ex_set = set(_extract_occ_set(up_rie.of(up_ical.Calendar.from_ical(ics_no_ex)).between(ws, we)))
            still_unexplained = []
            for item in remaining_ox:
                s_utc = item[3][1]
                if s_utc != ex_utc and item in up_no_ex_set:
                    continue
                still_unexplained.append(item)
            remaining_ox = still_unexplained

        assert not remaining_ox and not remaining_up, (
            f"Unexplained fuzz diff idx={idx} tz={tz} {dtstart} {dur} {rrule}: ox={remaining_ox} up={remaining_up}"
        )


def test_dst_fallback_fold_preserves_fold1_and_duration() -> None:
    """Fall-back folds set fold=1 cleanly so end is never before start (IANA & custom VTIMEZONE)."""
    ny_hourly = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:fh1\r\n"
        b"DTSTART;TZID=America/New_York:20261101T003000\r\n"
        b"DURATION:PT30M\r\nRRULE:FREQ=HOURLY;INTERVAL=1;COUNT=6\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    occs = compat_of(Calendar.from_ical(ny_hourly)).between((2026, 10, 31), (2026, 11, 2))
    assert len(occs) == 6
    for o in occs:
        s = o.decoded("DTSTART")
        e = o.decoded("DTEND")
        assert e.astimezone(datetime.timezone.utc) - s.astimezone(datetime.timezone.utc) == datetime.timedelta(minutes=30)
    # 01:30 EDT (05:30Z) -> 01:00 EST (fold=1, 06:00Z)
    fold_end = occs[1].decoded("DTEND")
    assert fold_end.hour == 1 and fold_end.fold == 1

    ny_daily = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:fd1\r\n"
        b"DTSTART;TZID=America/New_York:20261031T013000\r\n"
        b"DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=4\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    daily_occs = compat_of(Calendar.from_ical(ny_daily)).between((2026, 10, 30), (2026, 11, 5))
    nov1 = daily_occs[1]
    assert nov1.decoded("DTSTART").utcoffset() == datetime.timedelta(hours=-4)
    assert nov1.decoded("DTEND").fold == 1
    assert nov1.decoded("DTEND").utcoffset() == datetime.timedelta(hours=-5)

    custom_vtz = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//VTZ//EN\r\n"
        b"BEGIN:VTIMEZONE\r\nTZID:CustomFold+0345\r\n"
        b"BEGIN:DAYLIGHT\r\nDTSTART:20260329T020000\r\nTZOFFSETFROM:+0345\r\nTZOFFSETTO:+0445\r\nEND:DAYLIGHT\r\n"
        b"BEGIN:STANDARD\r\nDTSTART:20261025T020000\r\nTZOFFSETFROM:+0445\r\nTZOFFSETTO:+0345\r\nEND:STANDARD\r\n"
        b"END:VTIMEZONE\r\n"
        b"BEGIN:VEVENT\r\nUID:cvtz\r\n"
        b"DTSTART;TZID=CustomFold+0345:20261025T003000\r\n"
        b"DURATION:PT1H\r\nRRULE:FREQ=HOURLY;COUNT=4\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    c_occs = compat_of(Calendar.from_ical(custom_vtz)).between((2026, 10, 24), (2026, 10, 26))
    for o in c_occs:
        s = o.decoded("DTSTART").astimezone(datetime.timezone.utc)
        e = o.decoded("DTEND").astimezone(datetime.timezone.utc)
        assert e - s == datetime.timedelta(hours=1)


def test_rrule_and_after_final_until_period_multi_candidate() -> None:
    """Draining final UNTIL period emits every valid candidate on between() and streaming after()."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//D//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:syd\r\n"
        b"DTSTART;TZID=Australia/Sydney:20240616T010000\r\n"
        b"DURATION:PT30M\r\n"
        b"RRULE:FREQ=WEEKLY;BYDAY=MO,TH,TU,WE;WKST=SA;UNTIL=20250616T235959Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ws = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    up_cal = up_ical.Calendar.from_ical(ics)
    ox_cal = Calendar.from_ical(ics)
    up_between = _extract_occ_set(up_rie.of(up_cal).between(ws, we))
    ox_between = _extract_occ_set(compat_of(ox_cal).between(ws, we))
    ox_after = _extract_occ_set(list(compat_of(ox_cal).after(ws)))
    assert len(ox_between) == 211
    assert ox_between == up_between
    assert ox_after == up_between


def test_deepcopy_and_pickle_lazy_materialized_and_native() -> None:
    """copy.deepcopy and pickle work cleanly on lazy/materialized compat and native objects."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//P//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:p1\r\nDTSTART:20260701T100000Z\r\nSUMMARY:Orig\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    native = icaloxide.Calendar.from_ical(ics)
    native_rt = pickle.loads(pickle.dumps(native))
    assert native_rt.events[0].summary == "Orig"
    comp_rt = pickle.loads(pickle.dumps(native.events[0]))
    assert comp_rt.summary == "Orig"

    lazy_cal = Calendar.from_ical(ics)
    dc_lazy = copy.deepcopy(lazy_cal)
    assert lazy_cal.is_lazy() and dc_lazy.is_lazy()
    dc_lazy.events[0]["SUMMARY"] = "Cloned"
    assert lazy_cal.events[0].summary == "Orig"
    assert dc_lazy.events[0].summary == "Cloned"

    pk_lazy = pickle.loads(pickle.dumps(lazy_cal))
    assert pk_lazy.events[0].summary == "Orig"

    mat_cal = Calendar.from_ical(ics)
    mat_cal.events[0]["LOCATION"] = "Berlin"
    dc_mat = copy.deepcopy(mat_cal)
    dc_mat.events[0]["LOCATION"] = "Tokyo"
    assert mat_cal.events[0].location == "Berlin"
    assert dc_mat.events[0].location == "Tokyo"
    pk_mat = pickle.loads(pickle.dumps(dc_mat))
    assert pk_mat.events[0].location == "Tokyo"


def test_between_point_query_and_exclusive_date_bounds_and_extreme_tz_at() -> None:
    """between() excludes stop date, supports start==stop point queries, and at() hits +14/-11 TZs."""
    b_ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//B//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:e1\r\nDTSTART:20260704T100000Z\r\nDTEND:20260704T120000Z\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:e2\r\nDTSTART:20260706T100000Z\r\nDTEND:20260706T110000Z\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    q = compat_of(Calendar.from_ical(b_ics))
    assert [c.uid for c in q.between(datetime.date(2026, 7, 4), datetime.date(2026, 7, 6))] == ["e1"]
    assert [c.uid for c in q.between((2026, 7, 4), (2026, 7, 6))] == ["e1"]
    assert q.count(datetime.date(2026, 7, 4), datetime.date(2026, 7, 6)) == 1

    pt = datetime.datetime(2026, 7, 4, 11, 0, tzinfo=datetime.timezone.utc)
    assert [c.uid for c in q.between(pt, pt)] == ["e1"]
    assert q.between(pt, pt - datetime.timedelta(minutes=1)) == []

    ext_ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Ext//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:kiri\r\nDTSTART;TZID=Pacific/Kiritimati:20260701T010000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:niue\r\nDTSTART;TZID=Pacific/Niue:20260701T230000\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    eq = compat_of(Calendar.from_ical(ext_ics))
    for arg in (datetime.date(2026, 7, 1), (2026, 7, 1), "20260701"):
        assert sorted(c.uid for c in eq.at(arg)) == ["kiri", "niue"]


def test_jcal_and_xcal_period_lists_rdate_and_freebusy() -> None:
    """RDATE;VALUE=PERIOD and FREEBUSY emit RFC 7265 period jCal and roundtrip via jCal/xCal."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//J//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:mp\r\n"
        b"DTSTART;TZID=America/New_York:20260701T100000\r\n"
        b"RDATE;TZID=America/New_York;VALUE=PERIOD:20260702T100000/PT1H,20260703T100000/20260703T120000\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VFREEBUSY\r\nUID:fb1\r\n"
        b"DTSTART:20260701T080000Z\r\nDTEND:20260701T180000Z\r\n"
        b"FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260701T100000Z/20260701T110000Z,20260701T120000Z/PT30M\r\n"
        b"END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    jc = cal.to_jcal()
    ev_props = {p[0]: p for p in jc[2][0][1]}
    fb_props = {p[0]: p for p in jc[2][1][1]}
    assert ev_props["rdate"][2] == "period"
    assert ev_props["rdate"][3:] == [
        "2026-07-02T10:00:00/PT1H",
        "2026-07-03T10:00:00/2026-07-03T12:00:00",
    ]
    assert fb_props["freebusy"][2] == "period"
    assert fb_props["freebusy"][3:] == [
        "2026-07-01T10:00:00Z/2026-07-01T11:00:00Z",
        "2026-07-01T12:00:00Z/PT30M",
    ]

    cal_from_jc = Calendar.from_jcal(jc)
    assert len(cal_from_jc.events[0].rdates) == 2
    cal_from_xc = Calendar.from_xcal(cal.to_xcal())
    assert len(cal_from_xc.events[0].rdates) == 2

    # Parse icalendar 7.x 2-element list period jCal too
    up_jc = up_ical.Calendar.from_ical(ics).to_jcal()
    cal_from_up_jc = Calendar.from_jcal(up_jc)
    assert len(cal_from_up_jc.events[0].rdates) == 2


def test_count0_backwards_dtend_undated_vtodo_and_windows_x_wr_timezone() -> None:
    """COUNT=0, DTEND<DTSTART, undated VTODO, and Windows X-WR-TIMEZONE behave cleanly."""
    ics = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Edge//EN\r\n"
        b"X-WR-TIMEZONE:Eastern Standard Time\r\n"
        b"BEGIN:VEVENT\r\nUID:back\r\nDTSTART:20260702T100000Z\r\nDTEND:20260701T100000Z\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:cnt0\r\nDTSTART:20260702T100000Z\r\nRRULE:FREQ=DAILY;COUNT=0\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:wintz\r\nDTSTART:20260701T100000\r\nDTEND:20260701T110000\r\nEND:VEVENT\r\n"
        b"BEGIN:VTODO\r\nUID:undated\r\nSUMMARY:Someday\r\nEND:VTODO\r\n"
        b"END:VCALENDAR\r\n"
    )
    cal = Calendar.from_ical(ics)
    ws = datetime.datetime(2026, 6, 30, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 7, 5, tzinfo=datetime.timezone.utc)

    evs = {e.uid: e for e in compat_of(cal, components=["VEVENT"]).between(ws, we)}
    assert set(evs) == {"back", "cnt0", "wintz"}
    assert evs["back"].decoded("DTSTART") == datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)
    assert evs["back"].decoded("DTEND") == datetime.datetime(2026, 7, 2, 10, 0, tzinfo=datetime.timezone.utc)
    assert getattr(evs["wintz"].decoded("DTSTART").tzinfo, "key", None) == "America/New_York"

    todos = compat_of(cal, components=["VTODO"]).between(ws, we)
    assert len(todos) == 1
    assert todos[0].uid == "undated"
    assert todos[0].decoded("DTSTART", None) is None
    assert "DTSTART" not in todos[0]
    assert compat_of(cal, components=["VTODO"]).count(ws, we) == 1
    native_cal = icaloxide.Calendar.from_ical(ics)
    assert native_cal.query(components=["VTODO"]).between(ws, we) == []
    assert len(native_cal.query(components=["VTODO"], include_undated=True).between(ws, we)) == 1
    assert native_cal.query(components=["VTODO"], include_undated=True).busy_intervals(ws, we) == []


def test_500_series_dst_exdate_override_exact_parity_and_evil_parser() -> None:
    """500 overlapping DST+EXDATE+override series match upstream set-for-set + split-UTF8 parsing."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stress//EN"]
    tzs = ["America/New_York", "Europe/Berlin", "Asia/Tokyo", "Australia/Sydney", "UTC"]
    for i in range(250):
        tz = tzs[i % len(tzs)]
        tzp = "" if tz == "UTC" else f";TZID={tz}"
        zs = "Z" if tz == "UTC" else ""
        d = (i % 27) + 1
        h = 7 + (i % 12)
        rule = ["FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR", "FREQ=MONTHLY;BYDAY=-1FR"][i % 2]
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:st-{i}",
                f"DTSTART{tzp}:202201{d:02d}T{h:02d}0000{zs}",
                "DURATION:PT45M",
                f"RRULE:{rule}",
                f"EXDATE{tzp}:202606{d:02d}T{h:02d}0000{zs}",
                f"SUMMARY:S#{i}",
                "END:VEVENT",
                "BEGIN:VEVENT",
                f"UID:st-{i}",
                f"RECURRENCE-ID{tzp}:202607{d:02d}T{h:02d}0000{zs}",
                f"DTSTART{tzp}:202607{d:02d}T{h + 1:02d}1500{zs}",
                "DURATION:PT1H",
                f"SUMMARY:S#{i}-M",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    stress_ics = ("\r\n".join(lines) + "\r\n").encode("utf-8")

    ws = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    we = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)
    up_set = _extract_occ_set(up_rie.of(up_ical.Calendar.from_ical(stress_ics)).between(ws, we))
    ox_set = _extract_occ_set(compat_of(Calendar.from_ical(stress_ics)).between(ws, we))
    assert ox_set == up_set
    assert len(ox_set) > 10_000

    evil_ics = (
        b"\xef\xbb\xbf"
        b"begin:vcalendar\rversion:2.0\rprodid:-//Evil//EN\r"
        b"BEGIN:VTIMEZONE\r\nTZID:HistAmt\r\nBEGIN:STANDARD\r\nDTSTART:19090501T000000\r\n"
        b"TZOFFSETFROM:+001932\r\nTZOFFSETTO:+001932\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        b"BEGIN:VEVENT\r\nUID:evil\r\n"
        b"DTSTART;TZID=HistAmt:19200601T120000\r\n"
        b'ATTENDEE;CN="O^\'Brien^nLine2^^";MEMBER="mailto:a@x","mailto:b@x":mailto:c@x\r\n'
        b"SUMMARY:Hello\\, World\\; Line1\\nLine2\\NLine3\\\\End\r\n"
        b"DESCRIPTION:SplitEmoji \xf0\x9f\r\n \x8e\x89End\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ev = Calendar.from_ical(evil_ics).events[0]
    assert ev.decoded("DESCRIPTION") == "SplitEmoji 🎉End"
    assert ev.decoded("SUMMARY") == "Hello, World; Line1\nLine2\nLine3\\End"
    assert ev.get("ATTENDEE").params["CN"] == 'O"Brien\nLine2^'
    assert ev.get("ATTENDEE").params["MEMBER"] == ["mailto:a@x", "mailto:b@x"]
    assert ev.decoded("DTSTART").utcoffset() == datetime.timedelta(minutes=19, seconds=32)
    assert Event.from_ical(ev.to_ical()).decoded("DESCRIPTION") == "SplitEmoji 🎉End"
