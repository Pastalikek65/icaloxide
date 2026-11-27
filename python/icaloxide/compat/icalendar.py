"""Drop-in compatibility layer mimicking Python's ``icalendar`` package (v7.x)."""

from __future__ import annotations

import base64
import bisect
import copy
import datetime
import json
import re
from typing import Any, Iterable, Iterator, Optional, Sequence, Union

import icaloxide as _ox
from icaloxide.compat.errors import ICalParsingError, JCalParsingError
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
                return f"{val.year:04d}{val.month:02d}{val.day:02d}T{val.hour:02d}{val.minute:02d}{val.second:02d}Z"
            if "TZID" not in params:
                key = getattr(val.tzinfo, "key", None) or tzname
                if key:
                    params["TZID"] = key
        return f"{val.year:04d}{val.month:02d}{val.day:02d}T{val.hour:02d}{val.minute:02d}{val.second:02d}"
    if isinstance(val, datetime.date):
        params.setdefault("VALUE", "DATE")
        return f"{val.year:04d}{val.month:02d}{val.day:02d}"
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
        return f"{val.hour:02d}{val.minute:02d}{val.second:02d}"
    return str(val)


def _format_utc_offset(td: datetime.timedelta, colon: bool = False) -> str:
    total = int(td.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    sep = ":" if colon else ""
    if secs:
        return f"{sign}{hours:02}{sep}{mins:02}{sep}{secs:02}"
    return f"{sign}{hours:02}{sep}{mins:02}"


def _parse_utc_offset(raw: Union[str, bytes]) -> datetime.timedelta:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    s = text.strip().replace(":", "")
    if len(s) not in (5, 7) or s[0] not in ("+", "-"):
        raise ValueError(f"invalid UTC offset: {text!r}")
    sign = -1 if s[0] == "-" else 1
    h = int(s[1:3])
    m = int(s[3:5])
    sec = int(s[5:7]) if len(s) == 7 else 0
    return datetime.timedelta(seconds=sign * (h * 3600 + m * 60 + sec))


_ISO_DUR_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _parse_duration_str(raw: Union[str, bytes]) -> datetime.timedelta:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    m = _ISO_DUR_RE.match(text.strip())
    if not m:
        raise ValueError(f"invalid duration: {text!r}")
    sign = -1 if m.group("sign") == "-" else 1
    td = datetime.timedelta(
        weeks=int(m.group("weeks") or 0),
        days=int(m.group("days") or 0),
        hours=int(m.group("hours") or 0),
        minutes=int(m.group("minutes") or 0),
        seconds=int(m.group("seconds") or 0),
    )
    return -td if sign < 0 else td


def _unescape_ical_text(raw: str) -> str:
    return (
        raw.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\;", ";")
        .replace("\\,", ",")
        .replace("\\\\", "\\")
    )


def _flatten_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not params:
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        k_up = str(k).upper()
        if isinstance(v, (list, tuple)) and len(v) == 1:
            out[k_up] = v[0]
        else:
            out[k_up] = v
    return out


def _ensure_parameters(params: Optional[dict[str, Any]]) -> Parameters:
    if isinstance(params, Parameters):
        return params
    return Parameters(_flatten_params(params)) if params else Parameters()


class Parameters(dict):  # type: ignore[type-arg]
    """Case-insensitive parameter mapping matching ``icalendar.Parameters``."""

    always_quoted = ("ALTREP", "CN", "DIR", "SENT-BY", "MEMBER", "DELEGATED-FROM", "DELEGATED-TO")
    canonical_order: tuple[str, ...] = ()
    quote_also: list[str] = []

    def __init__(self, *args: Any, _on_mutate: Any = None, **kwargs: Any) -> None:
        super().__init__()
        self.quote_also: list[str] = []
        self._on_mutate: Any = None
        data = dict(*args, **kwargs)
        for k, v in data.items():
            dict.__setitem__(self, str(k).upper(), v)
        self._on_mutate = _on_mutate

    def _notify_mutate(self) -> None:
        if self._on_mutate is not None:
            self._on_mutate()

    def __deepcopy__(self, memo: dict[int, Any]) -> Parameters:
        cloned = Parameters({k: copy.deepcopy(v, memo) for k, v in self.items()})
        cloned.quote_also = list(self.quote_also)
        return cloned

    @property
    def params(self) -> Parameters:
        return self

    def move_to_end(self, key: str, last: bool = True) -> None:
        self._notify_mutate()
        k_up = str(key).upper()
        if k_up not in self:
            raise KeyError(key)
        val = dict.pop(self, k_up)
        if last:
            dict.__setitem__(self, k_up, val)
        else:
            items = list(self.items())
            dict.clear(self)
            dict.__setitem__(self, k_up, val)
            for ok, ov in items:
                dict.__setitem__(self, ok, ov)

    def update_tzid_from(self, dt: Any) -> None:
        if isinstance(dt, datetime.datetime) and dt.tzinfo is not None:
            off = dt.utcoffset()
            if off == datetime.timedelta(0):
                self.pop("TZID", None)
                return
            key = getattr(dt.tzinfo, "key", None) or getattr(dt.tzinfo, "zone", None)
            if key:
                self["TZID"] = str(key)

    @classmethod
    def from_jcal_property(cls, jcal_prop: list[Any]) -> Parameters:
        if len(jcal_prop) > 1 and isinstance(jcal_prop[1], dict):
            return cls.from_jcal(jcal_prop[1])
        return cls()

    def __setitem__(self, key: str, value: Any) -> None:
        self._notify_mutate()
        super().__setitem__(str(key).upper(), value)

    def __delitem__(self, key: str) -> None:
        self._notify_mutate()
        super().__delitem__(str(key).upper())

    def pop(self, key: str, *args: Any) -> Any:
        self._notify_mutate()
        return super().pop(str(key).upper(), *args)

    def popitem(self) -> tuple[str, Any]:
        self._notify_mutate()
        return super().popitem()

    def clear(self) -> None:
        self._notify_mutate()
        super().clear()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._notify_mutate()
        data = dict(*args, **kwargs)
        for k, v in data.items():
            dict.__setitem__(self, str(k).upper(), v)

    def setdefault(self, key: str, default: Any = None) -> Any:
        k_up = str(key).upper()
        if k_up in self:
            return self[k_up]
        self._notify_mutate()
        return super().setdefault(k_up, default)

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
        *,
        _on_mutate: Any = None,
    ) -> None:
        self._on_mutate: Any = None
        self._dt = dt
        self.params = _ensure_parameters(params)
        self._on_mutate = _on_mutate

    @property
    def dt(self) -> Any:
        return self._dt

    @dt.setter
    def dt(self, value: Any) -> None:
        if self._on_mutate is not None:
            self._on_mutate()
        self._dt = value

    @property
    def td(self) -> Optional[datetime.timedelta]:
        return self._dt if isinstance(self._dt, datetime.timedelta) else None

    @property
    def TZID(self) -> Optional[str]:
        return self.params.tzid()

    @property
    def VALUE(self) -> Optional[str]:
        return self.params.value()

    @property
    def RELATED(self) -> Optional[str]:
        val = self.params.get("RELATED")
        return str(val) if val is not None else None

    @property
    def RANGE(self) -> Optional[str]:
        val = self.params.get("RANGE")
        return str(val) if val is not None else None

    @property
    def dts(self) -> list[vDDDTypes]:
        return [self]

    def to_property_type(self) -> Any:
        val = self._dt
        if isinstance(val, datetime.datetime):
            return vDatetime(val, params=self.params)
        if isinstance(val, datetime.date):
            return vDate(val, params=self.params)
        if isinstance(val, datetime.time):
            return vTime(val, params=self.params)
        if isinstance(val, datetime.timedelta):
            return vDuration(val, params=self.params)
        if isinstance(val, tuple) and len(val) == 2:
            return vPeriod(val, params=self.params)
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> vDDDTypes:
        return self.__class__(
            copy.deepcopy(self._dt, memo),
            params=copy.deepcopy(self.params, memo),
        )

    def to_ical(self) -> bytes:
        local_params = dict(self.params)
        return _format_temporal(self.dt, local_params).encode("utf-8")

    @classmethod
    def from_ical(
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
        timezone: Any = None,
    ) -> Any:
        text = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).strip()
        if text.startswith(("P", "+P", "-P")):
            return _parse_duration_str(text)
        if len(text) == 8 and text.isdigit():
            return datetime.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        if len(text) == 6 and text.isdigit():
            return datetime.time(int(text[:2]), int(text[2:4]), int(text[4:6]))
        if len(text) in (15, 16) and text[8] in ("T", "t"):
            dt = _parse_until_str(text)
            tz_name = (params or {}).get("TZID") if params else None
            if isinstance(tz_name, (list, tuple)) and tz_name:
                tz_name = tz_name[0]
            if tz_name and dt.tzinfo is None:
                from zoneinfo import ZoneInfo

                dt = dt.replace(tzinfo=ZoneInfo(str(tz_name)))
            elif timezone is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone)
            return dt
        raise ValueError(f"invalid temporal value: {text!r}")

    def to_jcal(self, name: str = "dtstart") -> list[Any]:
        return _prop_to_jcal(name.upper(), self.dt, dict(self.params))

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vDDDTypes:
        pname = str(jcal_prop[0]).upper()
        params = Parameters.from_jcal_property(jcal_prop)
        jtype = str(jcal_prop[2]).lower()
        val = _jcal_values_to_py(pname, jtype, jcal_prop[3:], dict(params))
        return cls(val, params=params)

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
        obj.params = _ensure_parameters(params)
        return obj

    @property
    def ical_value(self) -> str:
        return str(self)

    @property
    def LANGUAGE(self) -> Optional[str]:
        v = self.params.get("LANGUAGE")
        return str(v) if v is not None else None

    @property
    def ALTREP(self) -> Optional[str]:
        v = self.params.get("ALTREP")
        return str(v) if v is not None else None

    @property
    def RELTYPE(self) -> Optional[str]:
        v = self.params.get("RELTYPE")
        return str(v) if v is not None else None

    @property
    def VALUE(self) -> Optional[str]:
        return self.params.value()

    def to_ical(self) -> bytes:
        return _escape_text(str(self)).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> str:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return _unescape_ical_text(text)

    def to_jcal(self, name: str = "summary") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "text", str(self)]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vText:
        params = Parameters.from_jcal_property(jcal_prop)
        val = str(jcal_prop[3]) if len(jcal_prop) > 3 else ""
        return cls(val, params=params)


