"""Command-line interface for ``icaloxide``."""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Optional, Sequence

import icaloxide
from icaloxide.compat import Calendar


def _parse_cli_date(raw: str) -> Union_dt:
    s = raw.strip()
    if "T" in s or "t" in s:
        if s.endswith(("Z", "z")):
            return datetime.datetime.fromisoformat(s[:-1]).replace(tzinfo=datetime.timezone.utc)
        return datetime.datetime.fromisoformat(s)
    return datetime.date.fromisoformat(s)


Union_dt = object


def _cmd_view(args: argparse.Namespace) -> int:
    cal = icaloxide.Calendar.from_ical(Path(args.file), strict=args.strict)
    print(f"Calendar: {cal.name or '(unnamed)'} ({cal.prodid or 'no PRODID'})")
    print(f"Stats: {cal.component_stats()}")
    for ev in cal.events:
        start = ev.get("DTSTART")
        summary = ev.summary or "(no summary)"
        print(f"  [VEVENT] {start}  {summary}  uid={ev.uid or ''}")
    for td in cal.todos:
        due = td.get("DUE") or td.get("DTSTART")
        summary = td.summary or "(no summary)"
        print(f"  [VTODO]  {due}  {summary}  status={td.status or ''}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        cal = icaloxide.Calendar.from_ical(Path(args.file), strict=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    issues = cal.validate()
    missing_tz = cal.missing_tzids()
    for line, msg in issues:
        print(f"line {line}: {msg}", file=sys.stderr)
    for tz in missing_tz:
        print(f"missing VTIMEZONE for TZID: {tz}", file=sys.stderr)
    if issues or (args.require_vtimezones and missing_tz):
        return 1
    print("OK")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    cal = icaloxide.Calendar.from_ical(Path(args.file))
    comps = [c.strip() for c in args.components.split(",") if c.strip()]
    q = cal.query(components=comps, skip_cancelled=args.skip_cancelled)
    start = _parse_cli_date(args.between[0])
    end = _parse_cli_date(args.between[1])
    if args.busy:
        for b_start, b_end in q.busy_intervals(start, end):  # type: ignore[arg-type]
            print(f"{b_start.isoformat()} -> {b_end.isoformat()}")
        return 0
    for occ in q.between(start, end):  # type: ignore[arg-type]
        s_str = occ.start.isoformat() if occ.start is not None else "(undated)"
        e_str = occ.end.isoformat() if occ.end is not None else "(undated)"
        print(f"[{occ.name}] {s_str} .. {e_str} | {occ.summary or ''}")
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    cal = Calendar.from_ical(Path(args.file))
    fmt = args.to.lower()
    if fmt == "ical":
        sys.stdout.write(cal.to_ical().decode("utf-8"))
    elif fmt == "jcal":
        print(cal.to_json())
    elif fmt == "xcal":
        print(cal.to_xcal())
    else:
        print(f"unsupported format: {args.to}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icaloxide", description="Fast Rust iCalendar CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_view = sub.add_parser("view", help="Display summary of events and todos")
    p_view.add_argument("file", help="Path to .ics file")
    p_view.add_argument("--strict", action="store_true", help="Use strict RFC 5545 parsing")
    p_view.set_defaults(func=_cmd_view)

    p_val = sub.add_parser("validate", help="Validate iCalendar file strictly")
    p_val.add_argument("file", help="Path to .ics file")
    p_val.add_argument("--require-vtimezones", action="store_true")
    p_val.set_defaults(func=_cmd_validate)

    p_q = sub.add_parser("query", help="Expand occurrences between dates")
    p_q.add_argument("file", help="Path to .ics file")
    p_q.add_argument("--between", nargs=2, required=True, metavar=("START", "END"))
    p_q.add_argument("--components", default="VEVENT")
    p_q.add_argument("--skip-cancelled", action="store_true")
    p_q.add_argument("--busy", action="store_true", help="Print merged busy UTC intervals")
    p_q.set_defaults(func=_cmd_query)

    p_conv = sub.add_parser("convert", help="Convert .ics to ical, jcal, or xcal")
    p_conv.add_argument("file", help="Path to .ics file")
    p_conv.add_argument("--to", required=True, choices=["ical", "jcal", "xcal"])
    p_conv.set_defaults(func=_cmd_convert)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
