"""Red-team v0.5.1 regression tests: sorted to_ical, Alarms, vDDDLists/vCategory, jCal, and exports."""

from __future__ import annotations

import datetime

import icalendar as up_ical
from icaloxide.compat import icalendar as ox_ical
from icaloxide.compat import prop as ox_prop
from icaloxide.compat import recurring_ical_events as ox_rie
from icaloxide.compat.alarms import Alarms
from icaloxide.compat.icalendar import Calendar, Event, vCategory, vDDDLists


def test_to_ical_sorted_kwarg_and_canonical_ordering() -> None:
    """Component.to_ical accepts sorted=True/False and sorts materialized properties when True."""
    cal = Calendar.new(prodid="-//Test//Test//EN")
    ev = Event()
    ev.add("Z-CUSTOM", "last")
    ev.add("SUMMARY", "Middle")
    ev.add("A-CUSTOM", "first")
    ev.add("UID", "sort-1")
    cal.add_component(ev)

    raw_sorted = cal.to_ical(sorted=True)
    raw_unsorted = cal.to_ical(sorted=False)
    lines_sorted = [line for line in raw_sorted.decode("utf-8").splitlines() if line]
    lines_unsorted = [line for line in raw_unsorted.decode("utf-8").splitlines() if line]

    assert lines_sorted.index("A-CUSTOM:first") < lines_sorted.index("SUMMARY:Middle")
    assert lines_unsorted.index("Z-CUSTOM:last") < lines_unsorted.index("SUMMARY:Middle")


def test_event_alarms_returns_alarms_object_and_supports_indexing() -> None:
    """ev.alarms returns an Alarms instance matching upstream .times AND supports ev.alarms[0]."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:ev-al\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"DTEND:20260701T110000Z\r\n"
        b"SUMMARY:Meeting\r\n"
        b"BEGIN:VALARM\r\n"
        b"ACTION:DISPLAY\r\n"
        b"TRIGGER:-PT15M\r\n"
        b"REPEAT:2\r\n"
        b"DURATION:PT5M\r\n"
        b"DESCRIPTION:Start\r\n"
        b"END:VALARM\r\n"
        b"BEGIN:VALARM\r\n"
        b"ACTION:DISPLAY\r\n"
        b"TRIGGER;RELATED=END:-PT10M\r\n"
        b"DESCRIPTION:End\r\n"
        b"END:VALARM\r\n"
        b"BEGIN:VALARM\r\n"
        b"ACTION:DISPLAY\r\n"
        b"TRIGGER;VALUE=DATE-TIME:20260701T093000Z\r\n"
        b"DESCRIPTION:Abs\r\n"
        b"END:VALARM\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    up_ev = up_ical.Calendar.from_ical(ics).events[0]
    ox_ev = Calendar.from_ical(ics).events[0]

    assert isinstance(ox_ev.alarms, Alarms)
    assert len(ox_ev.alarms) == 3
    assert ox_ev.alarms[0].decoded("ACTION") == "DISPLAY"

    up_times = sorted(t.trigger.isoformat() for t in up_ev.alarms.times)
    ox_times = sorted(t.trigger.isoformat() for t in ox_ev.alarms.times)
    assert ox_times == up_times


def test_exdate_vdddlists_and_categories_vcategory_wrappers() -> None:
    """EXDATE lists return vDDDLists and CATEGORIES return vCategory with .params and decoded()."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:wrap-1\r\n"
        b"DTSTART:20260701T100000Z\r\n"
        b"EXDATE;X-FLAG=1:20260702T100000Z,20260703T100000Z\r\n"
        b"CATEGORIES;LANGUAGE=en:Work,Urgent\r\n"
        b"SUMMARY:Wrap\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    ev = Calendar.from_ical(ics).events[0]

    exdates = ev["EXDATE"]
    assert isinstance(exdates, vDDDLists)
    assert len(exdates.dts) == 2
    assert exdates.params["X-FLAG"] == "1"
    assert ev.decoded("EXDATE") == [
        datetime.datetime(2026, 7, 2, 10, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 7, 3, 10, 0, tzinfo=datetime.timezone.utc),
    ]

    cats = ev["CATEGORIES"]
    assert isinstance(cats, vCategory)
    assert [str(c) for c in cats.cats] == ["Work", "Urgent"]
    assert cats.params["LANGUAGE"] == "en"
    assert ev.decoded("CATEGORIES") == ["Work", "Urgent"]

    # Materialize via mutation and verify roundtrip serialization
    ev["LOCATION"] = "Desk"
    assert b"CATEGORIES;LANGUAGE=en:Work,Urgent" in ev.to_ical()


def test_jcal_cal_address_uri_and_full_parity_with_upstream() -> None:
    """to_jcal emits RFC 7265 cal-address and uri types identically to upstream icalendar 7.3.2."""
    ics = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//T//T//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:jc1\r\n"
        b"DTSTART;TZID=America/New_York:20260701T090000\r\n"
        b"DTEND;TZID=America/New_York:20260701T100000\r\n"
        b"RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3\r\n"
        b"ATTENDEE;CN=Alice:mailto:a@ex.com\r\n"
        b"URL:https://example.com/meet\r\n"
        b"GEO:41.0082;28.9784\r\n"
        b"CATEGORIES:Work,Dev\r\n"
        b"SUMMARY:JCal Test\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    up_props = {p[0]: p for p in up_ical.Calendar.from_ical(ics).to_jcal()[2][0][1]}
    ox_props = {p[0]: p for p in Calendar.from_ical(ics).to_jcal()[2][0][1]}
    assert ox_props == up_props


def test_100_percent_icalendar_all_and_recurring_inner_shims() -> None:
    """Every symbol in upstream icalendar.__all__ exists on icaloxide.compat.icalendar."""
    missing = set(up_ical.__all__) - (set(dir(ox_ical)) | {"__version__"})
    assert missing == set()
    assert hasattr(ox_prop, "VPROPERTY")

    ev = Event.new(
        uid="s1",
        summary="SeriesTest",
        start=datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc),
        duration=datetime.timedelta(hours=1),
    )
    ev.add("RRULE", {"FREQ": "DAILY", "COUNT": 3})
    series = ox_rie.Series([ev])
    hits = series.between(datetime.date(2026, 7, 1), datetime.date(2026, 7, 5))
    assert len(hits) == 3

    occ = ox_rie.Occurrence(hits[0])
    assert occ.as_component().summary == "SeriesTest"
    al_occ = ox_rie.AlarmOccurrence(ev, parent=ev, trigger=hits[0].decoded("DTSTART"))
    assert al_occ.parent is ev
    assert issubclass(ox_rie.AbsoluteAlarmAdapter, ox_rie.ComponentAdapter)
    assert issubclass(ox_rie.AlarmSeriesRelativeToStart, ox_rie.Series)
    assert issubclass(ox_rie.AlarmSeriesRelativeToEnd, ox_rie.Series)