class vRecur(dict):  # type: ignore[type-arg]
    """Dictionary representation of RFC 5545 ``RRULE`` matching ``icalendar.vRecur``."""

    params: Parameters
    frequencies = ("YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY", "MINUTELY", "SECONDLY")
    canonical_order = (
        "FREQ",
        "UNTIL",
        "COUNT",
        "INTERVAL",
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYDAY",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
        "WKST",
    )

    @property
    def ical_value(self) -> dict[str, Any]:
        return dict(self)

    def __init__(
        self,
        *args: Any,
        params: Optional[dict[str, Any]] = None,
        _on_mutate: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._on_mutate: Any = None
        self.params = _ensure_parameters(params)
        data = dict(*args, **kwargs)
        for k, v in data.items():
            self[k] = v
        self._on_mutate = _on_mutate

    def _notify_mutate(self) -> None:
        if self._on_mutate is not None:
            self._on_mutate()

    def __deepcopy__(self, memo: dict[int, Any]) -> vRecur:
        return vRecur(
            {k: copy.deepcopy(v, memo) for k, v in self.items()},
            params=copy.deepcopy(self.params, memo),
        )

    def __setitem__(self, key: str, value: Any) -> None:
        self._notify_mutate()
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

    def __delitem__(self, key: str) -> None:
        self._notify_mutate()
        super().__delitem__(str(key).upper())

    def pop(self, key: str, *args: Any) -> Any:
        self._notify_mutate()
        return super().pop(str(key).upper(), *args)

    def popitem(self) -> tuple[str, Any]:
        self._notify_mutate()
        return super().popitem()

    def clear(self) -> None:
        self._notify_mutate()
        super().clear()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._notify_mutate()
        data = dict(*args, **kwargs)
        for k, v in data.items():
            self[k] = v

    def setdefault(self, key: str, default: Any = None) -> Any:
        key_up = str(key).upper()
        if key_up in self:
            return self[key_up]
        self[key_up] = default
        return self[key_up]

    @classmethod
    def from_ical(
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
    ) -> vRecur:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        rec = cls(params=params)
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

    def to_jcal(self, name: str = "rrule") -> list[Any]:
        return _prop_to_jcal(name.upper(), self, dict(self.params))

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vRecur:
        params = Parameters.from_jcal_property(jcal_prop)
        rec = dict(jcal_prop[3]) if len(jcal_prop) > 3 and isinstance(jcal_prop[3], dict) else {}
        return cls(rec, params=params)

    def __str__(self) -> str:
        return self.to_ical().decode("utf-8")

    def has_key(self, key: str) -> bool:
        return str(key).upper() in self

    def sorted_keys(self) -> list[str]:
        return sorted(self.keys())

    def sorted_items(self) -> list[tuple[str, Any]]:
        return sorted(self.items(), key=lambda kv: kv[0])

    def move_to_end(self, key: str, last: bool = True) -> None:
        k_up = str(key).upper()
        if k_up not in self:
            raise KeyError(key)
        self._notify_mutate()
        val = dict.pop(self, k_up)
        if last:
            dict.__setitem__(self, k_up, val)
        else:
            items = list(self.items())
            dict.clear(self)
            dict.__setitem__(self, k_up, val)
            for ok, ov in items:
                dict.__setitem__(self, ok, ov)


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


def _str_param_prop(param_name: str, *, upper_val: bool = False) -> property:
    def fget(self: Any) -> Optional[str]:
        v = self.params.get(param_name)
        return str(v) if v is not None else None

    def fset(self: Any, value: Optional[str]) -> None:
        if value is None:
            self.params.pop(param_name, None)
        else:
            s = str(value)
            self.params[param_name] = s.upper() if upper_val else s

    def fdel(self: Any) -> None:
        self.params.pop(param_name, None)

    return property(fget, fset, fdel)


class vGeo:
    def __init__(self, geo: tuple[float, float], params: Optional[dict[str, Any]] = None) -> None:
        self.latitude = float(geo[0])
        self.longitude = float(geo[1])
        self.params = _ensure_parameters(params)

    @property
    def ical_value(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)

    def to_ical(self) -> bytes:
        return f"{self.latitude};{self.longitude}".encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> tuple[float, float]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        lat_s, lon_s = text.strip().split(";", 1)
        return (float(lat_s), float(lon_s))

    def to_jcal(self, name: str = "geo") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "float", [self.latitude, self.longitude]]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vGeo:
        params = Parameters.from_jcal_property(jcal_prop)
        coords = jcal_prop[3]
        return cls((float(coords[0]), float(coords[1])), params=params)


class vCalAddress(vText):
    @classmethod
    def new(
        cls,
        email: str,
        *,
        cn: Optional[str] = None,
        cutype: Optional[str] = None,
        delegated_from: Optional[str] = None,
        delegated_to: Optional[str] = None,
        dir: Optional[str] = None,
        partstat: Optional[str] = None,
        role: Optional[str] = None,
        rsvp: Optional[bool] = None,
        sent_by: Optional[str] = None,
    ) -> vCalAddress:
        addr = str(email)
        if ":" not in addr and "@" in addr:
            addr = f"mailto:{addr}"
        params: dict[str, Any] = {}
        if cn is not None:
            params["CN"] = cn
        if cutype is not None:
            params["CUTYPE"] = str(cutype)
        if delegated_from is not None:
            params["DELEGATED-FROM"] = delegated_from
        if delegated_to is not None:
            params["DELEGATED-TO"] = delegated_to
        if dir is not None:
            params["DIR"] = dir
        if partstat is not None:
            params["PARTSTAT"] = str(partstat)
        if role is not None:
            params["ROLE"] = str(role)
        if rsvp is not None:
            params["RSVP"] = "TRUE" if rsvp else "FALSE"
        if sent_by is not None:
            params["SENT-BY"] = sent_by
        return cls(addr, params=params)

    @property
    def email(self) -> str:
        s = str(self)
        return s[7:] if s.lower().startswith("mailto:") else s

    CN = _str_param_prop("CN")
    CUTYPE = _str_param_prop("CUTYPE")
    DELEGATED_FROM = _str_param_prop("DELEGATED-FROM")
    DELEGATED_TO = _str_param_prop("DELEGATED-TO")
    DIR = _str_param_prop("DIR")
    PARTSTAT = _str_param_prop("PARTSTAT")
    ROLE = _str_param_prop("ROLE")
    SENT_BY = _str_param_prop("SENT-BY")
    name = CN

    @property
    def RSVP(self) -> Optional[bool]:
        v = self.params.get("RSVP")
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        return str(v).strip().upper() == "TRUE"

    @RSVP.setter
    def RSVP(self, value: Optional[bool]) -> None:
        if value is None:
            self.params.pop("RSVP", None)
        else:
            self.params["RSVP"] = "TRUE" if value else "FALSE"

    @RSVP.deleter
    def RSVP(self) -> None:
        self.params.pop("RSVP", None)

    def to_jcal(self, name: str = "attendee") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "cal-address", str(self)]


class vUri(vText):
    @property
    def uri(self) -> str:
        return str(self)

    FMTTYPE = _str_param_prop("FMTTYPE")
    GAP = _str_param_prop("GAP")
    LABEL = _str_param_prop("LABEL")
    LINKREL = _str_param_prop("LINKREL")

    def to_jcal(self, name: str = "url") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "uri", str(self)]


class vBoolean:
    BOOL_MAP = {"TRUE": True, "FALSE": False}

    def __init__(self, val: bool, params: Optional[dict[str, Any]] = None) -> None:
        self.val = bool(val)
        self.params = Parameters(params) if params else Parameters()

    @property
    def ical_value(self) -> bool:
        return self.val

    def to_ical(self) -> bytes:
        return b"TRUE" if self.val else b"FALSE"

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> bool:
        text = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).strip().upper()
        if text == "TRUE":
            return True
        if text == "FALSE":
            return False
        raise ValueError(f"invalid boolean value: {text!r}")

    def to_jcal(self, name: str = "rsvp") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "boolean", self.val]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vBoolean:
        params = Parameters.from_jcal_property(jcal_prop)
        return cls(bool(jcal_prop[3]), params=params)

    def __bool__(self) -> bool:
        return self.val


class vInt(int):
    params: Parameters

    def __new__(cls, val: Any, params: Optional[dict[str, Any]] = None) -> vInt:
        obj = super().__new__(cls, int(val))
        obj.params = _ensure_parameters(params)
        return obj

    @property
    def ical_value(self) -> int:
        return int(self)

    def to_ical(self) -> bytes:
        return str(int(self)).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> int:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return int(text.strip())

    def to_jcal(self, name: str = "sequence") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "integer", int(self)]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vInt:
        params = Parameters.from_jcal_property(jcal_prop)
        return cls(int(jcal_prop[3]), params=params)


class vFloat(float):
    params: Parameters

    def __new__(cls, val: Any, params: Optional[dict[str, Any]] = None) -> vFloat:
        obj = super().__new__(cls, float(val))
        obj.params = _ensure_parameters(params)
        return obj

    @property
    def ical_value(self) -> float:
        return float(self)

    def to_ical(self) -> bytes:
        return str(float(self)).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> float:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return float(text.strip())

    def to_jcal(self, name: str = "float") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "float", float(self)]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vFloat:
        params = Parameters.from_jcal_property(jcal_prop)
        return cls(float(jcal_prop[3]), params=params)


class vDuration(vDDDTypes):
    @classmethod
    def from_ical(  # type: ignore[override]
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
        timezone: Any = None,
    ) -> datetime.timedelta:
        _ = params, timezone
        return _parse_duration_str(raw)

    def to_jcal(self, name: str = "duration") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "duration", _format_temporal(self.dt, {})]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vDuration:  # type: ignore[override]
        params = Parameters.from_jcal_property(jcal_prop)
        return cls(_parse_duration_str(str(jcal_prop[3])), params=params)


class vPeriod:
    def __init__(
        self,
        per: tuple[datetime.datetime, Union[datetime.datetime, datetime.timedelta]],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.start = per[0]
        self.end_or_dur = per[1]
        self.params = _ensure_parameters(params)

    @property
    def dt(self) -> tuple[Any, Any]:
        return (self.start, self.end_or_dur)

    @property
    def ical_value(self) -> tuple[Any, Any]:
        return (self.start, self.end_or_dur)

    FBTYPE = _str_param_prop("FBTYPE")
    RANGE = _str_param_prop("RANGE")
    RELATED = _str_param_prop("RELATED")
    TZID = _str_param_prop("TZID")
    VALUE = _str_param_prop("VALUE")

    def overlaps(self, other: vPeriod) -> bool:
        s1 = self.start
        e1 = s1 + self.end_or_dur if isinstance(self.end_or_dur, datetime.timedelta) else self.end_or_dur
        s2 = other.start
        e2 = (
            s2 + other.end_or_dur
            if isinstance(other.end_or_dur, datetime.timedelta)
            else other.end_or_dur
        )
        return s1 < e2 and s2 < e1

    def to_ical(self) -> bytes:
        s = _format_temporal(self.start, {})
        e = _format_temporal(self.end_or_dur, {})
        return f"{s}/{e}".encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> tuple[Any, Any]:
        text = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).strip()
        lhs, rhs = text.split("/", 1)
        start = vDDDTypes.from_ical(lhs)
        end = _parse_duration_str(rhs) if rhs.startswith(("P", "+P", "-P")) else vDDDTypes.from_ical(rhs)
        return (start, end)

    def to_jcal(self, name: str = "freebusy") -> list[Any]:
        iso_s = _iso_temporal(self.start)
        if isinstance(self.end_or_dur, datetime.timedelta):
            iso_e = _format_temporal(self.end_or_dur, {})
        else:
            iso_e = _iso_temporal(self.end_or_dur)
        return [name.lower(), self.params.to_jcal(), "period", f"{iso_s}/{iso_e}"]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vPeriod:
        params = Parameters.from_jcal_property(jcal_prop)
        lhs, rhs = str(jcal_prop[3]).split("/", 1)
        s = _parse_iso_dt(lhs, dict(params))
        e: Any = (
            _parse_duration_str(rhs)
            if rhs.startswith(("P", "+P", "-P"))
            else _parse_iso_dt(rhs, dict(params))
        )
        return cls((s, e), params=params)


class vUTCOffset(vDDDTypes):
    def format(self) -> str:
        return _format_utc_offset(self.dt)

    def to_ical(self) -> bytes:
        return _format_utc_offset(self.dt).encode("utf-8")

    @classmethod
    def from_ical(  # type: ignore[override]
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
        timezone: Any = None,
    ) -> datetime.timedelta:
        _ = params, timezone
        return _parse_utc_offset(raw)

    def to_jcal(self, name: str = "tzoffsetto") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "utc-offset", _format_utc_offset(self.dt, colon=True)]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vUTCOffset:  # type: ignore[override]
        params = Parameters.from_jcal_property(jcal_prop)
        return cls(_parse_utc_offset(str(jcal_prop[3])), params=params)


class vCategory:
    def __init__(self, cats: Iterable[str], params: Optional[dict[str, Any]] = None) -> None:
        self.cats = [vText(c) for c in cats]
        self.params = _ensure_parameters(params)

    def __iter__(self) -> Iterator[vText]:
        return iter(self.cats)

    def __len__(self) -> int:
        return len(self.cats)

    def __getitem__(self, idx: Any) -> Any:
        return self.cats[idx]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, vCategory):
            return self.cats == other.cats
        if isinstance(other, list):
            return list(self.cats) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"vCategory({[str(c) for c in self.cats]!r})"

    @property
    def ical_value(self) -> list[str]:
        return [str(c) for c in self.cats]

    def to_ical(self) -> bytes:
        return ",".join(_escape_text(str(c)) for c in self.cats).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> list[str]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        items: list[str] = []
        buf: list[str] = []
        it = iter(text)
        for ch in it:
            if ch == ",":
                items.append(_unescape_ical_text("".join(buf)))
                buf.clear()
            elif ch == "\\":
                nxt = next(it, "")
                buf.append("\\" + nxt)
            else:
                buf.append(ch)
        if buf or items:
            items.append(_unescape_ical_text("".join(buf)))
        return items

    def to_jcal(self, name: str = "categories") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "text", *[str(c) for c in self.cats]]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vCategory:
        params = Parameters.from_jcal_property(jcal_prop)
        return cls([str(v) for v in jcal_prop[3:]], params=params)


