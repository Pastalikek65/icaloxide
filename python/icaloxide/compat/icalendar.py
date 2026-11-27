"""Drop-in compatibility layer mimicking Python's ``icalendar`` package."""

from __future__ import annotations

import datetime
from typing import Any, Iterator, Optional, Union

import icaloxide as _ox


def _escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> bytes:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return raw + b"\r\n"
    out = bytearray()
    start = 0
    first = True
    while start < len(line):
        budget = 75 if first else 74
        chunk = 0
        for ch in line[start:]:
            clen = len(ch.encode("utf-8"))
            if chunk > 0 and chunk + clen > budget:
                break
            chunk += clen
        if not first:
            out.extend(b"\r\n ")
        # Walk characters fitting within chunk bytes
        acc = 0
        end_idx = start
        for ch in line[start:]:
            clen = len(ch.encode("utf-8"))
            if acc + clen > chunk:
                break
            acc += clen
            end_idx += 1
        out.extend(line[start:end_idx].encode("utf-8"))
        start = end_idx
        first = False
    out.extend(b"\r\n")
    return bytes(out)


def _format_param_val(val: str) -> str:
    if any(ch in val for ch in (":", ";", ",", '"', "\n", "^")):
        escaped = val.replace("^", "^^").replace("\n", "^n").replace('"', "^'")
        return f'"{escaped}"'
    return val


