import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import icaloxide
import pytest

SAMPLE_ICS = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//icaloxide//pytest//EN\r\n"
    b"NAME:Proje Takvimi\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:ev-101@icaloxide\r\n"
    b"DTSTAMP:20261127T000000Z\r\n"
    b"DTSTART;TZID=Europe/Istanbul:20261127T143000\r\n"
    b"DURATION:PT1H30M\r\n"
    b"SUMMARY:Sprint Planlama \xc4\x9f\xc3\xbc\xc5\x9f\xc4\xb1\xc3\xb6\xc3\xa7\xf0\x9f\x97\x93\xef\xb8\x8f\r\n"
    b"CATEGORIES:Rust\\, Core,Python\r\n"
    b"PRIORITY:1\r\n"
    b"GEO:41.0082;28.9784\r\n"
    b"BEGIN:VALARM\r\n"
    b"ACTION:DISPLAY\r\n"
    b"TRIGGER:-PT15M\r\n"
    b"DESCRIPTION:Alarm\r\n"
    b"END:VALARM\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


@pytest.mark.parametrize(
    "factory",
    [
        lambda p: SAMPLE_ICS,
        lambda p: bytearray(SAMPLE_ICS),
        lambda p: memoryview(SAMPLE_ICS),
        lambda p: SAMPLE_ICS.decode("utf-8"),
        lambda p: p,
    ],
)
def test_from_ical_accepts_all_input_types(tmp_path: Path, factory) -> None:
    ics_path = tmp_path / "sample.ics"
    ics_path.write_bytes(SAMPLE_ICS)
    cal = icaloxide.Calendar.from_ical(factory(ics_path))
    assert cal.name == "Proje Takvimi"
    assert cal.prodid == "-//icaloxide//pytest//EN"
    assert len(cal) == 1
    assert cal.component_stats() == {"VEVENT": 1}


def test_lazy_component_property_conversions() -> None:
    cal = icaloxide.Calendar.from_ical(SAMPLE_ICS)
    assert len(cal.events) == 1
    ev = cal.events[0]

    assert ev.name == "VEVENT"
    assert ev.uid == "ev-101@icaloxide"
    assert ev.summary == "Sprint Planlama ğüşıöç🗓️"
    assert "SUMMARY" in ev
    assert "NONEXISTENT" not in ev
    assert ev.get("NONEXISTENT", "fallback") == "fallback"

    dtstart = ev["DTSTART"]
    assert isinstance(dtstart, datetime.datetime)
    assert dtstart == datetime.datetime(2026, 11, 27, 14, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert ev.params("DTSTART") == {"TZID": ["Europe/Istanbul"]}

    dtstamp = ev["DTSTAMP"]
    assert dtstamp == datetime.datetime(2026, 11, 27, 0, 0, tzinfo=datetime.timezone.utc)

    assert ev["DURATION"] == datetime.timedelta(hours=1, minutes=30)
    assert ev["CATEGORIES"] == ["Rust, Core", "Python"]
    assert ev["PRIORITY"] == 1
    assert ev["GEO"] == pytest.approx((41.0082, 28.9784))

    assert len(ev.subcomponents) == 1
    alarm = ev.subcomponents[0]
    assert alarm.name == "VALARM"
    assert alarm["TRIGGER"] == datetime.timedelta(minutes=-15)


def test_utf8_75_octet_folding_roundtrip() -> None:
    long_summary = "Türkçe karakter testi: ğüşıöçĞÜŞİÖÇ " * 4 + "🗓️" * 5
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:fold-1\r\n"
        f"SUMMARY:{long_summary}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    raw_out = cal.to_ical()
    for line in raw_out.split(b"\r\n"):
        if line:
            assert len(line) <= 75
            line.decode("utf-8")

    reparsed = icaloxide.Calendar.from_ical(raw_out, strict=True)
    assert reparsed.events[0].summary == long_summary


def test_calendar_new_and_strict_vs_lenient() -> None:
    cal = icaloxide.Calendar.new(prodid="-//myorg//app//EN", name="My Cal")
    assert cal.prodid == "-//myorg//app//EN"
    assert cal.name == "My Cal"
    assert b"BEGIN:VCALENDAR\r\n" in cal.to_ical()

    broken = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:INVALID\r\nEND:VCALENDAR\r\n"
    with pytest.raises(ValueError):
        icaloxide.Calendar.from_ical(broken, strict=True)

    lenient_cal = icaloxide.Calendar.from_ical(broken, strict=False)
    assert len(lenient_cal.warnings) >= 2
    assert lenient_cal.events[0]["DTSTART"] == "INVALID"