class vBinary:
    def __init__(self, obj: Union[bytes, str], params: Optional[dict[str, Any]] = None) -> None:
        self.obj = obj
        self.params = Parameters(params) if params else Parameters()

    @property
    def base64data(self) -> str:
        return self.to_ical().decode("ascii")

    @property
    def ical_value(self) -> Union[bytes, str]:
        return self.obj

    def to_ical(self) -> bytes:
        if isinstance(self.obj, bytes):
            return base64.b64encode(self.obj)
        return str(self.obj).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> bytes:
        data = raw.encode("ascii") if isinstance(raw, str) else raw
        return base64.b64decode(data.strip())

    def to_jcal(self, name: str = "attach") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "binary", self.to_ical().decode("ascii")]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vBinary:
        params = Parameters.from_jcal_property(jcal_prop)
        raw = str(jcal_prop[3]) if len(jcal_prop) > 3 else ""
        return cls(base64.b64decode(raw), params=params)


class vDate(vDDDTypes):
    def to_jcal(self, name: str = "dtstart") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "date", self.dt.isoformat()]


class vDatetime(vDDDTypes):
    def is_utc(self) -> bool:
        return is_utc(self.dt)

    def to_jcal(self, name: str = "dtstart") -> list[Any]:
        return _prop_to_jcal(name.upper(), self.dt, dict(self.params))


class vTime(vDDDTypes):
    def is_utc(self) -> bool:
        t = self.dt
        return (
            isinstance(t, datetime.time)
            and t.tzinfo is not None
            and t.utcoffset() == datetime.timedelta(0)
        )

    @classmethod
    def from_ical(
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
        timezone: Any = None,
    ) -> datetime.time:
        text = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).strip()
        is_utc_flag = text.endswith(("Z", "z"))
        digits = text[:-1] if is_utc_flag else text
        t = datetime.time(int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))
        if is_utc_flag:
            return t.replace(tzinfo=datetime.timezone.utc)
        if timezone is not None:
            return t.replace(tzinfo=timezone)
        return t

    def to_jcal(self, name: str = "time") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "time", self.dt.strftime("%H:%M:%S")]


class vDDDLists:
    """List of ``vDDDTypes`` temporals matching ``icalendar.prop.vDDDLists``."""

    def __init__(
        self,
        dt_list: Iterable[Any],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.params = _ensure_parameters(params)
        self.dts = [x if isinstance(x, vDDDTypes) else vDDDTypes(x, self.params) for x in dt_list]

    def to_ical(self) -> bytes:
        return b",".join(d.to_ical() for d in self.dts)

    @classmethod
    def from_ical(
        cls,
        raw: Union[str, bytes],
        params: Optional[dict[str, Any]] = None,
    ) -> list[Any]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return [vDDDTypes.from_ical(item.strip(), params=params) for item in text.split(",") if item.strip()]

    def to_jcal(self, name: str = "rdate") -> list[Any]:
        return _prop_to_jcal(name.upper(), [d.dt for d in self.dts], dict(self.params))

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vDDDLists:
        pname = str(jcal_prop[0]).upper()
        params = Parameters.from_jcal_property(jcal_prop)
        jtype = str(jcal_prop[2]).lower()
        vals = _jcal_values_to_py(pname, jtype, jcal_prop[3:], dict(params))
        items = vals if isinstance(vals, list) else [vals]
        return cls(items, params=params)

    def __iter__(self) -> Iterator[vDDDTypes]:
        return iter(self.dts)

    def __len__(self) -> int:
        return len(self.dts)

    def __getitem__(self, idx: Any) -> Any:
        return self.dts[idx]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, vDDDLists):
            return self.dts == other.dts
        if isinstance(other, list):
            return self.dts == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"vDDDLists({[d.dt for d in self.dts]!r})"


_WEEKDAY_RE = re.compile(r"^([+-]?\d+)?([A-Z]{2})$")


