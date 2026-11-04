#!/usr/bin/env python3
"""Show what happened around a given time, without reading the whole codebase.

    python scripts/failures.py                 # the last 10 recorded events
    python scripts/failures.py --since 14:30   # today, from 14:30 onwards
    python scripts/failures.py --at 14:32      # a 10-minute window around 14:32
    python scripts/failures.py --outages       # only the down/up lines
    python scripts/failures.py --app-log       # tail the app log too

The watchdog writes a JSON line per event to events.jsonl and a plain line per
outage to outages.log. This reads those and prints the window you asked for, so
a failure can be found by its timestamp instead of by trawling the source.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(__import__("os").environ.get("WATCHDOG_STATE_DIR", "/tmp/sagarpg-watchdog"))
EVENTS_FILE = STATE_DIR / "events.jsonl"
OUTAGE_LOG = STATE_DIR / "outages.log"
APP_LOG = Path("/tmp/sagarpg-app.log")


def _parse_when(value: str, *, base: datetime) -> datetime:
    """Accept '14:32', '2026-11-04 14:32' or an ISO timestamp."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=base.year, month=base.month, day=base.day)
        return parsed.astimezone()
    return datetime.fromisoformat(value).astimezone()


def load_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    events = []
    for line in EVENTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def print_events(events: list[dict]) -> None:
    if not events:
        print("No recorded events. Is the watchdog running?")
        return
    for event in events:
        when = event.get("at", "?")
        kind = event.get("event", "?")
        detail = {k: v for k, v in event.items() if k not in ("at", "event")}
        summary = " ".join(f"{k}={v}" for k, v in detail.items())
        print(f"{when}  {kind:16} {summary}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="from this time today, e.g. 14:30")
    parser.add_argument("--at", help="a 10-minute window around this time, e.g. 14:32")
    parser.add_argument("--last", type=int, default=10, help="how many recent events")
    parser.add_argument("--outages", action="store_true", help="only the outage lines")
    parser.add_argument("--app-log", action="store_true", help="also tail the app log")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).astimezone()

    if args.outages:
        if not OUTAGE_LOG.exists():
            print("No outages recorded. The app has stayed up.")
            return 0
        print(OUTAGE_LOG.read_text().rstrip())
        return 0

    events = load_events()

    if args.at:
        centre = _parse_when(args.at, base=now)
        low, high = centre - timedelta(minutes=5), centre + timedelta(minutes=5)
        events = [e for e in events
                  if low <= datetime.fromisoformat(e["at"]) <= high]
        print(f"Events between {low:%H:%M} and {high:%H:%M}:")
    elif args.since:
        start = _parse_when(args.since, base=now)
        events = [e for e in events if datetime.fromisoformat(e["at"]) >= start]
        print(f"Events since {start:%H:%M}:")
    else:
        events = events[-args.last:]
        print(f"Last {len(events)} events:")

    print_events(events)

    if args.app_log and APP_LOG.exists():
        print("\nApp log (last 20 lines):")
        print("\n".join(APP_LOG.read_text().splitlines()[-20:]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
