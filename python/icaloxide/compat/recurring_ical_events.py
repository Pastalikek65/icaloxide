"""Drop-in compatibility layer mimicking Python's ``recurring_ical_events`` package."""

from __future__ import annotations

import datetime
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

import icaloxide as _ox
from icaloxide.compat.icalendar import Calendar as CompatCalendar
from icaloxide.compat.icalendar import Component, _wrap_rust_component

DateTuple = Union[Tuple[int], Tuple[int, int], Tuple[int, int, int]]
TimePoint = Union[datetime.date, datetime.datetime, DateTuple]


def _normalize_span(
    arg: TimePoint,
) -> Tuple[Union[datetime.date, datetime.datetime], Union[datetime.date, datetime.datetime]]:
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


def _infer_tz_name(dt: Any) -> Optional[str]:
    if not isinstance(dt, datetime.datetime) or dt.tzinfo is None:
        return None
    key = getattr(dt.tzinfo, "key", None)
    if key:
        return str(key)
    off = dt.utcoffset()
    if off is not None:
        secs = int(off.total_seconds())
        if secs == 0:
            return "UTC"
        sign = "-" if secs < 0 else "+"
        abs_s = abs(secs)
        return f"UTC{sign}{abs_s // 3600:02}:{(abs_s % 3600) // 60:02}"
    return None


class UnfoldableCalendar:
    """Drop-in replacement for ``recurring_ical_events.UnfoldableCalendar``."""

    def __init__(
        self,
        rust_cal: _ox.Calendar,
        *,
        keep_recurrence_attributes: bool = False,
        components: Sequence[str] = ("VEVENT",),
        skip_bad_series: bool = False,
    ) -> None:
        self._rust_cal = rust_cal
        self._keep_recurrence = keep_recurrence_attributes
        self._components = list(components)
        self._skip_bad_series = skip_bad_series
        self._query = rust_cal.query(
            components=self._components,
            skip_bad_series=skip_bad_series,
        )

    def _query_for(self, bound: Any) -> _ox.CalendarQuery:
        if self._rust_cal.default_timezone is None:
            inferred = _infer_tz_name(bound)
            if inferred and inferred != "UTC":
                return self._rust_cal.query(
                    components=self._components,
                    skip_bad_series=self._skip_bad_series,
                    default_tz=inferred,
                )
        return self._query

    @property
    def suppressed_errors(self) -> list[str]:
        return self._query.suppressed_errors

    def at(self, a_date: TimePoint) -> list[Component]:
        if isinstance(a_date, tuple):
            start, end = _normalize_span(a_date)
            occs = self._query.between(start, end)
            return [
                _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
                for o in occs
            ]
        occs = self._query_for(a_date).at(a_date)
        return [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in occs
        ]

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
        occs = self._query_for(s_val).between(s_val, e_val)
        return [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in occs
        ]

    def after(
        self, earliest_end: Union[datetime.date, datetime.datetime]
    ) -> Iterator[Component]:
        for occ in self._query_for(earliest_end).after(earliest_end):
            yield _wrap_rust_component(occ, keep_recurrence=self._keep_recurrence)

    def paginate(
        self,
        page_size: int,
        earliest_end: Optional[Union[datetime.date, datetime.datetime]] = None,
    ) -> Iterator[list[Component]]:
        q = self._query_for(earliest_end) if earliest_end is not None else self._query
        for page in q.paginate(page_size, earliest_end):
            yield [
                _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
                for o in page
            ]

    @property
    def first(self) -> Optional[Component]:
        occ = self._query.first()
        if occ is None:
            return None
        return _wrap_rust_component(occ, keep_recurrence=self._keep_recurrence)

    def all(self) -> list[Component]:
        return [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in self._query.all()
        ]

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
            return self._query_for(s_val).count(s_val, e_val)

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
        keep_recurrence_attributes=keep_recurrence_attributes,
        components=components,
        skip_bad_series=skip_bad_series,
    )


__all__ = ["UnfoldableCalendar", "of"]
