"""Exception and warning hierarchy matching ``icalendar`` and ``recurring_ical_events``."""

from __future__ import annotations


class InvalidCalendar(ValueError):
    """Base error raised for invalid iCalendar / jCal / recurrence data."""


class ICalParsingError(InvalidCalendar):
    """Raised when an iCalendar byte or text stream fails strict parsing."""


class JCalParsingError(InvalidCalendar):
    """Raised when RFC 7265 jCal parsing fails."""


class BrokenCalendarProperty(InvalidCalendar):
    """Raised when a property has an unrecoverable invalid value."""


class BadRuleStringFormat(InvalidCalendar):
    """Raised when an RRULE string cannot be parsed."""


class PeriodEndBeforeStart(InvalidCalendar):
    """Raised when a recurrence PERIOD or query end precedes its start."""


class IncompleteComponent(ValueError):
    """Raised when a component lacks mandatory fields such as DTSTART."""


class IncompleteAlarmInformation(ValueError):
    """Base error raised when alarm timing cannot be resolved."""


class ComponentStartMissing(IncompleteAlarmInformation):
    """Raised when a relative alarm lacks parent start time."""


class ComponentEndMissing(IncompleteAlarmInformation):
    """Raised when a relative-to-end alarm lacks parent end time."""


class LocalTimezoneMissing(IncompleteAlarmInformation):
    """Raised when a floating trigger time requires a local timezone."""


class GloballyUniqueTZIDGuessed(UserWarning):
    """Warning emitted when guessing an IANA timezone from a custom TZID."""


class FeatureWillBeRemovedInFutureVersion(DeprecationWarning):
    """Deprecation warning matching ``icalendar.FeatureWillBeRemovedInFutureVersion``."""


__all__ = [
    "BadRuleStringFormat",
    "BrokenCalendarProperty",
    "ComponentEndMissing",
    "ComponentStartMissing",
    "FeatureWillBeRemovedInFutureVersion",
    "GloballyUniqueTZIDGuessed",
    "ICalParsingError",
    "IncompleteAlarmInformation",
    "IncompleteComponent",
    "InvalidCalendar",
    "JCalParsingError",
    "LocalTimezoneMissing",
    "PeriodEndBeforeStart",
]
