"""Drop-in compatibility layer mimicking Python's ``recurring_ical_events`` package."""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

import icaloxide as _ox
from icaloxide.compat.errors import BadRuleStringFormat, PeriodEndBeforeStart
from icaloxide.compat.icalendar import Calendar as CompatCalendar
from icaloxide.compat.icalendar import Component, _wrap_rust_component

DATE_MIN = datetime.date(1970, 1, 1)
DATE_MAX = datetime.date(9999, 12, 31)
DATE_MIN_DT = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
DATE_MAX_DT = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)

DateTuple = Union[
    Tuple[int],
    Tuple[int, int],
    Tuple[int, int, int],
    Tuple[int, int, int, int],
    Tuple[int, int, int, int, int],
    Tuple[int, int, int, int, int, int],
]
DateArgument = Union[int, str, datetime.date, datetime.datetime, DateTuple]

AllKnownComponents = (
    "VEVENT",
    "VTODO",
    "VJOURNAL",
    "VFREEBUSY",
    "VALARM",
    "VAVAILABILITY",
    "AVAILABLE",
)


class OccurrenceID:
    """Identifier matching ``recurring_ical_events.OccurrenceID``."""

    def __init__(
        self,
        name: str,
        uid: str,
        recurrence_id: Any,
        start: Any,
    ) -> None:
        self.name = name
        self.uid = uid
        self.recurrence_id = recurrence_id
        self.start = start

    def to_string(self) -> str:
        s_str = self.start.isoformat() if hasattr(self.start, "isoformat") else str(self.start)
        r_str = (
            self.recurrence_id.isoformat()
            if hasattr(self.recurrence_id, "isoformat")
            else str(self.recurrence_id or "")
        )
        return f"{self.name}|{self.uid}|{r_str}|{s_str}"

    def __repr__(self) -> str:
        return f"OccurrenceID({self.to_string()!r})"


class ComponentsWithName:
    """Compatibility selector for named components matching ``recurring_ical_events``."""

    def __init__(self, *names: str) -> None:
        self.names = tuple(n.upper() for n in names)


class SelectComponents:
    """Component selection predicate matching ``recurring_ical_events.SelectComponents``."""

    def __init__(self, *names: str) -> None:
        self.names = tuple(n.upper() for n in names)

    def matches(self, component: Component) -> bool:
        return component.name.upper() in self.names


class ComponentAdapter:
    """Base adapter for standard or custom (``X-*``) components in ``recurring_ical_events``."""

    name: str = "COMPONENT"

    @classmethod
    def component_name(cls) -> str:
        return cls.name.upper()

    @classmethod
    def collect_components_from(
        cls,
        calendar: CompatCalendar,
        *,
        keep_recurrence_attributes: bool = False,
    ) -> list[Component]:
        _ = keep_recurrence_attributes
        return calendar.walk(cls.component_name())

    @classmethod
    def component_bounds(cls, comp: Component) -> tuple[Any, Any]:
        start = comp.decoded("DTSTART") or comp.decoded("DUE") or comp.decoded("DTSTAMP")
        if start is None:
            return None, None
        end = comp.decoded("DTEND") or comp.decoded("DUE")
        if end is None:
            dur = comp.decoded("DURATION")
            if isinstance(dur, datetime.timedelta):
                end = start + dur
            elif isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
                end = start + datetime.timedelta(days=1)
            else:
                end = start
        return start, end


class EventAdapter(ComponentAdapter):
    name = "VEVENT"


class TodoAdapter(ComponentAdapter):
    name = "VTODO"


class JournalAdapter(ComponentAdapter):
    name = "VJOURNAL"


