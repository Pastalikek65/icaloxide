"""Submodule compatibility shim mimicking ``icalendar.cal``."""

from icaloxide.compat.icalendar import (
    Alarm,
    Availability,
    Available,
    Calendar,
    Component,
    ComponentFactory,
    Event,
    FreeBusy,
    Journal,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
    Todo,
)

__all__ = [
    "Alarm",
    "Availability",
    "Available",
    "Calendar",
    "Component",
    "ComponentFactory",
    "Event",
    "FreeBusy",
    "Journal",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "Todo",
]