def _format_temporal(val: Any, params: dict[str, Any]) -> str:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is not None:
            offset = val.utcoffset()
            tzname = val.tzinfo.tzname(val) if hasattr(val.tzinfo, "tzname") else None
            if offset == datetime.timedelta(0) and (
                tzname in ("UTC", "Z", None) or "TZID" not in params
            ):
                return val.strftime("%Y%m%dT%H%M%SZ")
            if "TZID" not in params:
                key = getattr(val.tzinfo, "key", None) or tzname
                if key:
                    params["TZID"] = key
        return val.strftime("%Y%m%dT%H%M%S")
    if isinstance(val, datetime.date):
        params.setdefault("VALUE", "DATE")
        return val.strftime("%Y%m%d")
    if isinstance(val, datetime.timedelta):
        total = int(val.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        parts = [sign, "P"]
        if days:
            parts.append(f"{days}D")
        if hours or mins or secs or not days:
            parts.append("T")
            if hours:
                parts.append(f"{hours}H")
            if mins:
                parts.append(f"{mins}M")
            if secs or (not days and not hours and not mins):
                parts.append(f"{secs}S")
        return "".join(parts)
    if isinstance(val, datetime.time):
        return val.strftime("%H%M%S")
    return str(val)


class vDDDTypes:
    """Wrapper matching ``icalendar.prop.vDDDTypes`` with ``.dt`` and ``.params``."""

    def __init__(
        self,
        dt: Union[datetime.date, datetime.datetime, datetime.timedelta, datetime.time, Any],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.dt = dt
        self.params: dict[str, Any] = dict(params) if params else {}

    def to_ical(self) -> bytes:
        local_params = dict(self.params)
        return _format_temporal(self.dt, local_params).encode("utf-8")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, vDDDTypes):
            return self.dt == other.dt
        return self.dt == other

    def __repr__(self) -> str:
        return f"vDDDTypes({self.dt!r})"


class vText(str):
    """String subclass matching ``icalendar.prop.vText``."""

    params: dict[str, Any]

    def __new__(cls, value: Any, params: Optional[dict[str, Any]] = None) -> vText:
        obj = super().__new__(cls, str(value))
        obj.params = dict(params) if params else {}
        return obj

    def to_ical(self) -> bytes:
        return _escape_text(str(self)).encode("utf-8")


_TEMPORAL_KEYS = frozenset(
    {
        "DTSTART",
        "DTEND",
        "DUE",
        "DTSTAMP",
        "CREATED",
        "LAST-MODIFIED",
        "COMPLETED",
        "RECURRENCE-ID",
        "DURATION",
        "TRIGGER",
        "RDATE",
        "EXDATE",
    }
)


class Component:
    """Compatibility wrapper supporting both lazy Rust proxy reads and mutable building."""

    name: str = "COMPONENT"

    def __init__(
        self,
        *,
        _rust_comp: Optional[Union[_ox.Component, _ox.Occurrence]] = None,
        _rust_cal: Optional[_ox.Calendar] = None,
    ) -> None:
        self._rust_comp = _rust_comp
        self._rust_cal = _rust_cal
        self._props: list[tuple[str, Any, dict[str, Any]]] = []
        self._subcomponents: list[Component] = []
        self._materialized = _rust_comp is None and _rust_cal is None
        if _rust_comp is not None:
            self.name = _rust_comp.name
        elif _rust_cal is not None:
            self.name = "VCALENDAR"

    def _ensure_materialized(self) -> None:
        if self._materialized:
            return
        self._materialized = True
        if self._rust_cal is not None:
            self._props = [("VERSION", "2.0", {})]
            if self._rust_cal.prodid:
                self._props.append(("PRODID", self._rust_cal.prodid, {}))
            if self._rust_cal.name:
                self._props.append(("NAME", self._rust_cal.name, {}))
            if self._rust_cal.default_timezone:
                self._props.append(("X-WR-TIMEZONE", self._rust_cal.default_timezone, {}))
            self._subcomponents = [
                _wrap_rust_component(c) for c in self._rust_cal.subcomponents
            ]
            return

        if isinstance(self._rust_comp, _ox.Occurrence):
            comp = self._rust_comp.component
            end_key = "DUE" if self._rust_comp.name == "VTODO" else "DTEND"
            for pname in comp.property_names():
                up = pname.upper()
                if up in ("DTSTART", "DTEND", "DUE", "DURATION"):
                    continue
                val = comp.get(pname)
                self._props.append((up, val, comp.params(pname)))
            self._props.append(("DTSTART", self._rust_comp.start, {}))
            self._props.append((end_key, self._rust_comp.end, {}))
            self._subcomponents = [_wrap_rust_component(c) for c in comp.subcomponents]
        elif self._rust_comp is not None:
            for pname in self._rust_comp.property_names():
                vals = self._rust_comp.get_all(pname)
                params = self._rust_comp.params(pname)
                for val in vals:
                    self._props.append((pname.upper(), val, dict(params)))
            self._subcomponents = [
                _wrap_rust_component(c) for c in self._rust_comp.subcomponents
            ]

    def _wrap_prop(self, key_upper: str, val: Any, params: dict[str, Any]) -> Any:
        if key_upper in _TEMPORAL_KEYS and isinstance(
            val, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)
        ):
            return vDDDTypes(val, params)
        if key_upper in _TEMPORAL_KEYS and isinstance(val, list):
            return [
                vDDDTypes(item, params)
                if isinstance(
                    item, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)
                )
                else item
                for item in val
            ]
        return val

    def decoded(self, name: str, default: Any = None) -> Any:
        key_upper = name.upper()
        if not self._materialized:
            if self._rust_comp is not None:
                if key_upper in self._rust_comp:
                    return self._rust_comp[key_upper]
                return default
            if self._rust_cal is not None:
                if key_upper == "NAME":
                    return self._rust_cal.name if self._rust_cal.name is not None else default
                if key_upper == "PRODID":
                    return self._rust_cal.prodid if self._rust_cal.prodid is not None else default
                if key_upper == "X-WR-TIMEZONE":
                    return (
                        self._rust_cal.default_timezone
                        if self._rust_cal.default_timezone is not None
                        else default
                    )
        self._ensure_materialized()
        matches = [val for k, val, _ in self._props if k == key_upper]
        if not matches:
            return default
        raw = matches[0] if len(matches) == 1 else matches
        if isinstance(raw, vDDDTypes):
            return raw.dt
        return raw

    def get(self, name: str, default: Any = None) -> Any:
        key_upper = name.upper()
        if not self._materialized and self._rust_comp is not None:
            if key_upper not in self._rust_comp:
                return default
            val = self._rust_comp[key_upper]
            if isinstance(self._rust_comp, _ox.Occurrence):
                occ_end_key = "DUE" if self._rust_comp.name == "VTODO" else "DTEND"
                params = (
                    {}
                    if key_upper in ("DTSTART", occ_end_key)
                    else self._rust_comp.component.params(key_upper)
                )
            else:
                params = self._rust_comp.params(key_upper)
            return self._wrap_prop(key_upper, val, params)

        self._ensure_materialized()
        matches = [(val, params) for k, val, params in self._props if k == key_upper]
        if not matches:
            return default
        if len(matches) == 1:
            val, params = matches[0]
            return self._wrap_prop(key_upper, val, params)
        return [self._wrap_prop(key_upper, val, params) for val, params in matches]

    def __getitem__(self, name: str) -> Any:
        sentinel = object()
        res = self.get(name, sentinel)
        if res is sentinel:
            raise KeyError(name)
        return res

    def __setitem__(self, name: str, value: Any) -> None:
        self._ensure_materialized()
        key_upper = name.upper()
        self._props = [(k, v, p) for k, v, p in self._props if k != key_upper]
        self._props.append((key_upper, value, {}))

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        key_upper = name.upper()
        if not self._materialized and self._rust_comp is not None:
            return key_upper in self._rust_comp
        self._ensure_materialized()
        return any(k == key_upper for k, _, _ in self._props)

    def add(
        self,
        name: str,
        value: Any,
        parameters: Optional[dict[str, Any]] = None,
        encode: int = 1,
    ) -> None:
        _ = encode
        self._ensure_materialized()
        params = dict(parameters) if parameters else {}
        self._props.append((name.upper(), value, params))

    def add_component(self, component: Component) -> None:
        self._ensure_materialized()
        self._subcomponents.append(component)

    @property
    def subcomponents(self) -> list[Component]:
        if not self._materialized:
            if self._rust_cal is not None:
                return [_wrap_rust_component(c) for c in self._rust_cal.subcomponents]
            if isinstance(self._rust_comp, _ox.Component):
                return [_wrap_rust_component(c) for c in self._rust_comp.subcomponents]
            if isinstance(self._rust_comp, _ox.Occurrence):
                return [_wrap_rust_component(c) for c in self._rust_comp.component.subcomponents]
        self._ensure_materialized()
        return list(self._subcomponents)

    def walk(self, name: Optional[str] = None) -> list[Component]:
        target = name.upper() if name else None
        out: list[Component] = []
        if target is None or self.name == target:
            out.append(self)
        for sub in self.subcomponents:
            out.extend(sub.walk(target))
        return out

    def to_ical(self) -> bytes:
        if not self._materialized:
            if self._rust_cal is not None:
                return self._rust_cal.to_ical()
            if self._rust_comp is not None:
                return self._rust_comp.to_ical()

        buf = bytearray()
        buf.extend(_fold_line(f"BEGIN:{self.name}"))
        for key, val, raw_params in self._props:
            params = dict(raw_params)
            val_str = self._serialize_value(key, val, params)
            param_str = ""
            for pk, pv in params.items():
                if isinstance(pv, (list, tuple)):
                    joined = ",".join(_format_param_val(str(x)) for x in pv)
                else:
                    joined = _format_param_val(str(pv))
                param_str += f";{pk.upper()}={joined}"
            buf.extend(_fold_line(f"{key}{param_str}:{val_str}"))
        for sub in self._subcomponents:
            buf.extend(sub.to_ical())
        buf.extend(_fold_line(f"END:{self.name}"))
        return bytes(buf)

    @staticmethod
    def _serialize_value(key: str, val: Any, params: dict[str, Any]) -> str:
        if isinstance(val, vDDDTypes):
            for pk, pv in val.params.items():
                params.setdefault(pk, pv)
            return _format_temporal(val.dt, params)
        if isinstance(val, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)):
            return _format_temporal(val, params)
        if isinstance(val, list):
            if key in ("CATEGORIES", "RESOURCES"):
                return ",".join(_escape_text(str(x)) for x in val)
            return ",".join(
                _format_temporal(x.dt if isinstance(x, vDDDTypes) else x, params)
                if isinstance(
                    x.dt if isinstance(x, vDDDTypes) else x,
                    (datetime.date, datetime.datetime, datetime.timedelta),
                )
                else str(x)
                for x in val
            )
        if isinstance(val, bool):
            return "TRUE" if val else "FALSE"
        if isinstance(val, tuple) and key == "GEO" and len(val) == 2:
            return f"{val[0]};{val[1]}"
        if key in ("RRULE", "ATTACH", "URL", "UID", "ATTENDEE", "ORGANIZER"):
            return str(val)
        return _escape_text(str(val))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name!r})"


