#!/usr/bin/env python3
"""Watch the app, restart it if it dies, and tell the owner when it does.

The app cannot report its own death: a process that has stopped runs no code.
So this watcher lives *outside* the app. It checks the health endpoint on a
short interval. When the app stops answering it restarts it, records the
failure, and sends the owner one WhatsApp message. It sends another when the
app answers again.

Two properties matter more than speed:

* The owner is told **once** per outage, not once per check. A dead app stays
  dead, and a message every minute would be worse than the outage.
* The record is written to disk **before** the restart and before any message,
  so a failure survives even if this watcher itself is killed.

Run it in the background:

    nohup python scripts/watchdog.py > /tmp/sagarpg-watchdog.log 2>&1 &

Or let scripts/watch_app.sh start it for you.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEALTH_URL = "http://127.0.0.1:{port}/health"
START_SCRIPT = ROOT / "scripts" / "start_app.sh"
STATE_DIR = Path(os.environ.get("WATCHDOG_STATE_DIR", "/tmp/sagarpg-watchdog"))
STATE_FILE = STATE_DIR / "state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
OUTAGE_LOG = STATE_DIR / "outages.log"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"status": "unknown", "since": None, "checks": 0}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def record(event: str, **fields) -> None:
    """Append one line to the event history. This is the record to read later."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = {"at": _now(), "event": event, **fields}
    with EVENTS_FILE.open("a") as handle:
        handle.write(json.dumps(line) + "\n")


def health_ok(port: int, timeout: float) -> tuple[bool, str]:
    """True when the app answers /health with a healthy body."""
    url = HEALTH_URL.format(port=port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            if response.status != 200:
                return False, f"HTTP {response.status}"
            try:
                parsed = json.loads(body)
            except ValueError:
                return False, f"unreadable health body: {body[:80]}"
            if parsed.get("status") != "ok":
                return False, f"health reported {parsed}"
            return True, "ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"no answer: {exc}"


def restart_app() -> tuple[bool, str]:
    """Run the start script. Returns (ok, output-tail)."""
    if not START_SCRIPT.exists():
        return False, f"missing {START_SCRIPT}"
    try:
        done = subprocess.run(
            ["bash", str(START_SCRIPT)],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "start script timed out after 300s"
    except Exception as exc:
        return False, f"could not run the start script: {exc}"
    lines = [ln for ln in (done.stdout or "").splitlines()
             if ln.strip() and "password" not in ln.lower()]
    return done.returncode == 0, " | ".join(lines[-4:])


def notify_owner(text: str) -> tuple[bool, str]:
    """Send the owner one WhatsApp message through the app's tested code path.

    Reusing the app's own channel and inbox means the message is recorded
    exactly like every other owner notification, and there is one place the
    WhatsApp credentials live.
    """
    try:
        from core import inbox
        from core.channel.registry import build_channel
        from core.config import Settings, load_dotenv
        from core.db import Database

        load_dotenv()
        settings = Settings.from_env()
        if not settings.owner_phone:
            return False, "OWNER_PHONE is not set, so nobody could be told"
        channel = build_channel(settings.channel_provider,
                                **settings.channel_credentials())
        result = channel.send_text(settings.owner_phone, text)
        if not result.ok:
            return False, f"send failed: {result.error}"
        try:
            inbox.record_outbound(Database(settings.database_url),
                                  settings.owner_phone, text,
                                  result.provider_message_id)
        except Exception as exc:  # the message went out; the record is a bonus
            return True, f"sent, but could not record it: {exc}"
        return True, "sent"
    except Exception as exc:
        return False, f"could not send: {exc}"


def _append_outage_log(text: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with OUTAGE_LOG.open("a") as handle:
        handle.write(f"[{_now()}] {text}\n")


def _restart_due(state: dict, retry_after: float) -> bool:
    """True when a restart should be attempted now.

    A restart is retried while the app stays down, because a first attempt can
    fail for a passing reason (Docker still starting). The retry is spaced out
    so the start script is not run on every single check.
    """
    last = state.get("last_restart_at")
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return True
    return elapsed >= retry_after


def handle_down(state: dict, reason: str, *, restart: bool, notify: bool,
                restart_retry: float = 120) -> dict:
    """The app did not answer. Record it, restart, tell the owner once."""
    already_down = state.get("status") == "down"

    if not already_down:
        # First sight of this outage. Record it, and tell the owner once.
        state = {"status": "down", "since": _now(), "checks_down": 1,
                 "reason": reason, "checks": state.get("checks", 0) + 1,
                 "last_restart_at": None}
        _save_state(state)
        _append_outage_log(f"DOWN: {reason}")
        record("down", reason=reason)

        if notify:
            message = (
                "Heads up: the hostel app stopped answering "
                f"({reason}). I am restarting it now. "
                "If guests message and get no reply, check the server. "
                "I will tell you when it is healthy again."
            )
            ok, detail = notify_owner(message)
            record("notify", stage="down", ok=ok, detail=detail)
            state["notified_down"] = ok
    else:
        # Same outage continuing. Count it, but do not alert again.
        state["checks_down"] = state.get("checks_down", 0) + 1
        state["checks"] = state.get("checks", 0) + 1

    if restart and _restart_due(state, restart_retry):
        ok, detail = restart_app()
        state["last_restart_at"] = _now()
        state["last_restart_ok"] = ok
        record("restart", ok=ok, detail=detail)
        _append_outage_log(f"restart: ok={ok} {detail}")

    _save_state(state)
    return state


def handle_up(state: dict, *, notify: bool) -> dict:
    """The app answered. If it was down, tell the owner it recovered."""
    if state.get("status") != "down":
        state["status"] = "up"
        state["checks"] = state.get("checks", 0) + 1
        return state

    down_since = state.get("since")
    state = {"status": "up", "since": _now(), "recovered_from": down_since,
             "checks": state.get("checks", 0) + 1}
    _save_state(state)
    record("up", recovered_from=down_since)
    _append_outage_log(f"UP: was down since {down_since}")

    if notify:
        message = (
            "Good news: the hostel app is answering again "
            f"(it was down since {down_since})."
        )
        ok, detail = notify_owner(message)
        record("notify", stage="up", ok=ok, detail=detail)
        state["notified_up"] = ok

    _save_state(state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "12000")))
    parser.add_argument("--interval", type=float,
                        default=float(os.environ.get("WATCHDOG_INTERVAL", "30")),
                        help="seconds between checks")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("WATCHDOG_TIMEOUT", "5")),
                        help="seconds to wait for /health")
    parser.add_argument("--no-restart", action="store_true",
                        help="watch and notify, but never restart")
    parser.add_argument("--no-notify", action="store_true",
                        help="watch and restart, but never message the owner")
    parser.add_argument("--once", action="store_true",
                        help="run one check and exit (for testing)")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record("watchdog_started", port=args.port, interval=args.interval,
           restart=not args.no_restart, notify=not args.no_notify)

    state = _load_state()
    while True:
        ok, detail = health_ok(args.port, args.timeout)
        if ok:
            state = handle_up(state, notify=not args.no_notify)
        else:
            state = handle_down(state, detail,
                                restart=not args.no_restart,
                                notify=not args.no_notify)
        _save_state(state)
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
