"""Drop-in compatibility layer mimicking Python's ``icalendar`` package (v7.x)."""

from __future__ import annotations

import bisect
import copy
import datetime
import json
from typing import Any, Iterable, Iterator, Optional, Sequence, Union

import icaloxide as _ox
from icaloxide.compat.xcal import component_to_xcal, xcal_to_jcal


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
        acc = 0
        end_idx = start
        for ch in line[start:]:
            clen = len(ch.encode("utf-8"))
            if acc > 0 and acc + clen > budget:
                break
            acc += clen
            end_idx += 1
        if not first:
            out.extend(b"\r\n ")
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
            # Fix #2020: never emit TZID alongside UTC 'Z'
            if offset == datetime.timedelta(0) and (
                tzname in ("UTC", "Z", "Etc/UTC", None) or "TZID" not in params
            ):
                params.pop("TZID", None)
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


class Parameters(dict):  # type: ignore[type-arg]
    """Case-insensitive parameter mapping matching ``icalendar.Parameters``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        data = dict(*args, **kwargs)
        for k, v in data.items():
            self[k] = v

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(str(key).upper(), value)

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(str(key).upper())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.upper())

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(str(key).upper(), default)

    def has_key(self, key: str) -> bool:
        return key in self

    def sorted_keys(self) -> list[str]:
        return sorted(self.keys())

    def sorted_items(self) -> list[tuple[str, Any]]:
        return sorted(self.items(), key=lambda kv: kv[0])

    def tzid(self) -> Optional[str]:
        val = self.get("TZID")
        if isinstance(val, (list, tuple)) and val:
            return str(val[0])
        return str(val) if val is not None else None

    def is_utc(self) -> bool:
        t = self.tzid()
        return t is not None and t.upper() in ("UTC", "ETC/UTC", "Z")

    def value(self) -> Optional[str]:
        val = self.get("VALUE")
        if isinstance(val, (list, tuple)) and val:
            return str(val[0])
        return str(val) if val is not None else None

    def to_jcal(self, exclude_utc_tzid: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.items():
            kl = str(k).lower()
            if kl == "value":
                continue
            if exclude_utc_tzid and kl == "tzid":
                continue
            out[kl] = v[0] if isinstance(v, (list, tuple)) and len(v) == 1 else v
        return out

    @classmethod
    def from_jcal(cls, jcal_params: dict[str, Any]) -> Parameters:
        return cls({str(k).upper(): v for k, v in dict(jcal_params).items()})

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> Parameters:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        out = cls()
        for part in text.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip().strip('"')
        return out

    def to_ical(self) -> bytes:
        parts = []
        for k, v in sorted(self.items()):
            if isinstance(v, (list, tuple)):
                joined = ",".join(_format_param_val(str(x)) for x in v)
            else:
                joined = _format_param_val(str(v))
            parts.append(f"{k}={joined}")
        return ";".join(parts).encode("utf-8")


class vDDDTypes:
    """Wrapper matching ``icalendar.prop.vDDDTypes`` with ``.dt`` and ``.params``."""

    def __init__(
        self,
        dt: Union[datetime.date, datetime.datetime, datetime.timedelta, datetime.time, Any],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.dt = dt
        self.params: Parameters = Parameters(params) if params else Parameters()

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

    params: Parameters

    def __new__(cls, value: Any, params: Optional[dict[str, Any]] = None) -> vText:
        obj = super().__new__(cls, str(value))
        obj.params = Parameters(params) if params else Parameters()
        return obj

    def to_ical(self) -> bytes:
        return _escape_text(str(self)).encode("utf-8")


class vRecur(dict):  # type: ignore[type-arg]
    """Dictionary representation of RFC 5545 ``RRULE`` matching ``icalendar.vRecur``."""

    params: Parameters

    def __init__(
        self,
        *args: Any,
        params: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.params = Parameters(params) if params else Parameters()
        data = dict(*args, **kwargs)
        for k, v in data.items():
            self[k] = v

    def __setitem__(self, key: str, value: Any) -> None:
        key_up = str(key).upper()
        if isinstance(value, (list, tuple)):
            items = list(value)
        else:
            items = [value]
        normalized: list[Any] = []
        for item in items:
            if key_up in ("INTERVAL", "COUNT", "BYMONTH", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYSETPOS", "BYHOUR", "BYMINUTE", "BYSECOND") and isinstance(item, str):
                try:
                    normalized.append(int(item))
                    continue
                except ValueError:
                    pass
            normalized.append(item)
        super().__setitem__(key_up, normalized)

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> vRecur:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        rec = cls()
        for part in text.strip().split(";"):
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k_up = k.strip().upper()
            vals: list[Any] = []
            for item in v.split(","):
                item_s = item.strip()
                if k_up == "UNTIL":
                    vals.append(_parse_until_str(item_s))
                elif k_up in ("INTERVAL", "COUNT", "BYMONTH", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYSETPOS", "BYHOUR", "BYMINUTE", "BYSECOND"):
                    try:
                        vals.append(int(item_s))
                    except ValueError:
                        vals.append(item_s)
                else:
                    vals.append(item_s)
            super(vRecur, rec).__setitem__(k_up, vals)
        return rec

    def to_ical(self) -> bytes:
        parts: list[str] = []
        for k, vals in self.items():
            rendered: list[str] = []
            for v in vals if isinstance(vals, (list, tuple)) else [vals]:
                if isinstance(v, (datetime.datetime, datetime.date)):
                    rendered.append(_format_temporal(v, {}))
                else:
                    rendered.append(str(v))
            parts.append(f"{k}={','.join(rendered)}")
        return ";".join(parts).encode("utf-8")

    def __str__(self) -> str:
        return self.to_ical().decode("utf-8")


def _parse_until_str(raw: str) -> Any:
    if len(raw) == 8 and raw.isdigit():
        return datetime.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    if len(raw) in (15, 16) and raw[8] in ("T", "t"):
        dt = datetime.datetime(
            int(raw[0:4]),
            int(raw[4:6]),
            int(raw[6:8]),
            int(raw[9:11]),
            int(raw[11:13]),
            int(raw[13:15]),
            tzinfo=datetime.timezone.utc if raw.endswith(("Z", "z")) else None,
        )
        return dt
    return raw


class vGeo:
    def __init__(self, geo: tuple[float, float], params: Optional[dict[str, Any]] = None) -> None:
        self.latitude = float(geo[0])
        self.longitude = float(geo[1])
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return f"{self.latitude};{self.longitude}".encode("utf-8")


class vCalAddress(vText):
    pass


class vUri(vText):
    pass


class vBoolean:
    def __init__(self, val: bool, params: Optional[dict[str, Any]] = None) -> None:
        self.val = bool(val)
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return b"TRUE" if self.val else b"FALSE"

    def __bool__(self) -> bool:
        return self.val


class vInt(int):
    params: Parameters

    def __new__(cls, val: Any, params: Optional[dict[str, Any]] = None) -> vInt:
        obj = super().__new__(cls, int(val))
        obj.params = Parameters(params) if params else Parameters()
        return obj

    def to_ical(self) -> bytes:
        return str(int(self)).encode("utf-8")


class vFloat(float):
    params: Parameters

    def __new__(cls, val: Any, params: Optional[dict[str, Any]] = None) -> vFloat:
        obj = super().__new__(cls, float(val))
        obj.params = Parameters(params) if params else Parameters()
        return obj

    def to_ical(self) -> bytes:
        return str(float(self)).encode("utf-8")


class vDuration(vDDDTypes):
    pass


class vPeriod:
    def __init__(
        self,
        per: tuple[datetime.datetime, Union[datetime.datetime, datetime.timedelta]],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.start = per[0]
        self.end_or_dur = per[1]
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        s = _format_temporal(self.start, {})
        e = _format_temporal(self.end_or_dur, {})
        return f"{s}/{e}".encode("utf-8")


class vUTCOffset(vDDDTypes):
    pass


class vCategory:
    def __init__(self, cats: Iterable[str], params: Optional[dict[str, Any]] = None) -> None:
        self.cats = [vText(c) for c in cats]
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return ",".join(_escape_text(str(c)) for c in self.cats).encode("utf-8")


class vBinary:
    def __init__(self, obj: Union[bytes, str], params: Optional[dict[str, Any]] = None) -> None:
        self.obj = obj
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return self.obj if isinstance(self.obj, bytes) else str(self.obj).encode("utf-8")


class vDate(vDDDTypes):
    pass


class vDatetime(vDDDTypes):
    pass


class vTime(vDDDTypes):
    pass


class vDDDLists:
    """List of ``vDDDTypes`` temporals matching ``icalendar.prop.vDDDLists``."""

    def __init__(
        self,
        dt_list: Iterable[Any],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.params = Parameters(params) if params else Parameters()
        self.dts = [x if isinstance(x, vDDDTypes) else vDDDTypes(x, params) for x in dt_list]

    def to_ical(self) -> bytes:
        return b",".join(d.to_ical() for d in self.dts)


class vWeekday(vText):
    pass


class vFrequency(vText):
    pass


class vMonth(vInt):
    pass


class vSkip(vText):
    pass


class vUid(vText):
    pass


class vBroken(vText):
    pass


class vUnknown(vText):
    pass


class vXmlReference(vUri):
    pass


class Conference(vUri):
    """RFC 7986 ``CONFERENCE`` wrapper."""


class Image(vUri):
    """RFC 7986 ``IMAGE`` wrapper."""


def is_utc(dt: Any) -> bool:
    if isinstance(dt, datetime.datetime) and dt.tzinfo is not None:
        return dt.utcoffset() == datetime.timedelta(0)
    return False


def use_zoneinfo() -> None:
    """Compatibility no-op: ``icaloxide`` always uses stdlib ``zoneinfo`` + embedded TZDB."""


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
    """Compatibility wrapper supporting lazy Rust proxies, mutable editing, dict protocol, and jCal."""

    name: str = "COMPONENT"

    def __init__(
        self,
        *,
        _rust_comp: Optional[Union[_ox.Component, _ox.Occurrence]] = None,
        _rust_cal: Optional[_ox.Calendar] = None,
        _keep_recurrence: bool = False,
    ) -> None:
        self._rust_comp = _rust_comp
        self._rust_cal = _rust_cal
        self._keep_recurrence = _keep_recurrence
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
                if not self._keep_recurrence and up in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                    continue
                for val in comp.get_all(pname):
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
        if key_upper == "RRULE":
            if isinstance(val, vRecur):
                return val
            if isinstance(val, dict):
                return vRecur(val, params=params)
            return vRecur.from_ical(str(val))
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
                    val = self._rust_comp[key_upper]
                    if key_upper == "RRULE" and isinstance(val, str):
                        return vRecur.from_ical(val)
                    return val
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
        if key_upper == "RRULE" and isinstance(raw, str):
            return vRecur.from_ical(raw)
        return raw

    def get(self, name: str, default: Any = None) -> Any:
        key_upper = name.upper()
        if not self._materialized and self._rust_comp is not None:
            if isinstance(self._rust_comp, _ox.Occurrence) and not self._keep_recurrence and key_upper in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                return default
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

    def __delitem__(self, name: str) -> None:
        self._ensure_materialized()
        key_upper = name.upper()
        before = len(self._props)
        self._props = [(k, v, p) for k, v, p in self._props if k != key_upper]
        if len(self._props) == before:
            raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        key_upper = name.upper()
        if not self._materialized and self._rust_comp is not None:
            if isinstance(self._rust_comp, _ox.Occurrence) and not self._keep_recurrence and key_upper in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                return False
            return key_upper in self._rust_comp
        self._ensure_materialized()
        return any(k == key_upper for k, _, _ in self._props)

    def has_key(self, name: str) -> bool:
        return name in self

    def keys(self) -> list[str]:
        self._ensure_materialized()
        seen: list[str] = []
        for k, _, _ in self._props:
            if k not in seen:
                seen.append(k)
        return seen

    def values(self) -> list[Any]:
        return [self.get(k) for k in self.keys()]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, self.get(k)) for k in self.keys()]

    def property_items(self, recursive: bool = True, sorted: bool = True) -> list[tuple[str, Any]]:
        _ = sorted
        out: list[tuple[str, Any]] = [("BEGIN", vText(self.name))]
        for k, v, p in self._raw_props():
            out.append((k, self._wrap_prop(k, v, p)))
        if recursive:
            for sub in self.subcomponents:
                out.extend(sub.property_items(recursive=True, sorted=sorted))
        out.append(("END", vText(self.name)))
        return out

    def _raw_props(self) -> list[tuple[str, Any, dict[str, Any]]]:
        self._ensure_materialized()
        return self._props

    def sorted_keys(self) -> list[str]:
        return sorted(self.keys())

    def sorted_items(self) -> list[tuple[str, Any]]:
        return sorted(self.items(), key=lambda kv: kv[0])

    def pop(self, key: str, default: Any = _ox) -> Any:
        if key in self:
            val = self[key]
            del self[key]
            return val
        if default is not _ox:
            return default
        raise KeyError(key)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        self.add(key, default)
        return self[key]

    def update(self, mapping: Any) -> None:
        items = mapping.items() if hasattr(mapping, "items") else mapping
        for k, v in items:
            self[k] = v

    def clear(self) -> None:
        self._ensure_materialized()
        self._props.clear()
        self._subcomponents.clear()

    def copy(self) -> Component:
        self._ensure_materialized()
        dup = self.__class__()
        dup.name = self.name
        dup._props = copy.deepcopy(self._props)
        dup._subcomponents = [sub.copy() for sub in self._subcomponents]
        return dup

    def __len__(self) -> int:
        return len(self.keys())

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

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
    def uid(self) -> Optional[str]:
        v = self.decoded("UID")
        return str(v) if v is not None else None

    @property
    def created(self) -> Any:
        return self.decoded("CREATED")

    @property
    def last_modified(self) -> Any:
        return self.decoded("LAST-MODIFIED")

    @property
    def links(self) -> list[vUri]:
        val = self.get("LINK")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [v if isinstance(v, vUri) else vUri(v) for v in items]

    @property
    def concepts(self) -> list[vUri]:
        val = self.get("CONCEPT")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [v if isinstance(v, vUri) else vUri(v) for v in items]

    @property
    def refids(self) -> list[str]:
        val = self.decoded("REFID")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [str(v) for v in items]

    @property
    def related_to(self) -> list[str]:
        val = self.decoded("RELATED-TO")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [str(v) for v in items]

    @property
    def comments(self) -> list[str]:
        val = self.decoded("COMMENT")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [str(v) for v in items]

    def is_empty(self) -> bool:
        self._ensure_materialized()
        return not self._props and not self._subcomponents

    def with_uid(self, uid: str) -> list[Component]:
        return [c for c in self.walk() if c.decoded("UID") == uid]

    def stamp(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self["DTSTAMP"] = now

    def content_lines(self) -> list[str]:
        return self.to_ical().decode("utf-8").splitlines()

    def content_line(self, name: str, default: Any = None) -> Any:
        return self.get(name, default)

    @classmethod
    def register(cls, comp_cls: type[Component]) -> type[Component]:
        _COMPONENT_CLASS_MAP[comp_cls.name.upper()] = comp_cls
        return comp_cls

    @classmethod
    def from_ical(
        cls, st: _ox.InputData, multiple: bool = False, *, strict: bool = False
    ) -> Union[Component, list[Component]]:
        if multiple:
            cals = _ox.Calendar.from_ical_multiple(st, strict=strict)
            return [_wrap_rust_component_or_cal(c) for c in cals]
        comp = _ox.parse_component(st, strict=strict)
        return _wrap_rust_component(comp)

    def to_xcal(self) -> str:
        """Serialize this component into RFC 6321 xCal XML."""
        return component_to_xcal(self)

    @classmethod
    def from_xcal(cls, xml_data: Union[str, bytes]) -> Component:
        """Parse RFC 6321 xCal XML into a Component."""
        return cls.from_jcal(xcal_to_jcal(xml_data))

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
            if isinstance(self._rust_comp, _ox.Occurrence):
                return self._rust_comp.to_ical(
                    keep_recurrence_attributes=self._keep_recurrence
                )
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

    def to_jcal(self) -> list[Any]:
        """Serialize this component into RFC 7265 jCal nested list structure."""
        self._ensure_materialized()
        jprops: list[list[Any]] = []
        for key, val, raw_params in self._props:
            jprops.append(_prop_to_jcal(key, val, raw_params))
        jsubs = [sub.to_jcal() for sub in self._subcomponents]
        return [self.name.lower(), jprops, jsubs]

    def to_json(self) -> str:
        """Serialize this component into an RFC 7265 jCal JSON string."""
        return json.dumps(self.to_jcal())

    @classmethod
    def from_jcal(cls, jcal: Union[str, list[Any]]) -> Component:
        """Parse an RFC 7265 jCal JSON string or nested list into a Component."""
        data = json.loads(jcal) if isinstance(jcal, str) else jcal
        if not isinstance(data, list) or len(data) < 3:
            raise ValueError("invalid jCal structure")
        comp_name = str(data[0]).upper()
        target_cls = _COMPONENT_CLASS_MAP.get(comp_name, cls)
        comp = target_cls()
        comp.name = comp_name
        for prop_entry in data[1]:
            if not isinstance(prop_entry, list) or len(prop_entry) < 4:
                continue
            pname = str(prop_entry[0]).upper()
            params = {str(k).upper(): v for k, v in dict(prop_entry[1]).items()}
            jtype = str(prop_entry[2]).lower()
            vals = prop_entry[3:]
            parsed = _jcal_values_to_py(pname, jtype, vals, params)
            comp.add(pname, parsed, parameters=params)
        for sub_data in data[2]:
            comp.add_component(Component.from_jcal(sub_data))
        return comp

    @staticmethod
    def _serialize_value(key: str, val: Any, params: dict[str, Any]) -> str:
        if isinstance(val, vRecur) or (key == "RRULE" and isinstance(val, dict)):
            rec = val if isinstance(val, vRecur) else vRecur(val)
            return rec.to_ical().decode("utf-8")
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
        if hasattr(val, "to_ical") and not isinstance(val, str):
            raw = val.to_ical()
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        if key == "REQUEST-STATUS":
            parts = str(val).split(";")
            return ";".join(_escape_text(p) for p in parts)
        if key in ("RRULE", "ATTACH", "URL", "UID", "ATTENDEE", "ORGANIZER", "LINK", "CONCEPT"):
            return str(val)
        return _escape_text(str(val))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name!r})"


def _normalize_jcal_param(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _prop_to_jcal(key: str, val: Any, raw_params: dict[str, Any]) -> list[Any]:
    params = {
        k.lower(): _normalize_jcal_param(v)
        for k, v in raw_params.items()
        if k.lower() != "value"
    }
    pname = key.lower()
    if isinstance(val, vDDDTypes):
        for pk, pv in val.params.items():
            lp = pk.lower()
            if lp != "value":
                params.setdefault(lp, _normalize_jcal_param(pv))
        val = val.dt

    if isinstance(val, vRecur) or (key == "RRULE" and isinstance(val, dict)):
        rec_dict: dict[str, Any] = {}
        for rk, rvs in val.items():
            items = rvs if isinstance(rvs, (list, tuple)) else [rvs]
            cleaned = [
                _iso_temporal(x) if isinstance(x, (datetime.date, datetime.datetime)) else x
                for x in items
            ]
            rec_dict[rk.lower()] = cleaned[0] if len(cleaned) == 1 else cleaned
        return [pname, params, "recur", rec_dict]
    if key == "RRULE" and isinstance(val, str):
        return _prop_to_jcal(key, vRecur.from_ical(val), raw_params)
    if isinstance(val, datetime.datetime):
        if (
            val.tzinfo is not None
            and val.utcoffset() != datetime.timedelta(0)
            and "tzid" not in params
        ):
            tz_key = getattr(val.tzinfo, "key", None)
            if tz_key:
                params["tzid"] = tz_key
        return [pname, params, "date-time", _iso_temporal(val)]
    if isinstance(val, datetime.date):
        return [pname, params, "date", val.isoformat()]
    if isinstance(val, datetime.time):
        return [pname, params, "time", val.strftime("%H:%M:%S")]
    if isinstance(val, datetime.timedelta):
        return [pname, params, "duration", _format_temporal(val, {})]
    if isinstance(val, bool):
        return [pname, params, "boolean", val]
    if isinstance(val, int):
        return [pname, params, "integer", val]
    if isinstance(val, float):
        return [pname, params, "float", val]
    if isinstance(val, tuple) and key == "GEO" and len(val) == 2:
        return [pname, params, "float", [val[0], val[1]]]
    if isinstance(val, list):
        if val and isinstance(val[0], datetime.datetime):
            return [pname, params, "date-time", *[_iso_temporal(x) for x in val]]
        if val and isinstance(val[0], datetime.date):
            return [pname, params, "date", *[x.isoformat() for x in val]]
        return [pname, params, "text", *[str(x) for x in val]]
    return [pname, params, "text", str(val)]


def _iso_temporal(dt: Union[datetime.date, datetime.datetime]) -> str:
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is not None and dt.utcoffset() == datetime.timedelta(0):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.isoformat()


def _jcal_values_to_py(pname: str, jtype: str, vals: list[Any], params: dict[str, Any]) -> Any:
    if jtype == "recur" and vals and isinstance(vals[0], dict):
        return vRecur(vals[0])
    if jtype == "date":
        parsed = [datetime.date.fromisoformat(str(v)) for v in vals]
        return parsed[0] if len(parsed) == 1 else parsed
    if jtype == "date-time":
        parsed_dt = [_parse_iso_dt(str(v), params) for v in vals]
        return parsed_dt[0] if len(parsed_dt) == 1 else parsed_dt
    if jtype == "time":
        parsed_t = [_parse_iso_time(str(v), params) for v in vals]
        return parsed_t[0] if len(parsed_t) == 1 else parsed_t
    if jtype == "integer":
        return int(vals[0])
    if jtype == "float":
        if pname == "GEO" and isinstance(vals[0], list) and len(vals[0]) == 2:
            return (float(vals[0][0]), float(vals[0][1]))
        return float(vals[0])
    if jtype == "boolean":
        return bool(vals[0])
    if len(vals) > 1 or pname in ("CATEGORIES", "RESOURCES"):
        return [str(v) for v in vals]
    return str(vals[0]) if vals else ""


def _parse_iso_dt(raw: str, params: dict[str, Any]) -> datetime.datetime:
    if raw.endswith("Z"):
        return datetime.datetime.fromisoformat(raw[:-1]).replace(tzinfo=datetime.timezone.utc)
    dt = datetime.datetime.fromisoformat(raw)
    if "TZID" in params and dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            tzid_val = params["TZID"]
            if isinstance(tzid_val, (list, tuple)) and tzid_val:
                tzid_val = tzid_val[0]
            dt = dt.replace(tzinfo=ZoneInfo(str(tzid_val)))
        except Exception:
            pass
    return dt


def _parse_iso_time(raw: str, params: dict[str, Any]) -> datetime.time:
    """Parse jCal ``time`` while preserving optional ``Z`` or ``TZID`` (#2154)."""
    if raw.endswith(("Z", "z")):
        t = datetime.time.fromisoformat(raw[:-1])
        return t.replace(tzinfo=datetime.timezone.utc)
    t = datetime.time.fromisoformat(raw)
    if "TZID" in params and t.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            tzid_val = params["TZID"]
            if isinstance(tzid_val, (list, tuple)) and tzid_val:
                tzid_val = tzid_val[0]
            t = t.replace(tzinfo=ZoneInfo(str(tzid_val)))  # type: ignore[arg-type]
        except Exception:
            pass
    return t


class _SpanMixin(Component):
    """Shared ergonomic properties matching ``icalendar 7.x`` Event/Todo/Journal."""

    @property
    def start(self) -> Any:
        return self.decoded("DTSTART")

    @start.setter
    def start(self, val: Any) -> None:
        self["DTSTART"] = val

    def set_start(self, val: Any, parameters: Optional[dict[str, Any]] = None) -> None:
        self._ensure_materialized()
        self._props = [(k, v, p) for k, v, p in self._props if k != "DTSTART"]
        self.add("DTSTART", val, parameters=parameters)

    @property
    def end(self) -> Any:
        if self.name == "VTODO" and "DUE" in self:
            return self.decoded("DUE")
        if "DTEND" in self:
            return self.decoded("DTEND")
        st = self.start
        dur = self.duration
        if st is not None and isinstance(dur, datetime.timedelta):
            return st + dur
        return st

    @end.setter
    def end(self, val: Any) -> None:
        key = "DUE" if self.name == "VTODO" else "DTEND"
        self[key] = val

    def set_end(self, val: Any, parameters: Optional[dict[str, Any]] = None) -> None:
        self._ensure_materialized()
        key = "DUE" if self.name == "VTODO" else "DTEND"
        self._props = [(k, v, p) for k, v, p in self._props if k != key]
        self.add(key, val, parameters=parameters)

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        if "DURATION" in self:
            return self.decoded("DURATION")
        st = self.decoded("DTSTART")
        end_key = "DUE" if self.name == "VTODO" else "DTEND"
        en = self.decoded(end_key)
        if st is not None and en is not None:
            return en - st
        return None

    def set_duration(self, val: datetime.timedelta) -> None:
        self["DURATION"] = val

    @property
    def summary(self) -> Optional[str]:
        return self.decoded("SUMMARY")

    @property
    def description(self) -> Optional[str]:
        return self.decoded("DESCRIPTION")

    @property
    def location(self) -> Optional[str]:
        return self.decoded("LOCATION")

    @property
    def uid(self) -> Optional[str]:
        return self.decoded("UID")

    @property
    def status(self) -> Optional[str]:
        return self.decoded("STATUS")

    @property
    def priority(self) -> Optional[int]:
        return self.decoded("PRIORITY")

    @property
    def sequence(self) -> int:
        val = self.decoded("SEQUENCE", 0)
        return int(val) if val is not None else 0

    @property
    def categories(self) -> list[str]:
        val = self.decoded("CATEGORIES")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [str(val)]

    @property
    def color(self) -> Optional[str]:
        return self.decoded("COLOR")

    @property
    def organizer(self) -> Optional[str]:
        return self.decoded("ORGANIZER")

    @property
    def attendees(self) -> list[str]:
        val = self.get("ATTENDEE")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [val]

    @property
    def rrules(self) -> list[vRecur]:
        val = self.get("RRULE")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [val]

    @property
    def rdates(self) -> list[Any]:
        val = self.get("RDATE")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [val]

    @property
    def exdates(self) -> list[Any]:
        val = self.get("EXDATE")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [val]

    @property
    def alarms(self) -> list[Alarm]:
        return [c for c in self.subcomponents if c.name == "VALARM"]  # type: ignore[misc]

    @property
    def classification(self) -> Optional[str]:
        return self.decoded("CLASS")

    @property
    def transparency(self) -> Optional[str]:
        return self.decoded("TRANSP")

    @property
    def url(self) -> Optional[str]:
        return self.decoded("URL")

    @property
    def contacts(self) -> list[str]:
        val = self.decoded("CONTACT")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [str(val)]

    @property
    def conferences(self) -> list[Conference]:
        val = self.get("CONFERENCE")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [v if isinstance(v, Conference) else Conference(v) for v in items]

    @property
    def images(self) -> list[Image]:
        val = self.get("IMAGE")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [v if isinstance(v, Image) else Image(v) for v in items]


class Event(_SpanMixin):
    name = "VEVENT"

    @classmethod
    def new(
        cls,
        *,
        summary: Optional[str] = None,
        start: Any = None,
        end: Any = None,
        duration: Optional[datetime.timedelta] = None,
        uid: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
    ) -> Event:
        ev = cls()
        if uid is not None:
            ev.add("UID", uid)
        if summary is not None:
            ev.add("SUMMARY", summary)
        if start is not None:
            ev.add("DTSTART", start)
        if end is not None:
            ev.add("DTEND", end)
        if duration is not None:
            ev.add("DURATION", duration)
        if description is not None:
            ev.add("DESCRIPTION", description)
        if location is not None:
            ev.add("LOCATION", location)
        return ev


class Todo(_SpanMixin):
    name = "VTODO"

    @classmethod
    def new(
        cls,
        *,
        summary: Optional[str] = None,
        start: Any = None,
        due: Any = None,
        uid: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Todo:
        td = cls()
        if uid is not None:
            td.add("UID", uid)
        if summary is not None:
            td.add("SUMMARY", summary)
        if start is not None:
            td.add("DTSTART", start)
        if due is not None:
            td.add("DUE", due)
        if status is not None:
            td.add("STATUS", status)
        return td


class Journal(_SpanMixin):
    name = "VJOURNAL"

    @classmethod
    def new(
        cls,
        *,
        summary: Optional[str] = None,
        start: Any = None,
        uid: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Journal:
        jn = cls()
        if uid is not None:
            jn.add("UID", uid)
        if summary is not None:
            jn.add("SUMMARY", summary)
        if start is not None:
            jn.add("DTSTART", start)
        if description is not None:
            jn.add("DESCRIPTION", description)
        return jn

    @property
    def descriptions(self) -> list[str]:
        val = self.decoded("DESCRIPTION")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [str(val)]


class Alarm(Component):
    name = "VALARM"

    @classmethod
    def new(
        cls,
        *,
        action: str = "DISPLAY",
        trigger: Union[datetime.timedelta, datetime.datetime, None] = None,
        description: Optional[str] = None,
        summary: Optional[str] = None,
        repeat: Optional[int] = None,
        duration: Optional[datetime.timedelta] = None,
    ) -> Alarm:
        al = cls()
        al.add("ACTION", action)
        if trigger is not None:
            al.add("TRIGGER", trigger)
        if description is not None:
            al.add("DESCRIPTION", description)
        if summary is not None:
            al.add("SUMMARY", summary)
        if repeat is not None:
            al.add("REPEAT", repeat)
        if duration is not None:
            al.add("DURATION", duration)
        return al

    @classmethod
    def new_display(
        cls,
        trigger: Union[datetime.timedelta, datetime.datetime],
        description: str = "Reminder",
        *,
        repeat: Optional[int] = None,
        duration: Optional[datetime.timedelta] = None,
    ) -> Alarm:
        al = cls()
        al.add("ACTION", "DISPLAY")
        al.add("TRIGGER", trigger)
        al.add("DESCRIPTION", description)
        if repeat is not None:
            al.add("REPEAT", repeat)
        if duration is not None:
            al.add("DURATION", duration)
        return al

    @classmethod
    def new_audio(
        cls,
        trigger: Union[datetime.timedelta, datetime.datetime],
        *,
        attach: Optional[str] = None,
    ) -> Alarm:
        al = cls()
        al.add("ACTION", "AUDIO")
        al.add("TRIGGER", trigger)
        if attach is not None:
            al.add("ATTACH", attach)
        return al

    @classmethod
    def new_email(
        cls,
        trigger: Union[datetime.timedelta, datetime.datetime],
        summary: str,
        description: str,
        attendees: Sequence[str] = (),
    ) -> Alarm:
        al = cls()
        al.add("ACTION", "EMAIL")
        al.add("TRIGGER", trigger)
        al.add("SUMMARY", summary)
        al.add("DESCRIPTION", description)
        for att in attendees:
            al.add("ATTENDEE", att)
        return al

    @property
    def action(self) -> Optional[str]:
        return self.decoded("ACTION")

    @property
    def trigger(self) -> Any:
        return self.decoded("TRIGGER")

    @property
    def triggers(self) -> list[Any]:
        t = self.trigger
        return [] if t is None else (list(t) if isinstance(t, list) else [t])

    @property
    def description(self) -> Optional[str]:
        return self.decoded("DESCRIPTION")

    @property
    def summary(self) -> Optional[str]:
        return self.decoded("SUMMARY")

    @property
    def acknowledged(self) -> Any:
        return self.decoded("ACKNOWLEDGED")

    @property
    def repeat(self) -> int:
        return int(self.decoded("REPEAT", 0) or 0)

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        return self.decoded("DURATION")


class FreeBusy(_SpanMixin):
    name = "VFREEBUSY"

    @classmethod
    def new(
        cls,
        *,
        start: Any = None,
        end: Any = None,
        uid: Optional[str] = None,
        organizer: Optional[str] = None,
    ) -> FreeBusy:
        fb = cls()
        if uid is not None:
            fb.add("UID", uid)
        if start is not None:
            fb.add("DTSTART", start)
        if end is not None:
            fb.add("DTEND", end)
        if organizer is not None:
            fb.add("ORGANIZER", organizer)
        return fb


class _CompiledVTimezoneTzInfo(datetime.tzinfo):
    """``datetime.tzinfo`` backed by Rust ``CompiledVTimezone`` transitions."""

    def __init__(
        self,
        name: str,
        transitions: list[tuple[int, int, int, bool, Optional[str]]],
    ) -> None:
        self._name = name
        self._transitions = transitions
        self._trans_utc = [t[0] for t in transitions]

    def _resolve(self, dt: Optional[datetime.datetime]) -> tuple[int, int, str]:
        if not self._transitions:
            return 0, 0, self._name
        if dt is None:
            _, _, off_to, is_dst, abbr = self._transitions[0]
            return off_to, (3600 if is_dst else 0), abbr or self._name

        naive_ts = int(dt.replace(tzinfo=None).timestamp()) if dt.year >= 1970 else (
            (dt.date() - datetime.date(1970, 1, 1)).days * 86400
            + dt.hour * 3600
            + dt.minute * 60
            + dt.second
        )
        # Converge local wall-clock timestamp against sorted UTC transitions
        est_off = self._transitions[0][1]
        idx = bisect.bisect_right(self._trans_utc, naive_ts - est_off)
        for _ in range(2):
            chosen = self._transitions[idx - 1] if idx > 0 else self._transitions[0]
            est_off = chosen[2] if idx > 0 else chosen[1]
            idx = bisect.bisect_right(self._trans_utc, naive_ts - est_off)
        if idx == 0:
            _, off_from, _, _, abbr = self._transitions[0]
            return off_from, 0, abbr or self._name
        _, off_from, off_to, is_dst, abbr = self._transitions[idx - 1]
        dst_secs = (off_to - off_from) if is_dst and off_to > off_from else (3600 if is_dst else 0)
        return off_to, dst_secs, abbr or self._name

    def utcoffset(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        off, _, _ = self._resolve(dt)
        return datetime.timedelta(seconds=off)

    def dst(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        _, dst_s, _ = self._resolve(dt)
        return datetime.timedelta(seconds=dst_s)

    def tzname(self, dt: Optional[datetime.datetime]) -> str:
        _, _, abbr = self._resolve(dt)
        return abbr


class Timezone(Component):
    name = "VTIMEZONE"
    DEFAULT_FIRST_DATE = datetime.date(1970, 1, 1)
    DEFAULT_LAST_DATE = datetime.date(2038, 1, 1)

    @property
    def tz_name(self) -> Optional[str]:
        v = self.decoded("TZID")
        return str(v) if v is not None else None

    @property
    def standard(self) -> list[TimezoneStandard]:
        return self.walk("STANDARD")  # type: ignore[return-value]

    @property
    def daylight(self) -> list[TimezoneDaylight]:
        return self.walk("DAYLIGHT")  # type: ignore[return-value]

    @classmethod
    def from_tzid(
        cls,
        tzid: str,
        first_date: datetime.date = DEFAULT_FIRST_DATE,
        last_date: datetime.date = DEFAULT_LAST_DATE,
    ) -> Timezone:
        raw = _ox.vtimezone_for_tzid(tzid, first_date.year, last_date.year)
        if raw is None:
            raise ValueError(f"unknown timezone identifier: {tzid!r}")
        comp = cls.from_ical(raw)
        assert isinstance(comp, Timezone)
        return comp

    @classmethod
    def from_tzinfo(
        cls,
        tzinfo: datetime.tzinfo,
        tzid: Optional[str] = None,
        first_date: datetime.date = DEFAULT_FIRST_DATE,
        last_date: datetime.date = DEFAULT_LAST_DATE,
    ) -> Timezone:
        key = tzid or getattr(tzinfo, "key", None) or getattr(tzinfo, "zone", None)
        if key:
            try:
                return cls.from_tzid(str(key), first_date=first_date, last_date=last_date)
            except ValueError:
                pass
        sample = datetime.datetime(2020, 1, 1)
        off = tzinfo.utcoffset(sample) or datetime.timedelta(0)
        name = str(key or (tzinfo.tzname(sample) if hasattr(tzinfo, "tzname") else None) or "CUSTOM")
        tz_comp = cls()
        tz_comp.add("TZID", name)
        std = TimezoneStandard()
        std.add("DTSTART", datetime.datetime(1970, 1, 1, 0, 0, 0))
        std.add("TZOFFSETFROM", vUTCOffset(off))
        std.add("TZOFFSETTO", vUTCOffset(off))
        tz_comp.add_component(std)
        return tz_comp

    def get_transitions(
        self,
    ) -> tuple[list[datetime.datetime], list[tuple[datetime.timedelta, datetime.timedelta, str]]]:
        zone = self.tz_name or "VTIMEZONE"
        rows = _ox.compile_vtimezone_transitions(self.to_ical())
        times: list[datetime.datetime] = []
        infos: list[tuple[datetime.timedelta, datetime.timedelta, str]] = []
        for utc_ts, off_from, off_to, is_dst, abbr in rows:
            times.append(
                datetime.datetime.fromtimestamp(utc_ts, tz=datetime.timezone.utc).replace(
                    tzinfo=None
                )
            )
            dst_s = (off_to - off_from) if is_dst and off_to > off_from else (3600 if is_dst else 0)
            infos.append(
                (
                    datetime.timedelta(seconds=off_to),
                    datetime.timedelta(seconds=dst_s),
                    abbr or zone,
                )
            )
        return times, infos

    def to_tz(self, tzp: Any = None, lookup_tzid: bool = True) -> datetime.tzinfo:
        _ = tzp
        if lookup_tzid and self.tz_name:
            if self.tz_name.upper() in ("UTC", "ETC/UTC"):
                return datetime.timezone.utc
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(self.tz_name)
            except Exception:
                pass
        rows = _ox.compile_vtimezone_transitions(self.to_ical())
        return _CompiledVTimezoneTzInfo(self.tz_name or "VTIMEZONE", rows)


class TimezoneStandard(Component):
    name = "STANDARD"


class TimezoneDaylight(Component):
    name = "DAYLIGHT"


class Availability(_SpanMixin):
    name = "VAVAILABILITY"

    @classmethod
    def new(
        cls,
        *,
        summary: Optional[str] = None,
        start: Any = None,
        end: Any = None,
        busy_type: Optional[str] = None,
        uid: Optional[str] = None,
    ) -> Availability:
        av = cls()
        if uid is not None:
            av.add("UID", uid)
        if summary is not None:
            av.add("SUMMARY", summary)
        if start is not None:
            av.add("DTSTART", start)
        if end is not None:
            av.add("DTEND", end)
        if busy_type is not None:
            av.add("BUSYTYPE", busy_type)
        return av

    @property
    def available(self) -> list[Available]:
        return [c for c in self.subcomponents if c.name == "AVAILABLE"]  # type: ignore[misc]

    @property
    def busy_type(self) -> Optional[str]:
        return self.decoded("BUSYTYPE")


class Available(_SpanMixin):
    name = "AVAILABLE"

    @classmethod
    def new(
        cls,
        *,
        summary: Optional[str] = None,
        start: Any = None,
        end: Any = None,
        duration: Optional[datetime.timedelta] = None,
        uid: Optional[str] = None,
    ) -> Available:
        av = cls()
        if uid is not None:
            av.add("UID", uid)
        if summary is not None:
            av.add("SUMMARY", summary)
        if start is not None:
            av.add("DTSTART", start)
        if end is not None:
            av.add("DTEND", end)
        if duration is not None:
            av.add("DURATION", duration)
        return av


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
    "VAVAILABILITY": Availability,
    "AVAILABLE": Available,
}


def _wrap_rust_component(
    comp: Union[_ox.Component, _ox.Occurrence],
    *,
    keep_recurrence: bool = False,
) -> Component:
    cls = _COMPONENT_CLASS_MAP.get(comp.name, Component)
    return cls(_rust_comp=comp, _keep_recurrence=keep_recurrence)


def _wrap_rust_component_or_cal(cal: _ox.Calendar) -> Component:
    return Calendar(_rust_cal=cal)


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
    def from_jcal(cls, jcal: Union[str, list[Any]]) -> Calendar:
        comp = Component.from_jcal(jcal)
        if isinstance(comp, Calendar):
            return comp
        cal = cls()
        cal.name = comp.name
        cal._props = comp._props
        cal._subcomponents = comp._subcomponents
        return cal

    @classmethod
    def new(
        cls,
        *,
        prodid: Optional[str] = None,
        name: Optional[str] = None,
        calscale: Optional[str] = "GREGORIAN",
        version: str = "2.0",
    ) -> Calendar:
        cal = cls(_rust_cal=_ox.Calendar.new(prodid=prodid, name=name))
        if calscale is not None and calscale != "GREGORIAN":
            cal["CALSCALE"] = calscale
        if version != "2.0":
            cal["VERSION"] = version
        return cal

    @property
    def calendar_name(self) -> Optional[str]:
        return self.decoded("NAME") or self.decoded("X-WR-CALNAME")

    @property
    def prodid(self) -> Optional[str]:
        return self.decoded("PRODID")

    @property
    def version(self) -> Optional[str]:
        return self.decoded("VERSION")

    @property
    def calscale(self) -> Optional[str]:
        return self.decoded("CALSCALE")

    @property
    def method(self) -> Optional[str]:
        return self.decoded("METHOD")

    @property
    def color(self) -> Optional[str]:
        return self.decoded("COLOR")

    @property
    def description(self) -> Optional[str]:
        return self.decoded("DESCRIPTION") or self.decoded("X-WR-CALDESC")

    @property
    def categories(self) -> list[str]:
        val = self.decoded("CATEGORIES")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [str(val)]

    @property
    def source(self) -> Optional[str]:
        return self.decoded("SOURCE")

    @property
    def refresh_interval(self) -> Any:
        return self.decoded("REFRESH-INTERVAL")

    @property
    def url(self) -> Optional[str]:
        return self.decoded("URL")

    @property
    def images(self) -> list[Image]:
        val = self.get("IMAGE")
        if val is None:
            return []
        items = val if isinstance(val, list) else [val]
        return [v if isinstance(v, Image) else Image(v) for v in items]

    def cancel_occurrence(self, uid: str, recurrence_id: Any) -> Calendar:
        """Exclude a specific occurrence of ``uid`` by adding an ``EXDATE`` to its master component."""
        self._ensure_materialized()
        for comp in self._subcomponents:
            if comp.uid == uid and "RECURRENCE-ID" not in comp:
                comp.add("EXDATE", recurrence_id)
                return self
        raise KeyError(f"master component with UID={uid!r} not found")

    def modify_occurrence(
        self,
        uid: str,
        recurrence_id: Any,
        *,
        range_this_and_future: bool = False,
        **updates: Any,
    ) -> Component:
        """Create or update a ``RECURRENCE-ID`` override component for ``uid``."""
        self._ensure_materialized()
        master = next(
            (c for c in self._subcomponents if c.uid == uid and "RECURRENCE-ID" not in c),
            None,
        )
        if master is None:
            raise KeyError(f"master component with UID={uid!r} not found")
        ov = master.copy()
        for rec_key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
            if rec_key in ov:
                del ov[rec_key]
        rid_params = {"RANGE": "THISANDFUTURE"} if range_this_and_future else None
        ov.add("RECURRENCE-ID", recurrence_id, parameters=rid_params)
        if "start" in updates:
            ov["DTSTART"] = updates.pop("start")
        if "end" in updates:
            end_key = "DUE" if ov.name == "VTODO" else "DTEND"
            ov[end_key] = updates.pop("end")
        if "duration" in updates:
            ov["DURATION"] = updates.pop("duration")
        for k, v in updates.items():
            ov[k.upper()] = v
        seq = int(ov.decoded("SEQUENCE", 0) or 0) + 1
        ov["SEQUENCE"] = seq
        self.add_component(ov)
        return ov

    def complete_occurrence(
        self,
        uid: str,
        recurrence_id: Any,
        *,
        completed_at: Optional[datetime.datetime] = None,
    ) -> Component:
        """Mark one occurrence of a recurring ``VTODO`` as ``COMPLETED`` via an override."""
        ts = completed_at or datetime.datetime.now(datetime.timezone.utc)
        return self.modify_occurrence(
            uid,
            recurrence_id,
            status="COMPLETED",
            completed=ts,
            percent_complete=100,
        )

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

    @property
    def freebusy(self) -> list[FreeBusy]:
        if not self._materialized and self._rust_cal is not None:
            return [FreeBusy(_rust_comp=fb) for fb in self._rust_cal.freebusies]
        return [c for c in self.subcomponents if c.name == "VFREEBUSY"]  # type: ignore[misc]

    @property
    def availabilities(self) -> list[Availability]:
        if not self._materialized and self._rust_cal is not None:
            return [Availability(_rust_comp=av) for av in self._rust_cal.availabilities]
        return [c for c in self.subcomponents if c.name == "VAVAILABILITY"]  # type: ignore[misc]

    def with_uid(self, uid: str) -> list[Component]:
        if not self._materialized and self._rust_cal is not None:
            return [_wrap_rust_component(c) for c in self._rust_cal.with_uid(uid)]
        return [c for c in self.walk() if c.decoded("UID") == uid]

    def get_used_tzids(self) -> list[str]:
        return self._as_rust_calendar().used_tzids()

    def get_missing_tzids(self) -> list[str]:
        return self._as_rust_calendar().missing_tzids()

    def validate(self) -> list[tuple[int, str]]:
        return self._as_rust_calendar().validate()

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


_COMPONENT_CLASS_MAP["VCALENDAR"] = Calendar
LazyCalendar = Calendar


__all__ = [
    "Alarm",
    "Availability",
    "Available",
    "Calendar",
    "Component",
    "Conference",
    "Event",
    "FreeBusy",
    "Image",
    "Journal",
    "LazyCalendar",
    "Parameters",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "Todo",
    "is_utc",
    "use_zoneinfo",
    "vBinary",
    "vBoolean",
    "vBroken",
    "vCalAddress",
    "vCategory",
    "vDDDLists",
    "vDDDTypes",
    "vDate",
    "vDatetime",
    "vDuration",
    "vFloat",
    "vFrequency",
    "vGeo",
    "vInt",
    "vMonth",
    "vPeriod",
    "vRecur",
    "vSkip",
    "vText",
    "vTime",
    "vUTCOffset",
    "vUid",
    "vUnknown",
    "vUri",
    "vWeekday",
    "vXmlReference",
]