def _normalize_date_arg(arg: DateArgument) -> tuple[Any, Any]:
    if isinstance(arg, int):
        arg = (arg,)
    if isinstance(arg, str):
        s = arg.strip()
        if len(s) == 8 and s.isdigit():
            d = datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            return d, d + datetime.timedelta(days=1)
        if len(s) in (15, 16) and s[8] in ("T", "t"):
            dt = datetime.datetime(
                int(s[0:4]),
                int(s[4:6]),
                int(s[6:8]),
                int(s[9:11]),
                int(s[11:13]),
                int(s[13:15]),
                tzinfo=datetime.timezone.utc if s.endswith(("Z", "z")) else None,
            )
            return dt, dt
        raise ValueError(f"invalid date argument string: {arg!r}")
    if isinstance(arg, tuple):
        n = len(arg)
        if n == 1:
            y = arg[0]
            return datetime.date(y, 1, 1), datetime.date(y + 1, 1, 1)
        if n == 2:
            y, m = arg
            start = datetime.date(y, m, 1)
            next_m = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
            return start, next_m
        if n == 3:
            d = datetime.date(arg[0], arg[1], arg[2])
            return d, d + datetime.timedelta(days=1)
        if n == 4:
            dt = datetime.datetime(arg[0], arg[1], arg[2], arg[3], 0, 0)
            return dt, dt + datetime.timedelta(hours=1)
        if n == 5:
            dt = datetime.datetime(arg[0], arg[1], arg[2], arg[3], arg[4], 0)
            return dt, dt + datetime.timedelta(minutes=1)
        if n == 6:
            dt = datetime.datetime(arg[0], arg[1], arg[2], arg[3], arg[4], arg[5])
            return dt, dt + datetime.timedelta(seconds=1)
        raise ValueError(f"invalid date tuple: {arg!r}")
    if isinstance(arg, datetime.datetime):
        return arg, arg
    if isinstance(arg, datetime.date):
        return arg, arg
    raise TypeError(f"unsupported date argument type: {type(arg).__name__}")


def _to_point(arg: DateArgument) -> Any:
    if isinstance(arg, datetime.datetime):
        return arg
    if isinstance(arg, datetime.date):
        return arg
    s, _ = _normalize_date_arg(arg)
    return s


def _as_cmp_utc(val: Any) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=datetime.timezone.utc)
        return val.astimezone(datetime.timezone.utc)
    if isinstance(val, datetime.date):
        return datetime.datetime.combine(val, datetime.time.min, tzinfo=datetime.timezone.utc)
    raise TypeError(f"cannot compare {type(val).__name__}")


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