class Event(Component):
    name = "VEVENT"


class Todo(Component):
    name = "VTODO"


class Journal(Component):
    name = "VJOURNAL"


class Alarm(Component):
    name = "VALARM"


class FreeBusy(Component):
    name = "VFREEBUSY"


class Timezone(Component):
    name = "VTIMEZONE"


class TimezoneStandard(Component):
    name = "STANDARD"


class TimezoneDaylight(Component):
    name = "DAYLIGHT"


_COMPONENT_CLASS_MAP: dict[str, type[Component]] = {
    "VCALENDAR": Component,
    "VEVENT": Event,
    "VTODO": Todo,
    "VJOURNAL": Journal,
    "VALARM": Alarm,
    "VFREEBUSY": FreeBusy,
    "VTIMEZONE": Timezone,
    "STANDARD": TimezoneStandard,
    "DAYLIGHT": TimezoneDaylight,
}


def _wrap_rust_component(comp: Union[_ox.Component, _ox.Occurrence]) -> Component:
    cls = _COMPONENT_CLASS_MAP.get(comp.name, Component)
    return cls(_rust_comp=comp)


class Calendar(Component):
    """Drop-in replacement for ``icalendar.Calendar`` backed by Rust."""

    name = "VCALENDAR"

    @classmethod
    def from_ical(
        cls, st: _ox.InputData, multiple: bool = False, *, strict: bool = False
    ) -> Union[Calendar, list[Calendar]]:
        if multiple:
            cals = _ox.Calendar.from_ical_multiple(st, strict=strict)
            return [cls(_rust_cal=c) for c in cals]
        cal = _ox.Calendar.from_ical(st, strict=strict)
        return cls(_rust_cal=cal)

    @classmethod
    def new(
        cls,
        *,
        prodid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Calendar:
        return cls(_rust_cal=_ox.Calendar.new(prodid=prodid, name=name))

    @property
    def events(self) -> list[Event]:
        if not self._materialized and self._rust_cal is not None:
            return [Event(_rust_comp=e) for e in self._rust_cal.events]
        return [c for c in self.subcomponents if c.name == "VEVENT"]  # type: ignore[misc]

    @property
    def todos(self) -> list[Todo]:
        if not self._materialized and self._rust_cal is not None:
            return [Todo(_rust_comp=t) for t in self._rust_cal.todos]
        return [c for c in self.subcomponents if c.name == "VTODO"]  # type: ignore[misc]

    @property
    def journals(self) -> list[Journal]:
        if not self._materialized and self._rust_cal is not None:
            return [Journal(_rust_comp=j) for j in self._rust_cal.journals]
        return [c for c in self.subcomponents if c.name == "VJOURNAL"]  # type: ignore[misc]

    @property
    def timezones(self) -> list[Timezone]:
        if not self._materialized and self._rust_cal is not None:
            return [Timezone(_rust_comp=tz) for tz in self._rust_cal.timezones]
        return [c for c in self.subcomponents if c.name == "VTIMEZONE"]  # type: ignore[misc]

    def add_missing_timezones(self) -> Calendar:
        if not self._materialized and self._rust_cal is not None:
            return Calendar(_rust_cal=self._rust_cal.add_missing_timezones())
        raw = self.to_ical()
        enriched = _ox.Calendar.from_ical(raw).add_missing_timezones()
        return Calendar(_rust_cal=enriched)

    def _as_rust_calendar(self) -> _ox.Calendar:
        if not self._materialized and self._rust_cal is not None:
            return self._rust_cal
        return _ox.Calendar.from_ical(self.to_ical())

    def __iter__(self) -> Iterator[Component]:
        return iter(self.subcomponents)


__all__ = [
    "Alarm",
    "Calendar",
    "Component",
    "Event",
    "FreeBusy",
    "Journal",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "Todo",
    "vDDDTypes",
    "vText",
]