class vWeekday(vText):
    week_days = {"SU": 0, "MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6}

    @property
    def relative(self) -> Optional[int]:
        m = _WEEKDAY_RE.match(str(self).strip().upper())
        if m and m.group(1):
            return int(m.group(1))
        return None

    @property
    def weekday(self) -> str:
        m = _WEEKDAY_RE.match(str(self).strip().upper())
        if m:
            return m.group(2)
        return str(self).strip().upper()


class vFrequency(vText):
    frequencies = ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")


class vMonth(vInt):
    _leap: bool = False

    def __new__(cls, val: Any, params: Optional[dict[str, Any]] = None) -> vMonth:
        s = str(val).strip()
        is_leap = s.upper().endswith("L")
        num_val = int(s[:-1]) if is_leap else int(val)
        obj = super().__new__(cls, num_val, params=params)
        obj._leap = is_leap
        return obj  # type: ignore[return-value]

    @property
    def leap(self) -> bool:
        return self._leap

    def to_ical(self) -> bytes:
        return f"{int(self)}{'L' if self._leap else ''}".encode("utf-8")


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


def use_pytz() -> None:
    """Compatibility no-op kept for drop-in ``icalendar`` imports."""


def q_join(items: Iterable[Any], sep: str = ",") -> str:
    """Join parameter values, quoting any element containing delimiters."""
    return sep.join(_format_param_val(str(x)) for x in items)


def q_split(text: str, sep: str = ",", maxsplit: int = -1) -> list[str]:
    """Split ``text`` by ``sep`` outside double quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == sep and not in_quotes and (maxsplit < 0 or len(parts) < maxsplit):
            parts.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _is_binary_prop(params: Any) -> bool:
    if not params:
        return False
    val = params.get("VALUE")
    enc = params.get("ENCODING")
    v_str = str(val[0] if isinstance(val, (list, tuple)) and val else val or "").upper()
    e_str = str(enc[0] if isinstance(enc, (list, tuple)) and enc else enc or "").upper()
    return v_str == "BINARY" or e_str == "BASE64"


def _decode_binary_val(val: Any) -> bytes:
    if isinstance(val, vBinary):
        return val.obj if isinstance(val.obj, bytes) else str(val.obj).encode("utf-8")
    if isinstance(val, bytes):
        return val
    text = str(val).strip()
    try:
        return base64.b64decode(text)
    except Exception:
        return text.encode("utf-8")


def _temporal_params_for_value(
    val: Any, orig_params: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    out = {
        k: v
        for k, v in _flatten_params(orig_params).items()
        if k not in ("TZID", "VALUE")
    }
    if isinstance(val, datetime.datetime):
        if val.tzinfo is not None:
            key = getattr(val.tzinfo, "key", None) or getattr(val.tzinfo, "zone", None)
            if key and str(key).upper() not in ("UTC", "ETC/UTC", "Z"):
                out["TZID"] = str(key)
            elif val.utcoffset() != datetime.timedelta(0) and orig_params:
                tz_orig = _flatten_params(orig_params).get("TZID")
                if tz_orig and str(tz_orig).upper() not in ("UTC", "ETC/UTC", "Z"):
                    out["TZID"] = str(tz_orig)
    elif isinstance(val, datetime.date):
        out["VALUE"] = "DATE"
    return out


class _TrackedSubcomponents(list):  # type: ignore[type-arg]
    """Live ``subcomponents`` list that marks its owning ``Component`` dirty on mutation."""

    def __init__(self, owner: Component, items: Iterable[Component] = ()) -> None:
        super().__init__(items)
        self._owner = owner

    def _bind(self, child: Any) -> Any:
        if isinstance(child, Component):
            child._parent = self._owner
        return child

    def _sync_owner(self) -> None:
        self._owner._mark_dirty()

    def append(self, item: Any) -> None:
        self._sync_owner()
        super().append(self._bind(item))

    def extend(self, items: Iterable[Any]) -> None:
        bound = [self._bind(x) for x in items]
        self._sync_owner()
        super().extend(bound)

    def insert(self, index: Any, item: Any) -> None:
        self._sync_owner()
        super().insert(index, self._bind(item))

    def pop(self, index: Any = -1) -> Any:
        self._sync_owner()
        return super().pop(index)

    def remove(self, item: Any) -> None:
        self._sync_owner()
        super().remove(item)

    def clear(self) -> None:
        self._sync_owner()
        super().clear()

    def __setitem__(self, index: Any, value: Any) -> None:
        self._sync_owner()
        if isinstance(index, slice):
            super().__setitem__(index, [self._bind(x) for x in value])
        else:
            super().__setitem__(index, self._bind(value))

    def __delitem__(self, index: Any) -> None:
        self._sync_owner()
        super().__delitem__(index)

    def __iadd__(self, items: Iterable[Any]) -> _TrackedSubcomponents:
        self.extend(items)
        return self

    def reverse(self) -> None:
        self._sync_owner()
        super().reverse()

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._sync_owner()
        super().sort(key=key, reverse=reverse)


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
_CAL_ADDRESS_KEYS = frozenset({"ATTENDEE", "ORGANIZER"})
_URI_KEYS = frozenset({"URL", "ATTACH", "TZURL", "SOURCE", "IMAGE", "CONFERENCE", "LINK"})
_RAW_STR_KEYS = frozenset(
    {"RRULE", "ATTACH", "URL", "UID", "ATTENDEE", "ORGANIZER", "LINK", "CONCEPT"}
)
_MISSING = object()


def _scalar_prop(
    key: str,
    *,
    default: Any = None,
    cast: Any = None,
    fallback_key: Optional[str] = None,
) -> property:
    def fget(self: Any) -> Any:
        val = self.decoded(key, None)
        if val is None:
            val = self.decoded(fallback_key, default) if fallback_key else default
        if val is not None and cast is not None:
            return cast(val)
        return val

    def fset(self: Any, val: Any) -> None:
        if val is None:
            self.pop(key, None)
        else:
            self[key] = val

    def fdel(self: Any) -> None:
        self.pop(key, None)

    return property(fget, fset, fdel)


def _multi_prop(
    key: str,
    *,
    use_get: bool = False,
    wrap: Any = None,
    unwrap_text: bool = False,
) -> property:
    def fget(self: Any) -> list[Any]:
        val = self.get(key) if use_get else self.decoded(key, None)
        if val is None:
            return []
        items = list(val) if isinstance(val, list) else [val]
        if wrap is not None:
            return [x if isinstance(x, wrap) else wrap(x) for x in items]
        if unwrap_text:
            return [str(x) for x in items]
        return items

    def fset(self: Any, items: Any) -> None:
        self.pop(key, None)
        if items is None:
            return
        seq = items if isinstance(items, (list, tuple)) else [items]
        for item in seq:
            self.add(key, item)

    def fdel(self: Any) -> None:
        self.pop(key, None)

    return property(fget, fset, fdel)


def _categories_prop(key: str = "CATEGORIES") -> property:
    def fget(self: Any) -> list[str]:
        val = self.decoded(key, None)
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [str(val)]

    def fset(self: Any, val: Any) -> None:
        self.pop(key, None)
        if val:
            self.add(key, list(val) if isinstance(val, (list, tuple)) else [val])

    def fdel(self: Any) -> None:
        self.pop(key, None)

    return property(fget, fset, fdel)


def _flatten_exdate_item(item: Any) -> list[Any]:
    if isinstance(item, vDDDLists):
        return [d.dt for d in item.dts]
    if isinstance(item, vDDDTypes):
        return [item.dt]
    if isinstance(item, list):
        out: list[Any] = []
        for sub in item:
            out.extend(_flatten_exdate_item(sub))
        return out
    return [item]


def _exdates_prop() -> property:
    def fget(self: Any) -> list[Any]:
        val = self.get("EXDATE")
        if val is None:
            return []
        raw_items = val if isinstance(val, list) and not isinstance(val, vDDDLists) else [val]
        out: list[Any] = []
        for item in raw_items:
            out.extend(_flatten_exdate_item(item))
        return out

    def fset(self: Any, val: Any) -> None:
        self.pop("EXDATE", None)
        if val:
            seq = val if isinstance(val, (list, tuple)) else [val]
            for item in seq:
                self.add("EXDATE", item)

    def fdel(self: Any) -> None:
        self.pop("EXDATE", None)

    return property(fget, fset, fdel)


def _flatten_rdate_item(item: Any) -> list[tuple[Any, Any]]:
    if isinstance(item, vDDDLists):
        out: list[tuple[Any, Any]] = []
        for d in item.dts:
            out.extend(_flatten_rdate_item(d.dt))
        return out
    if isinstance(item, vDDDTypes):
        return _flatten_rdate_item(item.dt)
    if isinstance(item, vPeriod):
        end_dt = (
            item.start + item.end_or_dur
            if isinstance(item.end_or_dur, datetime.timedelta)
            else item.end_or_dur
        )
        return [(item.start, end_dt)]
    if isinstance(item, tuple) and len(item) == 2:
        end_dt = (item[0] + item[1]) if isinstance(item[1], datetime.timedelta) else item[1]
        return [(item[0], end_dt)]
    if isinstance(item, list):
        out = []
        for sub in item:
            out.extend(_flatten_rdate_item(sub))
        return out
    return [(item, None)]


def _rdates_prop() -> property:
    def fget(self: Any) -> list[tuple[Any, Any]]:
        val = self.get("RDATE")
        if val is None:
            return []
        raw_items = val if isinstance(val, list) and not isinstance(val, vDDDLists) else [val]
        out: list[tuple[Any, Any]] = []
        for item in raw_items:
            out.extend(_flatten_rdate_item(item))
        return out

    def fset(self: Any, val: Any) -> None:
        self.pop("RDATE", None)
        if val:
            seq = val if isinstance(val, (list, tuple)) else [val]
            for item in seq:
                self.add("RDATE", item)

    def fdel(self: Any) -> None:
        self.pop("RDATE", None)

    return property(fget, fset, fdel)


def _rrules_prop() -> property:
    def fget(self: Any) -> list[vRecur]:
        val = self.get("RRULE")
        if val is None:
            return []
        return list(val) if isinstance(val, list) else [val]

    def fset(self: Any, val: Any) -> None:
        self.pop("RRULE", None)
        if val:
            seq = val if isinstance(val, (list, tuple)) else [val]
            for item in seq:
                self.add("RRULE", item)

    def fdel(self: Any) -> None:
        self.pop("RRULE", None)

    return property(fget, fset, fdel)


class Component:
    """Compatibility wrapper supporting lazy Rust proxies, mutable editing, dict protocol, and jCal."""

    name: str = "COMPONENT"

    required: tuple[str, ...] = ()
    singletons: tuple[str, ...] = ()
    multiple: tuple[str, ...] = ()
    exclusive: tuple[tuple[str, ...], ...] = ()
    inclusive: tuple[tuple[str, ...], ...] = ()
    canonical_order: tuple[str, ...] = ()
    ignore_exceptions = False

    def __init__(
        self,
        *,
        _rust_comp: Optional[Union[_ox.Component, _ox.Occurrence]] = None,
        _rust_cal: Optional[_ox.Calendar] = None,
        _keep_recurrence: bool = False,
        _parent: Optional[Component] = None,
    ) -> None:
        self._rust_comp = _rust_comp
        self._rust_cal = _rust_cal
        self._keep_recurrence = _keep_recurrence
        self._parent = _parent
        self._children_cache: Optional[_TrackedSubcomponents] = None
        self._errors: Optional[list[Any]] = None
        materialized = _rust_comp is None and _rust_cal is None
        self._materialized = materialized
        if materialized:
            self._props: Optional[list[tuple[str, Any, Optional[Parameters]]]] = []
            self._subcomponents: Optional[_TrackedSubcomponents] = _TrackedSubcomponents(self)
        else:
            self._props = None
            self._subcomponents = None
        if _rust_comp is not None:
            self.name = _rust_comp.name
        elif _rust_cal is not None:
            self.name = "VCALENDAR"

    @property
    def errors(self) -> list[Any]:
        if self._errors is None:
            if self._rust_cal is not None:
                self._errors = list(self._rust_cal.warnings)
            else:
                self._errors = []
        return self._errors

    @errors.setter
    def errors(self, value: list[Any]) -> None:
        self._errors = list(value)

    def _mark_dirty(self) -> None:
        cur: Optional[Component] = self
        while cur is not None:
            if not cur._materialized:
                cur._ensure_materialized()
            cur = cur._parent

    def _make_prop_params(self, raw_params: Any) -> Parameters:
        if isinstance(raw_params, Parameters):
            if raw_params._on_mutate is None:
                raw_params._on_mutate = self._mark_dirty
            return raw_params
        return Parameters(_flatten_params(raw_params), _on_mutate=self._mark_dirty)

    def _make_param_sync(
        self, key_upper: str, match_idx: int, params_obj: Parameters
    ) -> Any:
        def _sync() -> None:
            if not self._materialized:
                self._ensure_materialized()
            if self._props is not None:
                seen = 0
                for idx, (k, v, _p) in enumerate(self._props):
                    if k == key_upper:
                        if seen == match_idx:
                            if hasattr(v, "params") and isinstance(v.params, Parameters):
                                v.params = params_obj
                            self._props[idx] = (k, v, params_obj)
                            break
                        seen += 1
            self._mark_dirty()

        return _sync

    def _make_value_sync(self, key_upper: str, match_idx: int, wrapped: Any) -> Any:
        def _sync() -> None:
            if not self._materialized:
                self._ensure_materialized()
            assert self._props is not None
            seen = 0
            for idx, (k, _v, p) in enumerate(self._props):
                if k == key_upper:
                    if seen == match_idx:
                        if hasattr(wrapped, "params") and isinstance(wrapped.params, Parameters):
                            wrapped.params._on_mutate = self._mark_dirty
                            p = wrapped.params
                        self._props[idx] = (k, wrapped, p)
                        break
                    seen += 1
            if hasattr(wrapped, "_on_mutate"):
                wrapped._on_mutate = self._mark_dirty
            self._mark_dirty()

        return _sync

    def _get_children(self) -> _TrackedSubcomponents:
        if self._materialized:
            assert self._subcomponents is not None
            return self._subcomponents
        if self._children_cache is None:
            if self._rust_cal is not None:
                raw_subs = self._rust_cal.subcomponents
            elif isinstance(self._rust_comp, _ox.Component):
                raw_subs = self._rust_comp.subcomponents
            elif isinstance(self._rust_comp, _ox.Occurrence):
                raw_subs = self._rust_comp.component.subcomponents
            else:
                raw_subs = []
            self._children_cache = _TrackedSubcomponents(
                self, [_wrap_rust_component(c, _parent=self) for c in raw_subs]
            )
        return self._children_cache

    def _ensure_materialized(self) -> None:
        if self._materialized:
            return
        children = self._get_children()
        self._materialized = True
        self._children_cache = None
        props: list[tuple[str, Any, Optional[Parameters]]] = []
        self._props = props
        self._subcomponents = children
        if self._rust_cal is not None:
            for pname, val, raw_params in self._rust_cal.root.raw_properties_compact():
                up = pname.upper()
                if raw_params is None:
                    props.append((up, val, None))
                else:
                    p_obj = self._make_prop_params(raw_params)
                    if _is_binary_prop(p_obj):
                        val = vBinary(_decode_binary_val(val), params=p_obj)
                    props.append((up, val, p_obj))
            return

        if isinstance(self._rust_comp, _ox.Occurrence):
            comp = self._rust_comp.component
            end_key = "DUE" if self._rust_comp.name == "VTODO" else "DTEND"
            for pname, val, raw_params in comp.raw_properties_compact():
                up = pname.upper()
                if up in ("DTSTART", "DTEND", "DUE", "DURATION"):
                    continue
                if not self._keep_recurrence and up in (
                    "RRULE",
                    "RDATE",
                    "EXDATE",
                    "RECURRENCE-ID",
                ):
                    continue
                if raw_params is None:
                    props.append((up, val, None))
                else:
                    p_obj = self._make_prop_params(raw_params)
                    if _is_binary_prop(p_obj):
                        val = vBinary(_decode_binary_val(val), params=p_obj)
                    props.append((up, val, p_obj))
            dtstart_p = _temporal_params_for_value(self._rust_comp.start, comp.params("DTSTART"))
            end_p = _temporal_params_for_value(self._rust_comp.end, comp.params(end_key))
            props.append(
                (
                    "DTSTART",
                    self._rust_comp.start,
                    self._make_prop_params(dtstart_p) if dtstart_p else None,
                )
            )
            props.append(
                (
                    end_key,
                    self._rust_comp.end,
                    self._make_prop_params(end_p) if end_p else None,
                )
            )
        elif self._rust_comp is not None:
            for pname, val, raw_params in self._rust_comp.raw_properties_compact():
                up = pname.upper()
                if raw_params is None:
                    props.append((up, val, None))
                else:
                    p_obj = self._make_prop_params(raw_params)
                    if _is_binary_prop(p_obj):
                        val = vBinary(_decode_binary_val(val), params=p_obj)
                    props.append((up, val, p_obj))

    def _wrap_prop(
        self,
        key_upper: str,
        val: Any,
        params: Any,
        *,
        match_idx: int = 0,
    ) -> Any:
        if params is None:
            p_obj = Parameters()
            p_obj._on_mutate = self._make_param_sync(key_upper, match_idx, p_obj)
        elif isinstance(params, Parameters):
            p_obj = params
            if p_obj._on_mutate is None:
                p_obj._on_mutate = (
                    self._mark_dirty
                    if self._materialized
                    else self._make_param_sync(key_upper, match_idx, p_obj)
                )
        else:
            p_obj = Parameters(_flatten_params(params))
            p_obj._on_mutate = (
                self._mark_dirty
                if self._materialized
                else self._make_param_sync(key_upper, match_idx, p_obj)
            )

        if key_upper == "RRULE":
            if isinstance(val, vRecur):
                val._on_mutate = self._make_value_sync(key_upper, match_idx, val)
                return val
            rec = (
                vRecur(val, params=p_obj)
                if isinstance(val, dict)
                else vRecur.from_ical(str(val), params=p_obj)
            )
            rec._on_mutate = self._make_value_sync(key_upper, match_idx, rec)
            return rec
        if _is_binary_prop(p_obj):
            if isinstance(val, vBinary):
                return val
            return vBinary(_decode_binary_val(val), params=p_obj)
        if key_upper in _TEMPORAL_KEYS and isinstance(val, vDDDTypes):
            val._on_mutate = self._make_value_sync(key_upper, match_idx, val)
            return val
        if key_upper in _TEMPORAL_KEYS and isinstance(
            val, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)
        ):
            wrapped_dt = vDDDTypes(val, p_obj)
            wrapped_dt._on_mutate = self._make_value_sync(key_upper, match_idx, wrapped_dt)
            return wrapped_dt
        if key_upper in ("EXDATE", "RDATE") and isinstance(val, vDDDLists):
            return val
        if key_upper in ("EXDATE", "RDATE") and isinstance(val, list):
            return vDDDLists(val, p_obj)
        if key_upper in ("CATEGORIES", "RESOURCES") and isinstance(val, vCategory):
            return val
        if key_upper in ("CATEGORIES", "RESOURCES") and isinstance(val, list):
            return vCategory(val, p_obj)
        if key_upper in _TEMPORAL_KEYS and isinstance(val, list):
            return [
                vDDDTypes(item, p_obj)
                if isinstance(
                    item, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)
                )
                else item
                for item in val
            ]
        if key_upper in _CAL_ADDRESS_KEYS and isinstance(val, str):
            return val if isinstance(val, vCalAddress) else vCalAddress(val, params=p_obj)
        if key_upper in _URI_KEYS and isinstance(val, str) and not isinstance(val, vBinary):
            return val if isinstance(val, vUri) else vUri(val, params=p_obj)
        if isinstance(val, str):
            return val if isinstance(val, vText) else vText(val, params=p_obj)
        return val

    def decoded(self, name: str, default: Any = _MISSING) -> Any:
        key_upper = name.upper()
        if not self._materialized:
            if isinstance(self._rust_comp, _ox.Occurrence):
                if not self._keep_recurrence and key_upper in (
                    "RRULE",
                    "RDATE",
                    "EXDATE",
                    "RECURRENCE-ID",
                ):
                    if default is _MISSING:
                        raise KeyError(name)
                    return default
                occ_end_key = "DUE" if self._rust_comp.name == "VTODO" else "DTEND"
                if key_upper == "DTSTART":
                    return self._rust_comp.start
                if key_upper == occ_end_key:
                    return self._rust_comp.end
                if key_upper == "DURATION":
                    if default is _MISSING:
                        raise KeyError(name)
                    return default
                pairs = self._rust_comp.component.get_all_with_params(key_upper)
            elif self._rust_comp is not None:
                pairs = self._rust_comp.get_all_with_params(key_upper)
            elif self._rust_cal is not None:
                pairs = self._rust_cal.root.get_all_with_params(key_upper)
            else:
                pairs = []
            if not pairs:
                if default is _MISSING:
                    raise KeyError(name)
                return default
            decoded_vals = [
                vRecur.from_ical(val)
                if key_upper == "RRULE" and isinstance(val, str)
                else (_decode_binary_val(val) if _is_binary_prop(params) else val)
                for val, params in pairs
            ]
            return decoded_vals[0] if len(decoded_vals) == 1 else decoded_vals
        self._ensure_materialized()
        matches = [val for k, val, _ in self._props if k == key_upper]
        if not matches:
            if default is _MISSING:
                raise KeyError(name)
            return default
        raw = matches[0] if len(matches) == 1 else matches
        if isinstance(raw, vDDDTypes):
            return raw.dt
        if isinstance(raw, vDDDLists):
            return [d.dt for d in raw.dts]
        if isinstance(raw, vCategory):
            return [str(c) for c in raw.cats]
        if isinstance(raw, vBinary):
            return _decode_binary_val(raw)
        if key_upper == "RRULE" and isinstance(raw, str):
            return vRecur.from_ical(raw)
        return raw

    def get(self, name: str, default: Any = None) -> Any:
        key_upper = name.upper()
        if not self._materialized:
            if isinstance(self._rust_comp, _ox.Occurrence):
                if not self._keep_recurrence and key_upper in (
                    "RRULE",
                    "RDATE",
                    "EXDATE",
                    "RECURRENCE-ID",
                ):
                    return default
                occ_end_key = "DUE" if self._rust_comp.name == "VTODO" else "DTEND"
                if key_upper == "DTSTART":
                    dt_p = _temporal_params_for_value(
                        self._rust_comp.start,
                        self._rust_comp.component.params("DTSTART"),
                    )
                    return self._wrap_prop(key_upper, self._rust_comp.start, dt_p, match_idx=0)
                if key_upper == occ_end_key:
                    end_p = _temporal_params_for_value(
                        self._rust_comp.end,
                        self._rust_comp.component.params(occ_end_key),
                    )
                    return self._wrap_prop(key_upper, self._rust_comp.end, end_p, match_idx=0)
                if key_upper == "DURATION":
                    return default
                pairs = self._rust_comp.component.get_all_with_params(key_upper)
                if not pairs:
                    return default
                if len(pairs) == 1:
                    return self._wrap_prop(key_upper, pairs[0][0], pairs[0][1], match_idx=0)
                return [
                    self._wrap_prop(key_upper, val, raw_params, match_idx=idx)
                    for idx, (val, raw_params) in enumerate(pairs)
                ]
            if self._rust_comp is not None:
                pairs = self._rust_comp.get_all_with_params(key_upper)
                if not pairs:
                    return default
                if len(pairs) == 1:
                    return self._wrap_prop(key_upper, pairs[0][0], pairs[0][1], match_idx=0)
                return [
                    self._wrap_prop(key_upper, val, raw_params, match_idx=idx)
                    for idx, (val, raw_params) in enumerate(pairs)
                ]
            if self._rust_cal is not None:
                pairs = self._rust_cal.root.get_all_with_params(key_upper)
                if not pairs:
                    return default
                if len(pairs) == 1:
                    return self._wrap_prop(key_upper, pairs[0][0], pairs[0][1], match_idx=0)
                return [
                    self._wrap_prop(key_upper, val, raw_params, match_idx=idx)
                    for idx, (val, raw_params) in enumerate(pairs)
                ]

        self._ensure_materialized()
        matches = [(val, params) for k, val, params in self._props if k == key_upper]
        if not matches:
            return default
        if len(matches) == 1:
            val, params = matches[0]
            return self._wrap_prop(key_upper, val, params, match_idx=0)
        return [
            self._wrap_prop(key_upper, val, params, match_idx=idx)
            for idx, (val, params) in enumerate(matches)
        ]

    def __getitem__(self, name: str) -> Any:
        sentinel = object()
        res = self.get(name, sentinel)
        if res is sentinel:
            raise KeyError(name)
        return res

    def __setitem__(self, name: str, value: Any) -> None:
        self._mark_dirty()
        assert self._props is not None
        key_upper = name.upper()
        self._props = [(k, v, p) for k, v, p in self._props if k != key_upper]
        if hasattr(value, "params") and isinstance(value.params, Parameters):
            params: Optional[Parameters] = self._make_prop_params(value.params)
        else:
            params = None
        self._props.append((key_upper, value, params))

    def __delitem__(self, name: str) -> None:
        self._mark_dirty()
        key_upper = name.upper()
        before = len(self._props)
        self._props = [(k, v, p) for k, v, p in self._props if k != key_upper]
        if len(self._props) == before:
            raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        key_upper = name.upper()
        if not self._materialized:
            if isinstance(self._rust_comp, _ox.Occurrence):
                if not self._keep_recurrence and key_upper in (
                    "RRULE",
                    "RDATE",
                    "EXDATE",
                    "RECURRENCE-ID",
                ):
                    return False
                return key_upper in self._rust_comp
            if self._rust_comp is not None:
                return key_upper in self._rust_comp
            if self._rust_cal is not None:
                return key_upper in self._rust_cal.root
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
        self._mark_dirty()
        self._props.clear()
        list.clear(self._subcomponents)

    def copy(self) -> Component:
        self._ensure_materialized()
        assert self._props is not None and self._subcomponents is not None
        dup = self.__class__()
        dup.name = self.name
        dup._props = copy.deepcopy(self._props)
        assert dup._props is not None
        for _k, v, p in dup._props:
            if p is not None:
                p._on_mutate = dup._mark_dirty
            if hasattr(v, "params") and isinstance(v.params, Parameters):
                if p is not None:
                    v.params = p
                else:
                    v.params._on_mutate = dup._mark_dirty
            if hasattr(v, "_on_mutate"):
                v._on_mutate = dup._mark_dirty
        dup._subcomponents = _TrackedSubcomponents(dup, [sub.copy() for sub in self._subcomponents])
        for sub in dup._subcomponents:
            sub._parent = dup
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
        self._mark_dirty()
        assert self._props is not None
        if isinstance(parameters, Parameters):
            params: Optional[Parameters] = self._make_prop_params(parameters)
        elif parameters:
            params = Parameters(_flatten_params(parameters), _on_mutate=self._mark_dirty)
        elif hasattr(value, "params") and isinstance(value.params, Parameters):
            params = self._make_prop_params(value.params)
        else:
            params = None
        self._props.append((name.upper(), value, params))

    def add_component(self, component: Component) -> None:
        self._mark_dirty()
        component._parent = self
        self._subcomponents.append(component)

    uid = _scalar_prop("UID", cast=str)
    created = _scalar_prop("CREATED")
    last_modified = _scalar_prop("LAST-MODIFIED")
    links = _multi_prop("LINK", use_get=True, wrap=vUri)
    concepts = _multi_prop("CONCEPT", use_get=True, wrap=vUri)
    refids = _multi_prop("REFID", unwrap_text=True)
    related_to = _multi_prop("RELATED-TO", unwrap_text=True)
    comments = _multi_prop("COMMENT", unwrap_text=True)
    DTSTAMP = _scalar_prop("DTSTAMP")
    CREATED = _scalar_prop("CREATED")
    LAST_MODIFIED = _scalar_prop("LAST-MODIFIED")

    def is_empty(self) -> bool:
        self._ensure_materialized()
        return not self._props and not self._subcomponents

    def is_lazy(self) -> bool:
        return not self._materialized

    def is_thunderbird(self) -> bool:
        return "X-MOZ-LASTACK" in self or "X-MOZ-SNOOZE-TIME" in self

    def with_uid(self, uid: str) -> list[Component]:
        return [c for c in self.walk() if c.decoded("UID", None) == uid]

    def stamp(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self["DTSTAMP"] = now

    def popitem(self, last: bool = True) -> tuple[str, Any]:
        ks = self.keys()
        if not ks:
            raise KeyError("component is empty")
        key = ks[-1] if last else ks[0]
        return key, self.pop(key)

    def move_to_end(self, key: str, last: bool = True) -> None:
        self._mark_dirty()
        k_up = key.upper()
        matched = [p for p in self._props if p[0] == k_up]
        if not matched:
            raise KeyError(key)
        rest = [p for p in self._props if p[0] != k_up]
        self._props = (rest + matched) if last else (matched + rest)

    @classmethod
    def fromkeys(cls, iterable: Iterable[str], value: Any = None) -> Component:
        comp = cls()
        for k in iterable:
            comp.add(k, value)
        return comp

    def get_inline(self, name: str, default: Any = None) -> Any:
        return self.decoded(name, default)

    def set_inline(
        self,
        name: str,
        value: Any,
        parameters: Optional[dict[str, Any]] = None,
    ) -> None:
        self._mark_dirty()
        k_up = name.upper()
        self._props = [p for p in self._props if p[0] != k_up]
        self.add(k_up, value, parameters=parameters)

    @classmethod
    def get_component_class(cls, name: str) -> type[Component]:
        return _COMPONENT_CLASS_MAP.get(str(name).upper(), Component)

    @classmethod
    def new(
        cls,
        created: Any = None,
        comments: Any = None,
        concepts: Any = None,
        last_modified: Any = None,
        links: Any = None,
        refids: Any = None,
        related_to: Any = None,
        stamp: Any = None,
        subcomponents: Optional[Iterable[Component]] = None,
        **kwargs: Any,
    ) -> Component:
        comp = cls()
        _apply_common_new_kwargs(
            comp,
            created=created,
            comments=comments,
            concepts=concepts,
            last_modified=last_modified,
            links=links,
            refids=refids,
            related_to=related_to,
            stamp=stamp,
            subcomponents=subcomponents,
        )
        for k, v in kwargs.items():
            if v is not None:
                comp.add(k.upper().replace("_", "-"), v)
        return comp

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Component):
            return NotImplemented
        return (
            self.name == other.name
            and self.sorted_items() == other.sorted_items()
            and self.subcomponents == other.subcomponents
        )

    def content_lines(self) -> list[str]:
        return self.to_ical().decode("utf-8").splitlines()

    def content_line(self, name: str, default: Any = None) -> Any:
        return self.get(name, default)

    @classmethod
    def register(cls, comp_cls: type[Component]) -> type[Component]:
        _COMPONENT_CLASS_MAP[comp_cls.name.upper()] = comp_cls
        return comp_cls

    @classmethod
    def parse(
        cls, st: _ox.InputData, multiple: bool = False, *, strict: bool = False
    ) -> Union[Component, list[Component]]:
        return cls.from_ical(st, multiple=multiple, strict=strict)

    @classmethod
    def from_ical(
        cls, st: _ox.InputData, multiple: bool = False, *, strict: bool = False
    ) -> Union[Component, list[Component]]:
        try:
            if multiple:
                cals = _ox.Calendar.from_ical_multiple(st, strict=strict)
                return [_wrap_rust_component_or_cal(c) for c in cals]
            comp = _ox.parse_component(st, strict=strict)
            return _wrap_rust_component(comp)
        except ValueError as exc:
            raise ICalParsingError(str(exc)) from exc

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
            return self._get_children()
        return self._subcomponents

    def walk(self, name: Optional[str] = None) -> list[Component]:
        target = name.upper() if name else None
        out: list[Component] = []
        if target is None or self.name == target:
            out.append(self)
        for sub in self.subcomponents:
            out.extend(sub.walk(target))
        return out

    def _collect_ical_chunks(
        self, chunks: list[bytes], lines: list[str], *, sorted: bool = True
    ) -> None:
        if not self._materialized:
            if lines:
                chunks.append(_ox.fold_ical_lines(lines))
                lines.clear()
            chunks.append(self.to_ical(sorted=sorted))
            return

        lines.append(f"BEGIN:{self.name}")
        raw_list = self._props or []
        props = _order_props(raw_list, self.canonical_order) if sorted else raw_list
        for key, val, raw_params in props:
            if raw_params is None and isinstance(val, str) and not isinstance(val, (vCalAddress, vUri)):
                if key in _RAW_STR_KEYS:
                    lines.append(f"{key}:{val}")
                elif key == "REQUEST-STATUS":
                    esc_parts = ";".join(_escape_text(p) for p in val.split(";"))
                    lines.append(f"{key}:{esc_parts}")
                else:
                    lines.append(f"{key}:{_escape_text(val)}")
                continue
            params = dict(raw_params) if raw_params is not None else {}
            val_str = self._serialize_value(key, val, params)
            if not params:
                lines.append(f"{key}:{val_str}")
            else:
                param_str = ""
                for pk, pv in params.items():
                    if isinstance(pv, (list, tuple)):
                        joined = ",".join(_format_param_val(str(x)) for x in pv)
                    else:
                        joined = _format_param_val(str(pv))
                    param_str += f";{pk.upper()}={joined}"
                lines.append(f"{key}{param_str}:{val_str}")
        for sub in self._subcomponents or ():
            sub._collect_ical_chunks(chunks, lines, sorted=sorted)
        lines.append(f"END:{self.name}")

    def to_ical(self, sorted: bool = True) -> bytes:
        if not self._materialized:
            if self._rust_cal is not None:
                return self._rust_cal.to_ical(sorted=sorted)
            if isinstance(self._rust_comp, _ox.Occurrence):
                return self._rust_comp.to_ical(
                    keep_recurrence_attributes=self._keep_recurrence
                )
            if self._rust_comp is not None:
                return self._rust_comp.to_ical(sorted=sorted)

        chunks: list[bytes] = []
        lines: list[str] = []
        self._collect_ical_chunks(chunks, lines, sorted=sorted)
        if lines:
            chunks.append(_ox.fold_ical_lines(lines))
        if not chunks:
            return b""
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def to_jcal(self) -> list[Any]:
        """Serialize this component into RFC 7265 jCal nested list structure."""
        self._ensure_materialized()
        jprops: list[list[Any]] = []
        for key, val, raw_params in self._props or ():
            jprops.append(_prop_to_jcal(key, val, raw_params if raw_params is not None else {}))
        jsubs = [sub.to_jcal() for sub in self._subcomponents or ()]
        return [self.name.lower(), jprops, jsubs]

    def to_json(self) -> str:
        """Serialize this component into an RFC 7265 jCal JSON string."""
        return json.dumps(self.to_jcal())

    @classmethod
    def from_jcal(cls, jcal: Union[str, list[Any]]) -> Component:
        """Parse an RFC 7265 jCal JSON string or nested list into a Component."""
        try:
            data = json.loads(jcal) if isinstance(jcal, str) else jcal
        except json.JSONDecodeError as exc:
            raise JCalParsingError(str(exc)) from exc
        if not isinstance(data, list) or len(data) < 3:
            raise JCalParsingError("invalid jCal structure")
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
        if isinstance(val, vUTCOffset):
            for pk, pv in val.params.items():
                params.setdefault(pk, pv)
            return val.to_ical().decode("utf-8")
        if isinstance(val, datetime.timedelta) and key in ("TZOFFSETFROM", "TZOFFSETTO"):
            return _format_utc_offset(val)
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
        if isinstance(val, (vCategory, vDDDLists)):
            for pk, pv in val.params.items():
                params.setdefault(pk, pv)
            return val.to_ical().decode("utf-8")
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


def _order_props(
    props: list[tuple[str, Any, Parameters]], canonical_order: tuple[str, ...]
) -> list[tuple[str, Any, Parameters]]:
    order_map = {k: idx for idx, k in enumerate(canonical_order)}
    fallback = len(order_map)
    return sorted(props, key=lambda item: (order_map.get(item[0], fallback), item[0]))


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

    if isinstance(val, vCategory):
        for pk, pv in val.params.items():
            params.setdefault(pk.lower(), _normalize_jcal_param(pv))
        return [pname, params, "text", *[str(c) for c in val.cats]]
    if isinstance(val, vDDDLists):
        for pk, pv in val.params.items():
            lp = pk.lower()
            if lp != "value":
                params.setdefault(lp, _normalize_jcal_param(pv))
        return _prop_to_jcal(key, [d.dt for d in val.dts], params)
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
    if isinstance(val, vUTCOffset) or (key in ("TZOFFSETFROM", "TZOFFSETTO") and isinstance(val, datetime.timedelta)):
        td_val = val.dt if isinstance(val, vUTCOffset) else val
        return [pname, params, "utc-offset", _format_utc_offset(td_val, colon=True)]
    if isinstance(val, vBinary):
        return [pname, params, "binary", val.to_ical().decode("ascii")]
    if isinstance(val, vPeriod):
        return val.to_jcal(name=pname)
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
    if isinstance(val, vCalAddress) or (key in _CAL_ADDRESS_KEYS and isinstance(val, str)):
        return [pname, params, "cal-address", str(val)]
    if (
        (isinstance(val, vUri) or key in _URI_KEYS)
        and isinstance(val, str)
        and not _is_binary_prop(raw_params)
    ):
        return [pname, params, "uri", str(val)]
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
    if jtype == "utc-offset":
        parsed_off = [_parse_utc_offset(str(v)) for v in vals]
        return parsed_off[0] if len(parsed_off) == 1 else parsed_off
    if jtype == "duration":
        parsed_dur = [_parse_duration_str(str(v)) for v in vals]
        return parsed_dur[0] if len(parsed_dur) == 1 else parsed_dur
    if jtype == "period":
        out_p = [
            (
                vPeriod.from_jcal([pname, params, "period", v]).start,
                vPeriod.from_jcal([pname, params, "period", v]).end_or_dur,
            )
            for v in vals
        ]
        return out_p[0] if len(out_p) == 1 else out_p
    if jtype == "binary":
        return vBinary(base64.b64decode(str(vals[0])), params=params)
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


def _add_seq_or_single(comp: Component, prop_name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            comp.add(prop_name, item)
    else:
        comp.add(prop_name, value)


def _apply_common_new_kwargs(
    comp: Component,
    *,
    attendees: Optional[Sequence[Any]] = None,
    categories: Sequence[str] = (),
    classification: Any = None,
    color: Optional[str] = None,
    comments: Any = None,
    concepts: Any = None,
    conferences: Optional[Sequence[Any]] = None,
    contacts: Any = None,
    created: Any = None,
    description: Any = None,
    last_modified: Any = None,
    links: Any = None,
    location: Optional[str] = None,
    organizer: Any = None,
    priority: Optional[int] = None,
    recurrence_id: Any = None,
    refids: Any = None,
    related_to: Any = None,
    request_status: Any = None,
    sequence: Optional[int] = None,
    stamp: Any = None,
    status: Any = None,
    subcomponents: Optional[Iterable[Component]] = None,
    transparency: Any = None,
    uid: Any = None,
    url: Optional[str] = None,
) -> None:
    if uid is not None:
        comp.add("UID", str(uid))
    if created is not None:
        comp.add("CREATED", created)
    if last_modified is not None:
        comp.add("LAST-MODIFIED", last_modified)
    if stamp is not None:
        comp.add("DTSTAMP", stamp)
    _add_seq_or_single(comp, "DESCRIPTION", description)
    if location is not None:
        comp.add("LOCATION", location)
    if status is not None:
        comp.add("STATUS", str(status))
    if classification is not None:
        comp.add("CLASS", str(classification))
    if transparency is not None:
        comp.add("TRANSP", str(transparency))
    if priority is not None:
        comp.add("PRIORITY", priority)
    if sequence is not None:
        comp.add("SEQUENCE", sequence)
    if recurrence_id is not None:
        comp.add("RECURRENCE-ID", recurrence_id)
    if color is not None:
        comp.add("COLOR", color)
    if url is not None:
        comp.add("URL", url)
    if organizer is not None:
        comp.add("ORGANIZER", organizer)
    if categories:
        comp.add("CATEGORIES", list(categories))
    _add_seq_or_single(comp, "ATTENDEE", attendees)
    _add_seq_or_single(comp, "COMMENT", comments)
    _add_seq_or_single(comp, "CONCEPT", concepts)
    _add_seq_or_single(comp, "CONFERENCE", conferences)
    _add_seq_or_single(comp, "CONTACT", contacts)
    _add_seq_or_single(comp, "LINK", links)
    _add_seq_or_single(comp, "REFID", refids)
    _add_seq_or_single(comp, "RELATED-TO", related_to)
    _add_seq_or_single(comp, "REQUEST-STATUS", request_status)
    if subcomponents is not None:
        for sub in subcomponents:
            comp.add_component(sub)


class _SpanMixin(Component):
    """Shared ergonomic properties matching ``icalendar 7.x`` Event/Todo/Journal."""

    @property
    def start(self) -> Any:
        return self.decoded("DTSTART", None)

    @start.setter
    def start(self, val: Any) -> None:
        self["DTSTART"] = val

    @start.deleter
    def start(self) -> None:
        self.pop("DTSTART", None)

    def set_start(self, val: Any, parameters: Optional[dict[str, Any]] = None) -> None:
        self._ensure_materialized()
        self._props = [(k, v, p) for k, v, p in self._props if k != "DTSTART"]
        self.add("DTSTART", val, parameters=parameters)

    @property
    def end(self) -> Any:
        if self.name == "VTODO" and "DUE" in self:
            return self.decoded("DUE", None)
        if "DTEND" in self:
            return self.decoded("DTEND", None)
        st = self.start
        dur = self.decoded("DURATION", None)
        if st is not None and isinstance(dur, datetime.timedelta):
            return st + dur
        if (
            st is not None
            and self.name == "VEVENT"
            and isinstance(st, datetime.date)
            and not isinstance(st, datetime.datetime)
        ):
            return st + datetime.timedelta(days=1)
        return st

    @end.setter
    def end(self, val: Any) -> None:
        key = "DUE" if self.name == "VTODO" else "DTEND"
        self.pop("DURATION", None)
        self[key] = val

    @end.deleter
    def end(self) -> None:
        key = "DUE" if self.name == "VTODO" and "DUE" in self else "DTEND"
        self.pop(key, None)

    def set_end(self, val: Any, parameters: Optional[dict[str, Any]] = None) -> None:
        self._ensure_materialized()
        assert self._props is not None
        key = "DUE" if self.name == "VTODO" else "DTEND"
        self._props = [(k, v, p) for k, v, p in self._props if k not in (key, "DURATION")]
        self.add(key, val, parameters=parameters)

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        if "DURATION" in self:
            return self.decoded("DURATION", None)
        st = self.decoded("DTSTART", None)
        end_key = "DUE" if self.name == "VTODO" else "DTEND"
        en = self.decoded(end_key, None)
        if st is not None and en is not None:
            return en - st
        if st is not None and self.name == "VEVENT":
            if isinstance(st, datetime.date) and not isinstance(st, datetime.datetime):
                return datetime.timedelta(days=1)
            if isinstance(st, datetime.datetime):
                return datetime.timedelta(0)
        return None

    @duration.setter
    def duration(self, val: Any) -> None:
        if val is None:
            self.pop("DURATION", None)
        else:
            end_key = "DUE" if self.name == "VTODO" else "DTEND"
            self.pop(end_key, None)
            self["DURATION"] = val

    @duration.deleter
    def duration(self) -> None:
        self.pop("DURATION", None)

    def set_duration(self, val: datetime.timedelta) -> None:
        end_key = "DUE" if self.name == "VTODO" else "DTEND"
        self.pop(end_key, None)
        self["DURATION"] = val

    summary = _scalar_prop("SUMMARY")
    description = _scalar_prop("DESCRIPTION")
    location = _scalar_prop("LOCATION")
    status = _scalar_prop("STATUS")
    priority = _scalar_prop("PRIORITY")
    sequence = _scalar_prop("SEQUENCE", default=0, cast=int)
    categories = _categories_prop("CATEGORIES")
    color = _scalar_prop("COLOR")
    organizer = _scalar_prop("ORGANIZER")
    attendees = _multi_prop("ATTENDEE", use_get=True)
    rrules = _rrules_prop()
    rdates = _rdates_prop()
    exdates = _exdates_prop()

    @property
    def alarms(self) -> Alarms:
        return Alarms(self)

    classification = _scalar_prop("CLASS")
    transparency = _scalar_prop("TRANSP")
    url = _scalar_prop("URL")
    contacts = _multi_prop("CONTACT", unwrap_text=True)
    conferences = _multi_prop("CONFERENCE", use_get=True, wrap=Conference)
    images = _multi_prop("IMAGE", use_get=True, wrap=Image)
    DTSTART = _scalar_prop("DTSTART")
    DTEND = _scalar_prop("DTEND")
    DUE = _scalar_prop("DUE")
    DURATION = duration
    RECURRENCE_ID = _scalar_prop("RECURRENCE-ID")
    REQUEST_STATUS = _scalar_prop("REQUEST-STATUS")
    X_MOZ_LASTACK = _scalar_prop("X-MOZ-LASTACK")
    X_MOZ_SNOOZE_TIME = _scalar_prop("X-MOZ-SNOOZE-TIME")


class Event(_SpanMixin):
    name = "VEVENT"

    @classmethod
    def new(
        cls,
        *,
        attendees: Optional[Sequence[Any]] = None,
        categories: Sequence[str] = (),
        classification: Any = None,
        color: Optional[str] = None,
        comments: Any = None,
        concepts: Any = None,
        conferences: Optional[Sequence[Any]] = None,
        contacts: Any = None,
        created: Any = None,
        description: Optional[str] = None,
        end: Any = None,
        duration: Optional[datetime.timedelta] = None,
        last_modified: Any = None,
        links: Any = None,
        location: Optional[str] = None,
        organizer: Any = None,
        priority: Optional[int] = None,
        recurrence_id: Any = None,
        refids: Any = None,
        related_to: Any = None,
        request_status: Any = None,
        sequence: Optional[int] = None,
        stamp: Any = None,
        start: Any = None,
        status: Any = None,
        subcomponents: Optional[Iterable[Component]] = None,
        transparency: Any = None,
        summary: Optional[str] = None,
        uid: Any = None,
        url: Optional[str] = None,
    ) -> Event:
        ev = cls()
        _apply_common_new_kwargs(
            ev,
            attendees=attendees,
            categories=categories,
            classification=classification,
            color=color,
            comments=comments,
            concepts=concepts,
            conferences=conferences,
            contacts=contacts,
            created=created,
            description=description,
            last_modified=last_modified,
            links=links,
            location=location,
            organizer=organizer,
            priority=priority,
            recurrence_id=recurrence_id,
            refids=refids,
            related_to=related_to,
            request_status=request_status,
            sequence=sequence,
            stamp=stamp,
            status=status,
            subcomponents=subcomponents,
            transparency=transparency,
            uid=uid,
            url=url,
        )
        if summary is not None:
            ev.add("SUMMARY", summary)
        if start is not None:
            ev.add("DTSTART", start)
        if end is not None:
            ev.add("DTEND", end)
        if duration is not None:
            ev.add("DURATION", duration)
        return ev

    @classmethod
    def example(cls, name: str = "example") -> Event:
        _ = name
        ev = cls.new(
            uid="example-event@icaloxide",
            summary="Example Event",
            start=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc),
            duration=datetime.timedelta(hours=1),
        )
        ev.stamp()
        return ev


class Todo(_SpanMixin):
    name = "VTODO"

    @classmethod
    def new(
        cls,
        *,
        attendees: Optional[Sequence[Any]] = None,
        categories: Sequence[str] = (),
        classification: Any = None,
        color: Optional[str] = None,
        comments: Any = None,
        concepts: Any = None,
        contacts: Any = None,
        conferences: Optional[Sequence[Any]] = None,
        created: Any = None,
        description: Optional[str] = None,
        due: Any = None,
        end: Any = None,
        duration: Optional[datetime.timedelta] = None,
        last_modified: Any = None,
        links: Any = None,
        location: Optional[str] = None,
        organizer: Any = None,
        priority: Optional[int] = None,
        recurrence_id: Any = None,
        refids: Any = None,
        related_to: Any = None,
        request_status: Any = None,
        sequence: Optional[int] = None,
        stamp: Any = None,
        start: Any = None,
        status: Any = None,
        subcomponents: Optional[Iterable[Component]] = None,
        summary: Optional[str] = None,
        uid: Any = None,
        url: Optional[str] = None,
    ) -> Todo:
        td = cls()
        _apply_common_new_kwargs(
            td,
            attendees=attendees,
            categories=categories,
            classification=classification,
            color=color,
            comments=comments,
            concepts=concepts,
            conferences=conferences,
            contacts=contacts,
            created=created,
            description=description,
            last_modified=last_modified,
            links=links,
            location=location,
            organizer=organizer,
            priority=priority,
            recurrence_id=recurrence_id,
            refids=refids,
            related_to=related_to,
            request_status=request_status,
            sequence=sequence,
            stamp=stamp,
            status=status,
            subcomponents=subcomponents,
            uid=uid,
            url=url,
        )
        if summary is not None:
            td.add("SUMMARY", summary)
        if start is not None:
            td.add("DTSTART", start)
        if due is not None:
            td.add("DUE", due)
        elif end is not None:
            td.add("DUE", end)
        if duration is not None:
            td.add("DURATION", duration)
        return td

    @classmethod
    def example(cls, name: str = "example") -> Todo:
        _ = name
        td = cls.new(
            uid="example-todo@icaloxide",
            summary="Example Todo",
            start=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc),
            due=datetime.datetime(2026, 1, 2, 10, 0, tzinfo=datetime.timezone.utc),
            status="NEEDS-ACTION",
        )
        td.stamp()
        return td


class Journal(_SpanMixin):
    name = "VJOURNAL"

    @classmethod
    def new(
        cls,
        *,
        attendees: Optional[Sequence[Any]] = None,
        categories: Sequence[str] = (),
        classification: Any = None,
        color: Optional[str] = None,
        comments: Any = None,
        concepts: Any = None,
        contacts: Any = None,
        created: Any = None,
        description: Any = None,
        last_modified: Any = None,
        links: Any = None,
        organizer: Any = None,
        recurrence_id: Any = None,
        refids: Any = None,
        related_to: Any = None,
        request_status: Any = None,
        sequence: Optional[int] = None,
        stamp: Any = None,
        start: Any = None,
        status: Any = None,
        summary: Optional[str] = None,
        uid: Any = None,
        url: Optional[str] = None,
    ) -> Journal:
        jn = cls()
        _apply_common_new_kwargs(
            jn,
            attendees=attendees,
            categories=categories,
            classification=classification,
            color=color,
            comments=comments,
            concepts=concepts,
            contacts=contacts,
            created=created,
            description=description,
            last_modified=last_modified,
            links=links,
            organizer=organizer,
            recurrence_id=recurrence_id,
            refids=refids,
            related_to=related_to,
            request_status=request_status,
            sequence=sequence,
            stamp=stamp,
            status=status,
            uid=uid,
            url=url,
        )
        if summary is not None:
            jn.add("SUMMARY", summary)
        if start is not None:
            jn.add("DTSTART", start)
        return jn

    @classmethod
    def example(cls, name: str = "example") -> Journal:
        _ = name
        jn = cls.new(
            uid="example-journal@icaloxide",
            summary="Example Journal",
            start=datetime.date(2026, 1, 1),
            description="Sample journal entry",
        )
        jn.stamp()
        return jn

    descriptions = _multi_prop("DESCRIPTION", unwrap_text=True)


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
        attendees: Optional[Sequence[Any]] = None,
        concepts: Any = None,
        links: Any = None,
        refids: Any = None,
        related_to: Any = None,
        uid: Any = None,
    ) -> Alarm:
        al = cls()
        al.add("ACTION", action)
        _apply_common_new_kwargs(
            al,
            attendees=attendees,
            concepts=concepts,
            description=description,
            links=links,
            refids=refids,
            related_to=related_to,
            uid=uid,
        )
        if trigger is not None:
            al.add("TRIGGER", trigger)
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

    action = _scalar_prop("ACTION")
    trigger = _scalar_prop("TRIGGER")

    @property
    def triggers(self) -> list[Any]:
        t = self.trigger
        return [] if t is None else (list(t) if isinstance(t, list) else [t])

    description = _scalar_prop("DESCRIPTION")
    summary = _scalar_prop("SUMMARY")
    acknowledged = _scalar_prop("ACKNOWLEDGED")
    ACTION = _scalar_prop("ACTION")
    TRIGGER = _scalar_prop("TRIGGER")

    @property
    def TRIGGER_RELATED(self) -> str:
        if "TRIGGER" not in self:
            return "START"
        trig = self["TRIGGER"]
        params = getattr(trig, "params", {})
        return str(params.get("RELATED", "START")).strip().upper()

    @TRIGGER_RELATED.setter
    def TRIGGER_RELATED(self, value: str) -> None:
        if "TRIGGER" in self:
            trig = self["TRIGGER"]
            if hasattr(trig, "params"):
                trig.params["RELATED"] = str(value).upper()

    DURATION = _scalar_prop("DURATION")
    REPEAT = _scalar_prop("REPEAT", default=0, cast=int)
    ACKNOWLEDGED = _scalar_prop("ACKNOWLEDGED")

    Triggers = triggers

    attendees = _multi_prop("ATTENDEE", use_get=True)
    repeat = _scalar_prop("REPEAT", default=0, cast=int)

    @classmethod
    def example(cls, name: str = "example") -> Alarm:
        _ = name
        return cls.new_display(datetime.timedelta(minutes=-15), "Example Alarm")

    duration = _scalar_prop("DURATION")


class FreeBusy(_SpanMixin):
    name = "VFREEBUSY"

    @classmethod
    def new(
        cls,
        *,
        comments: Any = None,
        concepts: Any = None,
        contacts: Any = None,
        end: Any = None,
        links: Any = None,
        organizer: Any = None,
        refids: Any = None,
        related_to: Any = None,
        request_status: Any = None,
        stamp: Any = None,
        start: Any = None,
        uid: Any = None,
        url: Optional[str] = None,
    ) -> FreeBusy:
        fb = cls()
        _apply_common_new_kwargs(
            fb,
            comments=comments,
            concepts=concepts,
            contacts=contacts,
            links=links,
            organizer=organizer,
            refids=refids,
            related_to=related_to,
            request_status=request_status,
            stamp=stamp,
            uid=uid,
            url=url,
        )
        if start is not None:
            fb.add("DTSTART", start)
        if end is not None:
            fb.add("DTEND", end)
        return fb

    @classmethod
    def example(cls, name: str = "example") -> FreeBusy:
        _ = name
        fb = cls.new(
            uid="example-fb@icaloxide",
            start=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc),
            end=datetime.datetime(2026, 1, 1, 11, 0, tzinfo=datetime.timezone.utc),
        )
        fb.stamp()
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

    @classmethod
    def example(cls, name: str = "UTC") -> Timezone:
        return cls.from_tzid(name)

    @property
    def tz_name(self) -> Optional[str]:
        v = self.decoded("TZID", None)
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


class _TimezoneSubComponent(Component):
    """Shared properties for ``STANDARD`` and ``DAYLIGHT`` subcomponents."""

    DTSTART = _scalar_prop("DTSTART")
    TZOFFSETFROM = _scalar_prop("TZOFFSETFROM")
    TZOFFSETTO = _scalar_prop("TZOFFSETTO")
    exdates = _exdates_prop()
    rdates = _rdates_prop()
    rrules = _rrules_prop()


class TimezoneStandard(_TimezoneSubComponent):
    name = "STANDARD"


class TimezoneDaylight(_TimezoneSubComponent):
    name = "DAYLIGHT"


class Availability(_SpanMixin):
    name = "VAVAILABILITY"

    @classmethod
    def new(
        cls,
        *,
        busy_type: Optional[str] = None,
        categories: Sequence[str] = (),
        classification: Any = None,
        comments: Any = None,
        components: Optional[Sequence[Available]] = (),
        concepts: Any = None,
        contacts: Any = None,
        created: Any = None,
        description: Optional[str] = None,
        end: Any = None,
        last_modified: Any = None,
        links: Any = None,
        location: Optional[str] = None,
        organizer: Any = None,
        priority: Optional[int] = None,
        refids: Any = None,
        related_to: Any = None,
        sequence: Optional[int] = None,
        stamp: Any = None,
        start: Any = None,
        subcomponents: Optional[Iterable[Component]] = None,
        summary: Optional[str] = None,
        uid: Any = None,
        url: Optional[str] = None,
    ) -> Availability:
        av = cls()
        _apply_common_new_kwargs(
            av,
            categories=categories,
            classification=classification,
            comments=comments,
            concepts=concepts,
            contacts=contacts,
            created=created,
            description=description,
            last_modified=last_modified,
            links=links,
            location=location,
            organizer=organizer,
            priority=priority,
            refids=refids,
            related_to=related_to,
            sequence=sequence,
            stamp=stamp,
            subcomponents=subcomponents,
            uid=uid,
            url=url,
        )
        if summary is not None:
            av.add("SUMMARY", summary)
        if start is not None:
            av.add("DTSTART", start)
        if end is not None:
            av.add("DTEND", end)
        if busy_type is not None:
            av.add("BUSYTYPE", str(busy_type))
        if components:
            for item in components:
                av.add_component(item)
        return av

    @classmethod
    def example(cls, name: str = "example") -> Availability:
        _ = name
        av = cls.new(
            uid="example-avail@icaloxide",
            summary="Office Hours",
            start=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
            end=datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.timezone.utc),
            busy_type="BUSY-UNAVAILABLE",
        )
        av.stamp()
        return av

    @property
    def available(self) -> list[Available]:
        return [c for c in self.subcomponents if c.name == "AVAILABLE"]  # type: ignore[misc]

    busy_type = _scalar_prop("BUSYTYPE")


class Available(_SpanMixin):
    name = "AVAILABLE"

    @classmethod
    def new(
        cls,
        *,
        categories: Sequence[str] = (),
        comments: Any = None,
        concepts: Any = None,
        contacts: Any = None,
        created: Any = None,
        description: Optional[str] = None,
        duration: Optional[datetime.timedelta] = None,
        end: Any = None,
        last_modified: Any = None,
        links: Any = None,
        location: Optional[str] = None,
        recurrence_id: Any = None,
        refids: Any = None,
        related_to: Any = None,
        sequence: Optional[int] = None,
        stamp: Any = None,
        start: Any = None,
        summary: Optional[str] = None,
        uid: Any = None,
    ) -> Available:
        av = cls()
        _apply_common_new_kwargs(
            av,
            categories=categories,
            comments=comments,
            concepts=concepts,
            contacts=contacts,
            created=created,
            description=description,
            last_modified=last_modified,
            links=links,
            location=location,
            recurrence_id=recurrence_id,
            refids=refids,
            related_to=related_to,
            sequence=sequence,
            stamp=stamp,
            uid=uid,
        )
        if summary is not None:
            av.add("SUMMARY", summary)
        if start is not None:
            av.add("DTSTART", start)
        if end is not None:
            av.add("DTEND", end)
        if duration is not None:
            av.add("DURATION", duration)
        return av

    @classmethod
    def example(cls, name: str = "example") -> Available:
        _ = name
        av = cls.new(
            uid="example-available@icaloxide",
            summary="Morning Slot",
            start=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc),
            duration=datetime.timedelta(hours=3),
        )
        av.stamp()
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
    _parent: Optional[Component] = None,
) -> Component:
    cls = _COMPONENT_CLASS_MAP.get(comp.name, Component)
    return cls(_rust_comp=comp, _keep_recurrence=keep_recurrence, _parent=_parent)


def _wrap_rust_occurrence_fast(
    occ: _ox.Occurrence,
    keep_recurrence: bool = False,
) -> Component:
    cname = occ.name
    cls = _COMPONENT_CLASS_MAP.get(cname, Component)
    obj = cls.__new__(cls)
    obj._rust_comp = occ
    obj._rust_cal = None
    obj._keep_recurrence = keep_recurrence
    obj._parent = None
    obj._children_cache = None
    obj._props = None
    obj._subcomponents = None
    obj._errors = None
    obj._materialized = False
    obj.name = cname
    return obj


def _wrap_rust_component_or_cal(cal: _ox.Calendar) -> Component:
    return Calendar(_rust_cal=cal)


class Calendar(Component):
    """Drop-in replacement for ``icalendar.Calendar`` backed by Rust."""

    name = "VCALENDAR"

    @classmethod
    def from_ical(
        cls, st: _ox.InputData, multiple: bool = False, *, strict: bool = False
    ) -> Union[Component, list[Calendar]]:
        try:
            if multiple:
                cals = _ox.Calendar.from_ical_multiple(st, strict=strict)
                return [cls(_rust_cal=c) for c in cals]
            try:
                cal = _ox.Calendar.from_ical(st, strict=strict)
                return cls(_rust_cal=cal)
            except ValueError as inner_exc:
                if not strict and "expected VCALENDAR root component" in str(inner_exc):
                    comp = _ox.parse_component(st, strict=False)
                    return _wrap_rust_component(comp)
                raise
        except ValueError as exc:
            raise ICalParsingError(str(exc)) from exc

    @classmethod
    def example(cls, name: str = "example") -> Calendar:
        _ = name
        cal = cls.new(prodid="-//icaloxide//example//EN", name="Example Calendar")
        ev = Event.new(
            uid="example-1@icaloxide",
            summary="Example Event",
            start=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc),
            duration=datetime.timedelta(hours=1),
        )
        ev.stamp()
        cal.add_component(ev)
        return cal

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
        calscale: Optional[str] = "GREGORIAN",
        categories: Sequence[str] = (),
        color: Optional[str] = None,
        concepts: Any = None,
        description: Optional[str] = None,
        language: Optional[str] = None,
        last_modified: Any = None,
        links: Any = None,
        method: Optional[str] = None,
        name: Optional[str] = None,
        organization: Optional[str] = None,
        prodid: Optional[str] = None,
        refresh_interval: Optional[datetime.timedelta] = None,
        refids: Any = None,
        related_to: Any = None,
        source: Optional[str] = None,
        subcomponents: Optional[Iterable[Component]] = None,
        uid: Any = None,
        url: Optional[str] = None,
        version: str = "2.0",
    ) -> Calendar:
        effective_prodid = prodid
        if effective_prodid is None and organization is not None:
            effective_prodid = f"-//{organization}//{name or 'icaloxide'}//{language or 'EN'}"
        cal = cls(_rust_cal=_ox.Calendar.new(prodid=effective_prodid, name=name))
        if calscale is not None and calscale != "GREGORIAN":
            cal["CALSCALE"] = calscale
        elif calscale is None:
            cal.pop("CALSCALE", None)
        if version != "2.0":
            cal["VERSION"] = version
        if method is not None:
            cal["METHOD"] = method
        if refresh_interval is not None:
            cal["REFRESH-INTERVAL"] = refresh_interval
        if source is not None:
            cal["SOURCE"] = source
        if language is not None:
            cal["LANGUAGE"] = language
        if any(
            x is not None and x != ()
            for x in (
                categories,
                color,
                concepts,
                description,
                last_modified,
                links,
                refids,
                related_to,
                subcomponents,
                uid,
                url,
            )
        ):
            _apply_common_new_kwargs(
                cal,
                categories=categories,
                color=color,
                concepts=concepts,
                description=description,
                last_modified=last_modified,
                links=links,
                refids=refids,
                related_to=related_to,
                subcomponents=subcomponents,
                uid=uid,
                url=url,
            )
        return cal

    calendar_name = _scalar_prop("NAME", fallback_key="X-WR-CALNAME")
    prodid = _scalar_prop("PRODID")
    version = _scalar_prop("VERSION")
    calscale = _scalar_prop("CALSCALE")
    method = _scalar_prop("METHOD")
    color = _scalar_prop("COLOR")
    description = _scalar_prop("DESCRIPTION", fallback_key="X-WR-CALDESC")
    categories = _categories_prop("CATEGORIES")
    source = _scalar_prop("SOURCE")
    refresh_interval = _scalar_prop("REFRESH-INTERVAL")
    url = _scalar_prop("URL")
    images = _multi_prop("IMAGE", use_get=True, wrap=Image)
    DTSTART = _scalar_prop("DTSTART")
    DTEND = _scalar_prop("DTEND")
    DUE = _scalar_prop("DUE")
    DURATION = _scalar_prop("DURATION")
    RECURRENCE_ID = _scalar_prop("RECURRENCE-ID")
    REQUEST_STATUS = _scalar_prop("REQUEST-STATUS")
    X_MOZ_LASTACK = _scalar_prop("X-MOZ-LASTACK")
    X_MOZ_SNOOZE_TIME = _scalar_prop("X-MOZ-SNOOZE-TIME")

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
        return [c for c in self.subcomponents if c.name == "VEVENT"]  # type: ignore[misc]

    @property
    def todos(self) -> list[Todo]:
        return [c for c in self.subcomponents if c.name == "VTODO"]  # type: ignore[misc]

    @property
    def journals(self) -> list[Journal]:
        return [c for c in self.subcomponents if c.name == "VJOURNAL"]  # type: ignore[misc]

    @property
    def timezones(self) -> list[Timezone]:
        return [c for c in self.subcomponents if c.name == "VTIMEZONE"]  # type: ignore[misc]

    @property
    def freebusy(self) -> list[FreeBusy]:
        return [c for c in self.subcomponents if c.name == "VFREEBUSY"]  # type: ignore[misc]

    @property
    def availabilities(self) -> list[Availability]:
        return [c for c in self.subcomponents if c.name == "VAVAILABILITY"]  # type: ignore[misc]

    def with_uid(self, uid: str) -> list[Component]:
        return [c for c in self.walk() if c.decoded("UID", None) == uid]

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

from icaloxide.compat.alarms import Alarms, AlarmTime  # noqa: E402
from icaloxide.compat.enums import (  # noqa: E402
    BUSYTYPE,
    CLASS,
    CUTYPE,
    FBTYPE,
    PARTSTAT,
    RANGE,
    RELATED,
    RELTYPE,
    ROLE,
    STATUS,
    TRANSP,
    VALUE,
)
from icaloxide.compat.errors import (  # noqa: E402
    BadRuleStringFormat,
    BrokenCalendarProperty,
    ComponentEndMissing,
    ComponentStartMissing,
    FeatureWillBeRemovedInFutureVersion,
    GloballyUniqueTZIDGuessed,
    IncompleteAlarmInformation,
    IncompleteComponent,
    InvalidCalendar,
    LocalTimezoneMissing,
    PeriodEndBeforeStart,
)
from icaloxide.compat.props_extra import (  # noqa: E402
    AdrFields,
    ComponentFactory,
    NFields,
    TypesFactory,
    vAdr,
    vN,
    vOrg,
)

Component.types_factory = TypesFactory()  # type: ignore[attr-defined]
__version__ = str(getattr(_ox, "__version__", "0.5.1"))
version = __version__
version_tuple = tuple(int(p) for p in __version__.split(".")[:3] if p.isdigit())
__version_tuple__ = version_tuple
VPROPERTY = Union[
    vAdr,
    vBinary,
    vBoolean,
    vBroken,
    vCalAddress,
    vCategory,
    vDDDLists,
    vDDDTypes,
    vDate,
    vDatetime,
    vDuration,
    vFloat,
    vFrequency,
    vGeo,
    vInt,
    vMonth,
    vN,
    vOrg,
    vPeriod,
    vRecur,
    vSkip,
    vText,
    vTime,
    vUTCOffset,
    vUid,
    vUnknown,
    vUri,
    vWeekday,
    vXmlReference,
]


__all__ = [
    "AdrFields",
    "Alarm",
    "AlarmTime",
    "Alarms",
    "Availability",
    "Available",
    "BUSYTYPE",
    "BadRuleStringFormat",
    "BrokenCalendarProperty",
    "CLASS",
    "CUTYPE",
    "Calendar",
    "Component",
    "ComponentEndMissing",
    "ComponentFactory",
    "ComponentStartMissing",
    "Conference",
    "Event",
    "FBTYPE",
    "FeatureWillBeRemovedInFutureVersion",
    "FreeBusy",
    "GloballyUniqueTZIDGuessed",
    "ICalParsingError",
    "Image",
    "IncompleteAlarmInformation",
    "IncompleteComponent",
    "InvalidCalendar",
    "JCalParsingError",
    "Journal",
    "LazyCalendar",
    "LocalTimezoneMissing",
    "NFields",
    "PARTSTAT",
    "Parameters",
    "PeriodEndBeforeStart",
    "RANGE",
    "RELATED",
    "RELTYPE",
    "ROLE",
    "STATUS",
    "TRANSP",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "Todo",
    "TypesFactory",
    "VALUE",
    "VPROPERTY",
    "__version__",
    "__version_tuple__",
    "is_utc",
    "q_join",
    "q_split",
    "use_pytz",
    "use_zoneinfo",
    "vAdr",
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
    "vN",
    "vOrg",
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
    "version",
    "version_tuple",
]
