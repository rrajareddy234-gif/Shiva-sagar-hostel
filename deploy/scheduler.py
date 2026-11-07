#!/usr/bin/env python3
"""Run the scheduled jobs and WhatsApp the owner.

This replaces n8n. n8n earned its place for three jobs only: run on a schedule,
call an endpoint, and forward a message. Those three are a loop and two HTTP
calls, and this does them in the same language and the same test suite as the
rest of the system. In exchange the deployment loses a ~400 MB container, a
second web login and a second set of credentials to keep safe.

The message text is not built here. It comes from ``core/reports.py``, which is
tested, and reaches the owner through ``POST /jobs/notify-owner`` so the app
keeps sole ownership of the WhatsApp credentials and every notification is
recorded in the database.

The decision of *which* job is due is a pure function, ``due_jobs``, so the
timing can be tested without waiting for a clock.

Run it in the background:

    nohup python deploy/scheduler.py > /tmp/sagarpg-scheduler.log 2>&1 &

Or let ``deploy/docker-compose.lite.yml`` run it as a container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import reports  # noqa: E402  (needs ROOT on sys.path first)


@dataclass(frozen=True)
class Job:
    """One scheduled call.

    ``every_minutes`` and ``at`` are mutually exclusive: a job either repeats on
    an interval or fires once a day at a wall-clock time.
    """

    name: str
    path: str
    every_minutes: int | None = None
    at: str | None = None

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        """Is this job due at ``now``, given when it last ran?"""
        if self.every_minutes is not None:
            if last_run is None:
                return True
            return now >= last_run + timedelta(minutes=self.every_minutes)

        if self.at is not None:
            hour, minute = (int(part) for part in self.at.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Before today's slot: not yet due.
            if now < target:
                return False
            # Already ran today at or after today's slot: done.
            if last_run is not None and last_run >= target:
                return False
            return True

        return False


JOBS: tuple[Job, ...] = (
    Job("health-check", "/jobs/health-check", every_minutes=10),
    Job("pending-requests", "/jobs/pending-requests", every_minutes=15),
    Job("expire-holds", "/jobs/expire-holds", every_minutes=15),
    Job("unanswered-enquiries", "/jobs/unanswered-enquiries", every_minutes=30),
    Job("daily-report", "/jobs/daily-report", at="07:30"),
    Job("upcoming", "/jobs/upcoming", at="20:00"),
)


def due_jobs(now: datetime, last_runs: dict[str, datetime],
             jobs: tuple[Job, ...] = JOBS) -> list[Job]:
    """Every job that should run now, in a stable order."""
    return [job for job in jobs if job.due(now, last_runs.get(job.name))]


class Scheduler:
    def __init__(self, app_url: str, token: str, timeout: float = 30.0,
                 notify: bool = True) -> None:
        self.app_url = app_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.notify = notify

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Job-Token": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read().decode() or "{}"
        return json.loads(raw)

    def fetch(self, job: Job) -> dict:
        """Call the job endpoint and return its JSON.

        A non-200 status is not automatically a failure: /jobs/health-check
        answers 503 *with a useful body* when something is wrong, and that body
        carries the message the owner needs. So the error body is parsed first,
        and only an unparseable one becomes a generic error.
        """
        try:
            return self._request("GET", f"{self.app_url}{job.path}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="ignore")
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and payload:
                    return payload
            except ValueError:
                pass
            return {"error": f"{job.name} returned HTTP {exc.code}: {body[:300]}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"error": f"{job.name} could not be reached: {exc}"}

    def notify_owner(self, text: str) -> bool:
        try:
            self._request("POST", f"{self.app_url}/jobs/notify-owner", {"text": text})
            return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"notify failed: {exc}", file=sys.stderr)
            return False

    def run_job(self, job: Job) -> None:
        """Fetch one job and message the owner if there is anything to say.

        ``text`` is None when the job has nothing worth reporting — a healthy
        system, or an empty day. That is silence by design, not a failure. An
        error, though, always becomes a message: the owner has to hear about a
        job that stopped working.
        """
        payload = self.fetch(job)
        text = payload.get("text")
        if not text and payload.get("error"):
            text = reports.alert_text(str(payload["error"]))
        if not text:
            print(f"{job.name}: nothing to report")
            return
        if self.notify:
            print(f"{job.name}: notifying the owner")
            self.notify_owner(text)
        else:
            print(f"{job.name}: would notify the owner (--no-notify):\n{text}")


def _load_last_runs(path: Path) -> dict[str, datetime]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    runs = {}
    for name, stamp in raw.items():
        try:
            runs[name] = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
    return runs


def _save_last_runs(path: Path, runs: dict[str, datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.isoformat() for k, v in runs.items()}, indent=2))
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default=os.environ.get("SAGAR_APP_URL",
                                                            "http://127.0.0.1:12000"))
    parser.add_argument("--token", default=os.environ.get("JOB_TOKEN", ""))
    parser.add_argument("--interval", type=float,
                        default=float(os.environ.get("SCHEDULER_INTERVAL", "60")),
                        help="seconds between checks")
    parser.add_argument("--timezone", default=os.environ.get("HOSTEL_TIMEZONE",
                                                             "Asia/Kolkata"))
    parser.add_argument("--state", default=os.environ.get(
        "SCHEDULER_STATE_FILE", "/tmp/sagarpg-scheduler/last-runs.json"))
    parser.add_argument("--no-notify", action="store_true",
                        help="run the jobs, print the text, send nothing")
    parser.add_argument("--once", action="store_true",
                        help="run every due job once and exit (for testing)")
    args = parser.parse_args()

    if not args.token and not args.no_notify:
        print("JOB_TOKEN is not set; the app will refuse every job call",
              file=sys.stderr)
        return 2

    tz = ZoneInfo(args.timezone)
    state_path = Path(args.state)
    scheduler = Scheduler(args.app_url, args.token, notify=not args.no_notify)
    last_runs = _load_last_runs(state_path)

    print(f"scheduler watching {args.app_url}, every {args.interval:.0f}s, "
          f"timezone {args.timezone}")

    while True:
        now = datetime.now(tz)
        for job in due_jobs(now, last_runs):
            scheduler.run_job(job)
            # Recorded after the run so a crash mid-job retries it next tick
            # rather than skipping it for a whole interval.
            last_runs[job.name] = now
            _save_last_runs(state_path, last_runs)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