def _encode_cursor(occ: _ox.Occurrence) -> str:
    payload = json.dumps(
        [occ.start.isoformat(), occ.end.isoformat(), occ.uid or "", occ.name],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(token: str) -> tuple[Any, Any, str, str]:
    raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    s_iso, e_iso, uid, name = json.loads(raw)
    s_dt = (
        datetime.datetime.fromisoformat(s_iso)
        if "T" in s_iso
        else datetime.date.fromisoformat(s_iso)
    )
    e_dt = (
        datetime.datetime.fromisoformat(e_iso)
        if "T" in e_iso
        else datetime.date.fromisoformat(e_iso)
    )
    return s_dt, e_dt, str(uid), str(name)


def _occ_cursor_key(occ: _ox.Occurrence) -> tuple[str, str, str, str]:
    return (occ.start.isoformat(), occ.end.isoformat(), occ.uid or "", occ.name)


class Page:
    """One page in a paginated recurrence query matching ``recurring_ical_events.pages.Page``."""

    def __init__(self, components: list[Component], next_page_id: str = "") -> None:
        self._components = components
        self._next_page_id = next_page_id

    @property
    def components(self) -> list[Component]:
        return self._components

    @property
    def next_page_id(self) -> str:
        return self._next_page_id

    def has_next_page(self) -> bool:
        return self._next_page_id != ""

    def is_last(self) -> bool:
        return self._next_page_id == ""

    def __len__(self) -> int:
        return len(self._components)

    def __bool__(self) -> bool:
        return bool(self._components)

    def __iter__(self) -> Iterator[Component]:
        return iter(self._components)


class Pages:
    """Lazy paginated iterator yielding :class:`Page` objects with cursor ``next_page_id``."""

    def __init__(
        self,
        occ_iter: Iterator[_ox.Occurrence],
        size: int,
        *,
        latest_start: Optional[DateArgument] = None,
        keep_recurrence_attributes: bool = False,
        cursor_token: str = "",
    ) -> None:
        if size <= 0:
            raise ValueError(f"A page must have at least one component, not {size}.")
        self._iter = occ_iter
        self._size = size
        self._latest_start = _to_point(latest_start) if latest_start is not None else None
        self._keep_recurrence = keep_recurrence_attributes
        self._next_occ: Optional[_ox.Occurrence] = None

        cursor_key: Optional[tuple[str, str, str, str]] = None
        if cursor_token:
            cs, ce, cuid, cname = _decode_cursor(cursor_token)
            cursor_key = (cs.isoformat(), ce.isoformat(), cuid, cname)

        for occ in self._iter:
            if cursor_key is not None and _occ_cursor_key(occ) < cursor_key:
                continue
            if self._before_or_at_latest(occ):
                self._next_occ = occ
            break

    def _before_or_at_latest(self, occ: _ox.Occurrence) -> bool:
        if self._latest_start is None:
            return True
        s = occ.start
        ls = self._latest_start
        if isinstance(s, datetime.datetime) and not isinstance(ls, datetime.datetime):
            ls = datetime.datetime.combine(ls, datetime.time.max, tzinfo=s.tzinfo)
        elif not isinstance(s, datetime.datetime) and isinstance(ls, datetime.datetime):
            s = datetime.datetime.combine(s, datetime.time.min, tzinfo=ls.tzinfo)
        return s <= ls  # type: ignore[operator]

    @property
    def size(self) -> int:
        return self._size

    def generate_next_page(self) -> Page:
        for page in self:
            return page
        return Page([])

    def __iter__(self) -> Pages:
        return self

    def __next__(self) -> Page:
        if self._next_occ is None:
            raise StopIteration
        batch = [self._next_occ]
        next_peek: Optional[_ox.Occurrence] = None
        for occ in self._iter:
            if not self._before_or_at_latest(occ):
                break
            if len(batch) < self._size:
                batch.append(occ)
            else:
                next_peek = occ
                break
        self._next_occ = next_peek
        token = _encode_cursor(next_peek) if next_peek is not None else ""
        comps = [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in batch
        ]
        return Page(comps, next_page_id=token)


def _split_component_selectors(
    components: Sequence[Any],
) -> tuple[list[str], list[type[ComponentAdapter]]]:
    names: list[str] = []
    custom: list[type[ComponentAdapter]] = []
    for item in components:
        if isinstance(item, ComponentsWithName) or isinstance(item, SelectComponents):
            names.extend(item.names)
        elif isinstance(item, type) and issubclass(item, ComponentAdapter):
            cname = item.component_name()
            if cname in AllKnownComponents:
                names.append(cname)
            else:
                custom.append(item)
        else:
            names.append(str(item))
    return names, custom


class UnfoldableCalendar:
    """Drop-in replacement for ``recurring_ical_events.CalendarQuery``."""

    def __init__(
        self,
        rust_cal: _ox.Calendar,
        *,
        keep_recurrence_attributes: bool = False,
        components: Sequence[Any] = ("VEVENT",),
        skip_bad_series: bool = False,
        max_iterations: Optional[int] = None,
        max_periods: Optional[int] = None,
    ) -> None:
        self._rust_cal = rust_cal
        self._keep_recurrence = keep_recurrence_attributes
        self._components, self._custom_adapters = _split_component_selectors(components)
        self._skip_bad_series = skip_bad_series
        self._max_iterations = max_iterations
        self._max_periods = max_periods
        try:
            self._query = rust_cal.query(
                components=self._components,
                skip_bad_series=skip_bad_series,
                max_iterations=max_iterations,
                max_periods=max_periods,
            )
        except ValueError as exc:
            raise BadRuleStringFormat(str(exc)) from exc

    def _query_for(self, bound: Any) -> _ox.CalendarQuery:
        if self._rust_cal.default_timezone is None:
            inferred = _infer_tz_name(bound)
            if inferred and inferred != "UTC":
                return self._rust_cal.query(
                    components=self._components,
                    skip_bad_series=self._skip_bad_series,
                    default_tz=inferred,
                    max_iterations=self._max_iterations,
                    max_periods=self._max_periods,
                )
        return self._query

    def _custom_components_in(self, win_start: Any, win_end: Any) -> list[Component]:
        if not self._custom_adapters:
            return []
        compat_cal = CompatCalendar(_rust_cal=self._rust_cal)
        ws = _as_cmp_utc(win_start)
        if isinstance(win_end, datetime.date) and not isinstance(win_end, datetime.datetime):
            we = datetime.datetime.combine(win_end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc)
        else:
            we = _as_cmp_utc(win_end)
        matched: list[Component] = []
        for adapter in self._custom_adapters:
            for comp in adapter.collect_components_from(
                compat_cal, keep_recurrence_attributes=self._keep_recurrence
            ):
                cs, ce = adapter.component_bounds(comp)
                if cs is None:
                    continue
                su = _as_cmp_utc(cs)
                eu = _as_cmp_utc(ce)
                overlaps = (su < we and eu > ws) if eu > su else (ws <= su < we)
                if overlaps:
                    matched.append(comp)
        return matched

    @property
    def suppressed_errors(self) -> list[str]:
        return self._query.suppressed_errors

    @staticmethod
    def to_datetime(date: DateArgument) -> Any:
        return _to_point(date)

    def at(self, a_date: DateArgument) -> list[Component]:
        s_bound, e_bound = _normalize_date_arg(a_date)
        if s_bound == e_bound and not isinstance(a_date, tuple):
            occs = self._query_for(s_bound).at(s_bound)
            custom = self._custom_components_in(s_bound, e_bound + datetime.timedelta(seconds=1) if isinstance(e_bound, datetime.datetime) else e_bound)
        elif isinstance(a_date, (int, str, tuple)):
            if isinstance(e_bound, datetime.datetime):
                occs = self._query_for(s_bound).between(s_bound, e_bound)
                custom = self._custom_components_in(s_bound, e_bound)
            else:
                end_q = e_bound - datetime.timedelta(days=1)
                occs = self._query_for(s_bound).between(s_bound, end_q)
                custom = self._custom_components_in(s_bound, end_q)
        else:
            occs = self._query_for(s_bound).at(s_bound)
            custom = self._custom_components_in(s_bound, s_bound)
        res = [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in occs
        ]
        res.extend(custom)
        return res

    def between(
        self,
        start: DateArgument,
        stop: Union[DateArgument, datetime.timedelta],
    ) -> list[Component]:
        s_val = _to_point(start)
        if isinstance(stop, datetime.timedelta):
            if isinstance(s_val, datetime.datetime):
                e_val: Any = s_val + stop
            else:
                e_val = datetime.datetime.combine(s_val, datetime.time.min) + stop
            if stop < datetime.timedelta(0):
                raise PeriodEndBeforeStart("between() stop duration must be non-negative")
        else:
            e_val = _to_point(stop)
        occs = self._query_for(s_val).between(s_val, e_val)
        res = [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in occs
        ]
        res.extend(self._custom_components_in(s_val, e_val))
        return res

    def after(self, earliest_end: DateArgument) -> Iterator[Component]:
        pt = _to_point(earliest_end)
        for occ in self._query_for(pt).after(pt):
            yield _wrap_rust_component(occ, keep_recurrence=self._keep_recurrence)
        for comp in self._custom_components_in(pt, DATE_MAX):
            yield comp

    def paginate(
        self,
        page_size: int,
        earliest_end: Optional[DateArgument] = None,
        latest_start: Optional[DateArgument] = None,
        next_page_id: str = "",
    ) -> Pages:
        if next_page_id:
            cs, _, _, _ = _decode_cursor(next_page_id)
            anchor = cs
        elif earliest_end is not None:
            anchor = _to_point(earliest_end)
        else:
            anchor = datetime.date(1900, 1, 1)
        q = self._query_for(anchor)
        return Pages(
            q.after(anchor),
            page_size,
            latest_start=latest_start,
            keep_recurrence_attributes=self._keep_recurrence,
            cursor_token=next_page_id,
        )

    @property
    def first(self) -> Optional[Component]:
        occ = self._query.first()
        if occ is None:
            return None
        return _wrap_rust_component(occ, keep_recurrence=self._keep_recurrence)

    def next_after(self, earliest_end: DateArgument) -> Optional[Component]:
        pt = _to_point(earliest_end)
        occ = self._query_for(pt).next_after(pt)
        if occ is None:
            return None
        return _wrap_rust_component(occ, keep_recurrence=self._keep_recurrence)

    def all(self) -> list[Component]:
        res = [
            _wrap_rust_component(o, keep_recurrence=self._keep_recurrence)
            for o in self._query.all()
        ]
        res.extend(self._custom_components_in(DATE_MIN, DATE_MAX))
        return res

    def count(
        self,
        start: Optional[DateArgument] = None,
        stop: Optional[Union[DateArgument, datetime.timedelta]] = None,
    ) -> int:
        if start is not None and stop is not None:
            s_val = _to_point(start)
            if isinstance(stop, datetime.timedelta):
                if isinstance(s_val, datetime.datetime):
                    e_val: Any = s_val + stop
                else:
                    e_val = datetime.datetime.combine(s_val, datetime.time.min) + stop
            else:
                e_val = _to_point(stop)
            return self._query_for(s_val).count(s_val, e_val) + len(
                self._custom_components_in(s_val, e_val)
            )
        return self._query.count_all() + len(self._custom_components_in(DATE_MIN, DATE_MAX))

    def busy_intervals(
        self,
        start: DateArgument,
        stop: Union[DateArgument, datetime.timedelta],
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        s_val = _to_point(start)
        e_val = s_val + stop if isinstance(stop, datetime.timedelta) else _to_point(stop)
        return self._query_for(s_val).busy_intervals(s_val, e_val)

    def free_intervals(
        self,
        start: DateArgument,
        stop: Union[DateArgument, datetime.timedelta],
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        s_val = _to_point(start)
        e_val = s_val + stop if isinstance(stop, datetime.timedelta) else _to_point(stop)
        return self._query_for(s_val).free_intervals(s_val, e_val)


CalendarQuery = UnfoldableCalendar


def _to_rust_calendar(cal: Any) -> _ox.Calendar:
    if isinstance(cal, _ox.Calendar):
        return cal
    if isinstance(cal, CompatCalendar):
        return cal._as_rust_calendar()
    if hasattr(cal, "to_ical"):
        return _ox.Calendar.from_ical(cal.to_ical())
    raise TypeError("expected icaloxide.Calendar or object with .to_ical()")


def _merge_rust_calendars(cals: Sequence[Any]) -> _ox.Calendar:
    merged = CompatCalendar.new()
    for src in cals:
        rc = _to_rust_calendar(src)
        compat = CompatCalendar(_rust_cal=rc)
        for sub in compat.subcomponents:
            merged.add_component(sub)
    return merged._as_rust_calendar()


def of(
    a_calendar: Any,
    keep_recurrence_attributes: bool = False,
    components: Sequence[Any] = ("VEVENT",),
    skip_bad_series: bool = False,
    calendar_query: type[UnfoldableCalendar] = UnfoldableCalendar,
    *,
    max_iterations: Optional[int] = None,
    max_periods: Optional[int] = None,
) -> UnfoldableCalendar:
    """Wrap one or multiple calendars into a fast Rust recurrence query."""
    if isinstance(a_calendar, (list, tuple)):
        if len(a_calendar) == 1:
            rust_cal = _to_rust_calendar(a_calendar[0])
        elif len(a_calendar) == 0:
            rust_cal = _ox.Calendar.new()
        else:
            rust_cal = _merge_rust_calendars(a_calendar)
    else:
        rust_cal = _to_rust_calendar(a_calendar)
    return calendar_query(
        rust_cal,
        keep_recurrence_attributes=keep_recurrence_attributes,
        components=components,
        skip_bad_series=skip_bad_series,
        max_iterations=max_iterations,
        max_periods=max_periods,
    )


__all__ = [
    "AllKnownComponents",
    "CalendarQuery",
    "ComponentAdapter",
    "ComponentsWithName",
    "DATE_MAX",
    "DATE_MAX_DT",
    "DATE_MIN",
    "DATE_MIN_DT",
    "EventAdapter",
    "JournalAdapter",
    "OccurrenceID",
    "Page",
    "Pages",
    "SelectComponents",
    "TodoAdapter",
    "UnfoldableCalendar",
    "of",
]
