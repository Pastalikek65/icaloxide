"""Submodule compatibility shim mimicking ``icalendar.param``."""

from icaloxide.compat.icalendar import Parameters, q_join, q_split

__all__ = ["Parameters", "q_join", "q_split"]
