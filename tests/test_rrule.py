import datetime
import itertools
from zoneinfo import ZoneInfo

import icaloxide


RRULE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//icaloxide//pytest-rrule//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:daily-standup@icaloxide\r\n"
    "DTSTART;TZID=America/New_York:20260306T090000\r\n"
    "DURATION:PT30M\r\n"
    "RRULE:FREQ=DAILY\r\n"
    "EXDATE;TZID=America/New_York:20260307T090000\r\n"
    "SUMMARY:Daily Standup\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "TRIGGER:-PT15M\r\n"
    "DESCRIPTION:Standup soon\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:daily-standup@icaloxide\r\n"
    "RECURRENCE-ID;TZID=America/New_York:20260309T090000\r\n"
    "DTSTART;TZID=America/New_York:20260309T113000\r\n"
    "DURATION:PT45M\r\n"
    "SUMMARY:Extended Standup\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:allday-holiday@icaloxide\r\n"
    "DTSTART;VALUE=DATE:20260308\r\n"
    "SUMMARY:All Day Event\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_query_between_at_count_and_dst_preservation() -> None:
    cal = icaloxide.Calendar.from_ical(RRULE_ICS, strict=True)
    q = cal.query()

    start = datetime.datetime(2026, 3, 6, 0, 0, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 3, 11, 0, 0, tzinfo=datetime.timezone.utc)

    occs = q.between(start, end)
    assert q.count(start, end) == len(occs)

    # Mar 6 (standup), Mar 7 (excluded), Mar 8 (all-day + standup), Mar 9 (override), Mar 10 (standup)
    assert len(occs) == 5

    ny = ZoneInfo("America/New_York")
    standups = [o for o in occs if not o.is_all_day]
    assert len(standups) == 4
    assert standups[0].start == datetime.datetime(2026, 3, 6, 9, 0, tzinfo=ny)
    assert standups[1].start == datetime.datetime(2026, 3, 8, 9, 0, tzinfo=ny)
    assert standups[2].start == datetime.datetime(2026, 3, 9, 11, 30, tzinfo=ny)
    assert standups[2].summary == "Extended Standup"
    assert standups[2]["DTSTART"] == datetime.datetime(2026, 3, 9, 11, 30, tzinfo=ny)
    assert standups[3].start == datetime.datetime(2026, 3, 10, 9, 0, tzinfo=ny)

    all_day = [o for o in occs if o.is_all_day]
    assert len(all_day) == 1
    assert all_day[0].start == datetime.date(2026, 3, 8)
    assert all_day[0].end == datetime.date(2026, 3, 9)

    on_mar8 = q.at(datetime.date(2026, 3, 8))
    assert any(o.summary == "All Day Event" for o in on_mar8)
    assert any(o.summary == "Daily Standup" for o in on_mar8)


def test_streaming_after_and_valarm_query() -> None:
    cal = icaloxide.Calendar.from_ical(RRULE_ICS, strict=True)
    q = cal.query()

    first_3 = list(
        itertools.islice(
            q.after(datetime.datetime(2026, 6, 1, 0, 0, tzinfo=datetime.timezone.utc)),
            3,
        )
    )
    assert len(first_3) == 3
    assert [o.start.date() for o in first_3] == [
        datetime.date(2026, 6, 1),
        datetime.date(2026, 6, 2),
        datetime.date(2026, 6, 3),
    ]

    limited = list(
        q.after(
            datetime.datetime(2026, 6, 1, 0, 0, tzinfo=datetime.timezone.utc),
            limit=2,
        )
    )
    assert len(limited) == 2

    alarm_q = cal.query(components=["VALARM"])
    alarms = alarm_q.between(
        datetime.datetime(2026, 3, 6, 0, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 3, 6, 23, 59, tzinfo=datetime.timezone.utc),
    )
    assert len(alarms) == 1
    assert alarms[0].name == "VALARM"
    assert alarms[0].start == datetime.datetime(
        2026, 3, 6, 13, 45, tzinfo=datetime.timezone.utc
    )
