"""Drop-in compatibility layer mimicking Python's ``icalendar`` package (v7.x)."""

from __future__ import annotations

import copy
import datetime
import json
from typing import Any, Iterable, Iterator, Optional, Sequence, Union

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
        if key in ("RRULE", "ATTACH", "URL", "UID", "ATTENDEE", "ORGANIZER"):
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

            dt = dt.replace(tzinfo=ZoneInfo(str(params["TZID"])))
        except Exception:
            pass
    return dt


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


class Alarm(Component):
    name = "VALARM"

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
    def repeat(self) -> int:
        return int(self.decoded("REPEAT", 0) or 0)

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        return self.decoded("DURATION")


class FreeBusy(Component):
    name = "VFREEBUSY"


class Timezone(Component):
    name = "VTIMEZONE"


class TimezoneStandard(Component):
    name = "STANDARD"


class TimezoneDaylight(Component):
    name = "DAYLIGHT"


class Availability(Component):
    name = "VAVAILABILITY"


class Available(_SpanMixin):
    name = "AVAILABLE"


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
    ) -> Calendar:
        return cls(_rust_cal=_ox.Calendar.new(prodid=prodid, name=name))

    @property
    def calendar_name(self) -> Optional[str]:
        return self.decoded("NAME") or self.decoded("X-WR-CALNAME")

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


__all__ = [
    "Alarm",
    "Availability",
    "Available",
    "Calendar",
    "Component",
    "Event",
    "FreeBusy",
    "Journal",
    "Parameters",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "Todo",
    "vBinary",
    "vBoolean",
    "vCalAddress",
    "vCategory",
    "vDDDTypes",
    "vDate",
    "vDatetime",
    "vDuration",
    "vFloat",
    "vGeo",
    "vInt",
    "vPeriod",
    "vRecur",
    "vText",
    "vTime",
    "vUTCOffset",
    "vUri",
]
