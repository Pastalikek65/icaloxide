"""Drop-in compatibility shims for ``icalendar`` and ``recurring_ical_events``."""

from icaloxide.compat.icalendar import (
    Alarm,
    Calendar,
    Component,
    Event,
    FreeBusy,
    Journal,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
    Todo,
    vDDDTypes,
    vText,
)
from icaloxide.compat.recurring_ical_events import UnfoldableCalendar, of

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
    "UnfoldableCalendar",
    "of",
    "vDDDTypes",
    "vText",
]
