"""Drop-in compatibility layer mimicking Python's ``recurring_ical_events`` package."""

from __future__ import annotations

import datetime
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

import icaloxide as _ox
from icaloxide.compat.icalendar import Calendar as CompatCalendar
from icaloxide.compat.icalendar import Component, _wrap_rust_component

DateTuple = Union[Tuple[int], Tuple[int, int], Tuple[int, int, int]]
TimePoint = Union[datetime.date, datetime.datetime, DateTuple]


def _normalize_span(arg: TimePoint) -> Tuple[datetime.date | datetime.datetime, datetime.date | datetime.datetime]:
    """Normalize a date, datetime, or (Y[, M[, D]]) tuple into (start, end_inclusive_or_exclusive)."""
    if isinstance(arg, tuple):
        if len(arg) == 1:
            y = arg[0]
            return datetime.date(y, 1, 1), datetime.date(y, 12, 31)
        if len(arg) == 2:
            y, m = arg
            start = datetime.date(y, m, 1)
            next_m = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
            return start, next_m - datetime.timedelta(days=1)
        if len(arg) == 3:
            d = datetime.date(arg[0], arg[1], arg[2])
            return d, d
        raise ValueError(f"invalid date tuple: {arg!r}")
    return arg, arg


class UnfoldableCalendar:
    """Drop-in replacement for ``recurring_ical_events.UnfoldableCalendar``."""

    def __init__(
        self,
        rust_cal: _ox.Calendar,
        *,
        components: Sequence[str] = ("VEVENT",),
        skip_bad_series: bool = False,
    ) -> None:
        _ = skip_bad_series
        self._rust_cal = rust_cal
        self._components = tuple(components)
        self._query = rust_cal.query(components=list(components))

    def at(self, a_date: TimePoint) -> list[Component]:
        if isinstance(a_date, tuple):
            start, end = _normalize_span(a_date)
            occs = self._query.between(start, end)
            return [_wrap_rust_component(o) for o in occs]
        occs = self._query.at(a_date)
        return [_wrap_rust_component(o) for o in occs]

    def between(
        self,
        start: TimePoint,
        stop: Union[TimePoint, datetime.timedelta],
    ) -> list[Component]:
        s_val, _ = _normalize_span(start)
        if isinstance(stop, datetime.timedelta):
            if isinstance(s_val, datetime.datetime):
                e_val: Any = s_val + stop
            else:
                e_val = datetime.datetime.combine(s_val, datetime.time.min) + stop
        else:
            _, e_val = _normalize_span(stop)
        occs = self._query.between(s_val, e_val)
        return [_wrap_rust_component(o) for o in occs]

    def after(self, earliest_end: Union[datetime.date, datetime.datetime]) -> Iterator[Component]:
        for occ in self._query.after(earliest_end):
            yield _wrap_rust_component(occ)

    def count(
        self,
        start: Optional[TimePoint] = None,
        stop: Optional[Union[TimePoint, datetime.timedelta]] = None,
    ) -> int:
        if start is not None and stop is not None:
            s_val, _ = _normalize_span(start)
            if isinstance(stop, datetime.timedelta):
                if isinstance(s_val, datetime.datetime):
                    e_val: Any = s_val + stop
                else:
                    e_val = datetime.datetime.combine(s_val, datetime.time.min) + stop
            else:
                _, e_val = _normalize_span(stop)
            return self._query.count(s_val, e_val)

        # Unbounded count over finite rules (from epoch 1900 to 2100)
        return self._query.count(
            datetime.date(1900, 1, 1),
            datetime.date(2100, 12, 31),
        )


def of(
    a_calendar: Any,
    keep_recurrence_attributes: bool = False,
    components: Sequence[str] = ("VEVENT",),
    skip_bad_series: bool = False,
) -> UnfoldableCalendar:
    """Wrap an ``icaloxide.Calendar``, ``compat.Calendar``, or upstream ``icalendar.Calendar``."""
    _ = keep_recurrence_attributes
    if isinstance(a_calendar, _ox.Calendar):
        rust_cal = a_calendar
    elif isinstance(a_calendar, CompatCalendar):
        rust_cal = a_calendar._as_rust_calendar()
    elif hasattr(a_calendar, "to_ical"):
        rust_cal = _ox.Calendar.from_ical(a_calendar.to_ical())
    else:
        raise TypeError("expected icaloxide.Calendar or object with .to_ical()")
    return UnfoldableCalendar(
        rust_cal,
        components=components,
        skip_bad_series=skip_bad_series,
    )


__all__ = ["UnfoldableCalendar", "of"]
