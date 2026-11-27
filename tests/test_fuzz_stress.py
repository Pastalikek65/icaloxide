"""Adversarial, malformed, and fuzz-mutated corpus stress tests."""

from __future__ import annotations

import datetime
import random

import icaloxide
import pytest

SEED_CORPUS = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//icaloxide//fuzz//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:fuzz-1\r\n"
    b"DTSTART;TZID=Europe/Istanbul:20261127T100000\r\n"
    b"SUMMARY;ALTREP=\"http://example.com:8080/a;b,c\":Test\r\n"
    b"RRULE:FREQ=DAILY;COUNT=3\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


def test_lenient_parser_never_crashes_on_mutated_inputs() -> None:
    rng = random.Random(20261127)
    for _ in range(250):
        buf = bytearray(SEED_CORPUS)
        for _ in range(rng.randint(1, 12)):
            op = rng.randrange(3)
            if op == 0 and buf:
                del buf[rng.randrange(len(buf))]
            elif op == 1 and buf:
                buf[rng.randrange(len(buf))] = rng.randrange(256)
            else:
                buf.insert(rng.randrange(len(buf) + 1), rng.randrange(256))

        try:
            cal = icaloxide.Calendar.from_ical(bytes(buf), strict=False)
            _ = cal.to_ical()
        except ValueError:
            # Clean parse errors (e.g., MissingVCalendarRoot / EmptyCalendar) are expected.
            pass


def test_dos_impossible_rrule_aborts_cleanly() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//icaloxide//dos//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:impossible\r\n"
        "DTSTART:20260101T000000Z\r\n"
        "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = icaloxide.Calendar.from_ical(ics, strict=True)
    q = cal.query()
    with pytest.raises(ValueError, match="safety budget"):
        q.between(
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc),
        )


def test_deep_nesting_aborts_cleanly() -> None:
    nested = b"BEGIN:VCALENDAR\r\n" + (b"BEGIN:VEVENT\r\n" * 64) + (b"END:VEVENT\r\n" * 64) + b"END:VCALENDAR\r\n"
    with pytest.raises(ValueError, match="nesting depth"):
        icaloxide.Calendar.from_ical(nested, strict=False)
