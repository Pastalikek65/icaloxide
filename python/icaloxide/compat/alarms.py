"""RFC 9074 Alarm calculation and snooze/acknowledge tracking matching ``icalendar.alarms``."""

from __future__ import annotations

import datetime
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from icaloxide.compat.errors import (
    ComponentEndMissing,
    ComponentStartMissing,
    IncompleteAlarmInformation,
    LocalTimezoneMissing,
)


def _as_utc(dt: Union_dt, local_tz: Optional[datetime.tzinfo] = None) -> datetime.datetime:
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            if local_tz is None:
                raise LocalTimezoneMissing(
                    "floating alarm/component datetime requires Alarms.set_local_timezone()"
                )
            return dt.replace(tzinfo=local_tz).astimezone(datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    if isinstance(dt, datetime.date):
        base = datetime.datetime.combine(dt, datetime.time.min)
        tz = local_tz if local_tz is not None else datetime.timezone.utc
        return base.replace(tzinfo=tz).astimezone(datetime.timezone.utc)
    raise TypeError(f"expected date or datetime, got {type(dt).__name__}")


Union_dt = Any


def _add_span(base: Any, delta: datetime.timedelta) -> Any:
    if isinstance(base, datetime.datetime):
        return base + delta
    if isinstance(base, datetime.date):
        return datetime.datetime.combine(base, datetime.time.min) + delta
    raise IncompleteAlarmInformation(f"invalid alarm anchor type: {type(base).__name__}")


class AlarmTime:
    """Computed alarm trigger occurrence with RFC 9074 / Thunderbird ack & snooze state."""

    def __init__(
        self,
        alarm: Any,
        trigger: datetime.datetime,
        acknowledged_until: Optional[datetime.datetime] = None,
        snoozed_until: Optional[datetime.datetime] = None,
        parent: Any = None,
    ) -> None:
        self._alarm = alarm
        self._parent = parent
        self._trigger = trigger
        self._acknowledged_until = acknowledged_until
        self._snoozed_until = snoozed_until

    @property
    def alarm(self) -> Any:
        return self._alarm

    @property
    def action(self) -> Optional[str]:
        return getattr(self._alarm, "action", None) or (
            self._alarm.decoded("ACTION") if hasattr(self._alarm, "decoded") else None
        )

    @property
    def parent(self) -> Any:
        return self._parent

    @property
    def trigger(self) -> datetime.datetime:
        return self._trigger

    @property
    def acknowledged(self) -> Optional[datetime.datetime]:
        return self._acknowledged_until

    @property
    def snoozed(self) -> Optional[datetime.datetime]:
        return self._snoozed_until

    def is_active(self) -> bool:
        """Return ``True`` if this alarm has not been acknowledged past its active trigger."""
        ack_prop = None
        if hasattr(self._alarm, "decoded"):
            ack_prop = self._alarm.decoded("ACKNOWLEDGED")
        trigger_utc = _as_utc(self._trigger, getattr(self._trigger, "tzinfo", None))

        ack_candidates: list[datetime.datetime] = []
        if self._acknowledged_until is not None:
            ack_candidates.append(_as_utc(self._acknowledged_until))
        if isinstance(ack_prop, (datetime.date, datetime.datetime)):
            ack_candidates.append(_as_utc(ack_prop))

        effective_trigger = trigger_utc
        if self._snoozed_until is not None:
            snooze_utc = _as_utc(self._snoozed_until)
            if snooze_utc > effective_trigger:
                effective_trigger = snooze_utc

        if not ack_candidates:
            return True
        latest_ack = max(ack_candidates)
        return latest_ack < effective_trigger

    def __repr__(self) -> str:
        return f"AlarmTime(trigger={self._trigger!r}, active={self.is_active()!r})"


class Alarms:
    """Compute trigger times and active/acknowledged/snoozed states across alarms."""

    def __init__(self, component: Any = None) -> None:
        self._alarms: list[Any] = []
        self._start_alarms: list[Any] = []
        self._end_alarms: list[Any] = []
        self._absolute_alarms: list[Any] = []
        self._parent: Any = None
        self._start: Any = None
        self._end: Any = None
        self._last_ack: Optional[datetime.datetime] = None
        self._snooze_until: Optional[datetime.datetime] = None
        self._local_tzinfo: Optional[datetime.tzinfo] = None
        if component is not None:
            self.add_component(component)

    def __len__(self) -> int:
        return len(self._alarms)

    def __getitem__(self, index: Any) -> Any:
        return self._alarms[index]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._alarms)

    def add_component(self, component: Any) -> None:
        name = getattr(component, "name", "").upper()
        if name == "VALARM":
            self.add_alarm(component)
            return
        self.set_parent(component)
        if name in ("VEVENT", "VTODO"):
            start = component.decoded("DTSTART")
            if start is not None:
                self.set_start(start)
            end_key = "DUE" if name == "VTODO" and "DUE" in component else "DTEND"
            end = component.decoded(end_key)
            if end is None and start is not None:
                dur = component.decoded("DURATION")
                if isinstance(dur, datetime.timedelta):
                    end = _add_span(start, dur)
            if end is not None:
                self.set_end(end)
            last_ack = component.decoded("X-MOZ-LASTACK")
            if isinstance(last_ack, (datetime.date, datetime.datetime)):
                self.acknowledge_until(last_ack)
            snooze = component.decoded("X-MOZ-SNOOZE-TIME")
            if isinstance(snooze, (datetime.date, datetime.datetime)):
                self.snooze_until(snooze)
        for sub in getattr(component, "subcomponents", ()):
            self.add_component(sub)

    def add_alarm(self, alarm: Any) -> None:
        self._alarms.append(alarm)
        if "TRIGGER" not in alarm:
            return
        trig = alarm["TRIGGER"]
        trig_val = getattr(trig, "dt", trig)
        params = getattr(trig, "params", {})
        if isinstance(trig_val, (datetime.date, datetime.datetime)):
            self._absolute_alarms.append(alarm)
            return
        if isinstance(trig_val, datetime.timedelta):
            rel = str(params.get("RELATED", "START")).strip().upper()
            if rel == "END":
                self._end_alarms.append(alarm)
            else:
                self._start_alarms.append(alarm)

    def set_parent(self, parent: Any) -> None:
        self._parent = parent

    def set_start(self, dt: Any) -> None:
        self._start = dt

    def set_end(self, dt: Any) -> None:
        self._end = dt

    def acknowledge_until(self, dt: Optional[Union_dt]) -> None:
        if dt is None:
            self._last_ack = None
        else:
            self._last_ack = _as_utc(dt, self._local_tzinfo)

    def snooze_until(self, dt: Optional[Union_dt]) -> None:
        if dt is None:
            self._snooze_until = None
        else:
            self._snooze_until = _as_utc(dt, self._local_tzinfo)

    def set_local_timezone(self, tzinfo: Union_dt) -> None:
        if tzinfo is None:
            self._local_tzinfo = None
        elif isinstance(tzinfo, str):
            self._local_tzinfo = ZoneInfo(tzinfo)
        else:
            self._local_tzinfo = tzinfo

    @staticmethod
    def _repeat_triggers(first: Any, alarm: Any) -> Iterator[Any]:
        yield first
        repeat = int(alarm.decoded("REPEAT", 0) or 0)
        dur = alarm.decoded("DURATION")
        if repeat > 0 and isinstance(dur, datetime.timedelta):
            for idx in range(1, repeat + 1):
                yield _add_span(first, dur * idx)

    def _make_alarm_time(self, alarm: Any, trigger: Any) -> AlarmTime:
        if isinstance(trigger, datetime.date) and not isinstance(trigger, datetime.datetime):
            trigger = datetime.datetime.combine(trigger, datetime.time.min)
        if getattr(trigger, "tzinfo", None) is None and self._local_tzinfo is not None:
            trigger = trigger.replace(tzinfo=self._local_tzinfo)
        return AlarmTime(
            alarm=alarm,
            trigger=trigger,
            acknowledged_until=self._last_ack,
            snoozed_until=self._snooze_until,
            parent=self._parent,
        )

    @property
    def times(self) -> list[AlarmTime]:
        out: list[AlarmTime] = []
        if self._end_alarms:
            if self._end is None:
                raise ComponentEndMissing(
                    "Use Alarms.set_end because an alarm is relative to the end of a component."
                )
            for al in self._end_alarms:
                delta = al.decoded("TRIGGER")
                for trig in self._repeat_triggers(_add_span(self._end, delta), al):
                    out.append(self._make_alarm_time(al, trig))
        if self._start_alarms:
            if self._start is None:
                raise ComponentStartMissing(
                    "Use Alarms.set_start because an alarm is relative to the start of a component."
                )
            for al in self._start_alarms:
                delta = al.decoded("TRIGGER")
                for trig in self._repeat_triggers(_add_span(self._start, delta), al):
                    out.append(self._make_alarm_time(al, trig))
        for al in self._absolute_alarms:
            abs_trig = al.decoded("TRIGGER")
            for trig in self._repeat_triggers(abs_trig, al):
                out.append(self._make_alarm_time(al, trig))
        return out

    @property
    def active(self) -> list[AlarmTime]:
        result: list[AlarmTime] = []
        for at in self.times:
            if getattr(at.trigger, "tzinfo", None) is None and self._local_tzinfo is None:
                raise LocalTimezoneMissing(
                    "floating alarm time requires Alarms.set_local_timezone() to check active status"
                )
            if at.is_active():
                result.append(at)
        return result


__all__ = ["AlarmTime", "Alarms"]
