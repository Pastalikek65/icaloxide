"""Structured property types and factory mappings matching ``icalendar 7.x``."""

from __future__ import annotations

from typing import Any, NamedTuple, Optional, Union

from icaloxide.compat.icalendar import (
    _COMPONENT_CLASS_MAP,
    Alarm,
    Availability,
    Available,
    Calendar,
    Component,
    Event,
    FreeBusy,
    Journal,
    Parameters,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
    Todo,
    _escape_text,
    vBinary,
    vBoolean,
    vBroken,
    vCalAddress,
    vCategory,
    vDate,
    vDatetime,
    vDDDLists,
    vDDDTypes,
    vDuration,
    vFloat,
    vFrequency,
    vGeo,
    vInt,
    vMonth,
    vPeriod,
    vRecur,
    vSkip,
    vText,
    vTime,
    vUid,
    vUnknown,
    vUri,
    vUTCOffset,
    vWeekday,
    vXmlReference,
)


def _split_semicolons(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    it = iter(text)
    for ch in it:
        if ch == ";":
            out.append("".join(buf))
            buf.clear()
        elif ch == "\\":
            nxt = next(it, "")
            if nxt in ("n", "N"):
                buf.append("\n")
            elif nxt in (";", ",", "\\"):
                buf.append(nxt)
            elif nxt:
                buf.append("\\" + nxt)
            else:
                buf.append("\\")
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


class AdrFields(NamedTuple):
    po_box: str = ""
    extended: str = ""
    street: str = ""
    locality: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""


class NFields(NamedTuple):
    family: str = ""
    given: str = ""
    additional: str = ""
    prefix: str = ""
    suffix: str = ""


class vAdr:
    """RFC 6350/9073 structured postal address matching ``icalendar.vAdr``."""

    def __init__(
        self,
        fields: Union[AdrFields, tuple[str, ...], list[str], str],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        if isinstance(fields, str):
            parts = _split_semicolons(fields)
        else:
            parts = [str(x) for x in fields]
        while len(parts) < 7:
            parts.append("")
        self.fields = AdrFields(*parts[:7])
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return ";".join(_escape_text(x) for x in self.fields).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> AdrFields:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return cls(text).fields

    def to_jcal(self, name: str = "adr") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "text", *self.fields]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vAdr:
        params = Parameters.from_jcal(jcal_prop[1]) if len(jcal_prop) > 1 else Parameters()
        return cls(jcal_prop[3:], params=params)


class vN:
    """Structured person name matching ``icalendar.vN``."""

    def __init__(
        self,
        fields: Union[NFields, tuple[str, ...], list[str], str],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        if isinstance(fields, str):
            parts = _split_semicolons(fields)
        else:
            parts = [str(x) for x in fields]
        while len(parts) < 5:
            parts.append("")
        self.fields = NFields(*parts[:5])
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return ";".join(_escape_text(x) for x in self.fields).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> NFields:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return cls(text).fields

    def to_jcal(self, name: str = "n") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "text", *self.fields]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vN:
        params = Parameters.from_jcal(jcal_prop[1]) if len(jcal_prop) > 1 else Parameters()
        return cls(jcal_prop[3:], params=params)


class vOrg:
    """Structured organization hierarchy matching ``icalendar.vOrg``."""

    def __init__(
        self,
        fields: Union[tuple[str, ...], list[str], str],
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        if isinstance(fields, str):
            self.fields = tuple(_split_semicolons(fields))
        else:
            self.fields = tuple(str(x) for x in fields)
        self.params = Parameters(params) if params else Parameters()

    def to_ical(self) -> bytes:
        return ";".join(_escape_text(x) for x in self.fields).encode("utf-8")

    @classmethod
    def from_ical(cls, raw: Union[str, bytes]) -> tuple[str, ...]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return cls(text).fields

    def to_jcal(self, name: str = "org") -> list[Any]:
        return [name.lower(), self.params.to_jcal(), "text", *self.fields]

    @classmethod
    def from_jcal(cls, jcal_prop: list[Any]) -> vOrg:
        params = Parameters.from_jcal(jcal_prop[1]) if len(jcal_prop) > 1 else Parameters()
        return cls(jcal_prop[3:], params=params)


class ComponentFactory(dict):  # type: ignore[type-arg]
    """Component registry matching ``icalendar.ComponentFactory``."""

    def __init__(self) -> None:
        super().__init__(
            {
                "VCALENDAR": Calendar,
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
                **{k: v for k, v in _COMPONENT_CLASS_MAP.items()},
            }
        )

    def get_component_class(self, name: str) -> type[Component]:
        return self.get(str(name).upper(), Component)


class TypesFactory(dict):  # type: ignore[type-arg]
    """Property value type factory matching ``icalendar.TypesFactory``."""

    def __init__(self) -> None:
        super().__init__(
            {
                "binary": vBinary,
                "boolean": vBoolean,
                "cal-address": vCalAddress,
                "date": vDate,
                "date-time": vDatetime,
                "duration": vDuration,
                "float": vFloat,
                "geo": vGeo,
                "integer": vInt,
                "period": vPeriod,
                "recur": vRecur,
                "text": vText,
                "time": vTime,
                "uri": vUri,
                "utc-offset": vUTCOffset,
                "categories": vCategory,
                "ddd-lists": vDDDLists,
                "ddd-types": vDDDTypes,
                "weekday": vWeekday,
                "frequency": vFrequency,
                "month": vMonth,
                "skip": vSkip,
                "uid": vUid,
                "broken": vBroken,
                "unknown": vUnknown,
                "xml-reference": vXmlReference,
                "adr": vAdr,
                "n": vN,
                "org": vOrg,
            }
        )

    def for_property(self, name: str) -> Any:
        key = str(name).upper()
        prop_types = {
            "SUMMARY": vText,
            "DESCRIPTION": vText,
            "LOCATION": vText,
            "UID": vUid,
            "DTSTART": vDDDTypes,
            "DTEND": vDDDTypes,
            "DUE": vDDDTypes,
            "DTSTAMP": vDDDTypes,
            "CREATED": vDDDTypes,
            "LAST-MODIFIED": vDDDTypes,
            "DURATION": vDuration,
            "RRULE": vRecur,
            "RDATE": vDDDLists,
            "EXDATE": vDDDLists,
            "FREEBUSY": vPeriod,
            "GEO": vGeo,
            "ATTENDEE": vCalAddress,
            "ORGANIZER": vCalAddress,
            "URL": vUri,
            "CATEGORIES": vCategory,
            "ATTACH": vBinary,
            "TZOFFSETFROM": vUTCOffset,
            "TZOFFSETTO": vUTCOffset,
            "PRIORITY": vInt,
            "SEQUENCE": vInt,
            "PERCENT-COMPLETE": vInt,
        }
        return prop_types.get(key, vText)


__all__ = [
    "AdrFields",
    "ComponentFactory",
    "NFields",
    "TypesFactory",
    "vAdr",
    "vN",
    "vOrg",
]
