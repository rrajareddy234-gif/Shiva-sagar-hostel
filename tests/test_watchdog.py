"""The watchdog's decisions, without a running app or a real send.

The watchdog exists because a dead app cannot report itself dead. These tests
cover the decisions it makes: when to alert, when to restart, and the promise
that matters most — the owner is told once per outage, not once per check.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import watchdog


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(watchdog, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(watchdog, "OUTAGE_LOG", tmp_path / "outages.log")
    return tmp_path


def _events(state_dir: Path) -> list[dict]:
    path = state_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_first_failure_alerts_once_and_restarts(state_dir, monkeypatch):
    sends, restarts = [], []
    monkeypatch.setattr(watchdog, "notify_owner",
                        lambda text: (sends.append(text), (True, "sent"))[1])
    monkeypatch.setattr(watchdog, "restart_app",
                        lambda: (restarts.append(1), (True, "ok"))[1])

    state = {"status": "up", "checks": 5}
    state = watchdog.handle_down(state, "connection refused",
                                 restart=True, notify=True)

    assert state["status"] == "down"
    assert len(sends) == 1
    assert len(restarts) == 1
    assert "stopped answering" in sends[0]


def test_a_failure_that_continues_alerts_only_once(state_dir, monkeypatch):
    sends, restarts = [], []
    monkeypatch.setattr(watchdog, "notify_owner",
                        lambda text: (sends.append(text), (True, "sent"))[1])
    monkeypatch.setattr(watchdog, "restart_app",
                        lambda: (restarts.append(1), (True, "ok"))[1])

    state = {"status": "up"}
    for _ in range(5):
        state = watchdog.handle_down(state, "connection refused",
                                     restart=True, notify=True)

    assert len(sends) == 1, "one outage must not become five messages"
    assert state["checks_down"] == 5
    # The restart is retried, but not on every single check.
    assert len(restarts) == 1


def test_a_restart_is_retried_after_the_retry_window(state_dir, monkeypatch):
    restarts = []
    monkeypatch.setattr(watchdog, "notify_owner", lambda text: (True, "sent"))
    monkeypatch.setattr(watchdog, "restart_app",
                        lambda: (restarts.append(1), (True, "ok"))[1])

    state = {"status": "up"}
    state = watchdog.handle_down(state, "down", restart=True, notify=True)
    assert len(restarts) == 1

    # A stale timestamp lets the next attempt through.
    state["last_restart_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    watchdog.handle_down(state, "down", restart=True, notify=True)
    assert len(restarts) == 2


def test_recovery_alerts_once_and_only_after_an_outage(state_dir, monkeypatch):
    sends = []
    monkeypatch.setattr(watchdog, "notify_owner",
                        lambda text: (sends.append(text), (True, "sent"))[1])

    # Never went down: no recovery message.
    state = watchdog.handle_up({"status": "up"}, notify=True)
    assert sends == []
    assert state["status"] == "up"

    # Was down: exactly one recovery message, then quiet.
    state = {"status": "down", "since": "2026-11-04T10:00:00+00:00"}
    state = watchdog.handle_up(state, notify=True)
    assert len(sends) == 1
    assert "answering again" in sends[0]
    watchdog.handle_up(state, notify=True)
    assert len(sends) == 1, "recovery must not repeat while the app stays up"


def test_the_outage_is_recorded_before_anything_else(state_dir, monkeypatch):
    # Even if the restart and the message both fail, the failure is on disk.
    monkeypatch.setattr(watchdog, "notify_owner", lambda text: (False, "no net"))
    monkeypatch.setattr(watchdog, "restart_app", lambda: (False, "no docker"))

    watchdog.handle_down({"status": "up"}, "HTTP 503",
                         restart=True, notify=True)

    events = _events(state_dir)
    assert events[0]["event"] == "down"
    assert events[0]["reason"] == "HTTP 503"
    assert (state_dir / "outages.log").read_text().startswith("[")
    assert "DOWN: HTTP 503" in (state_dir / "outages.log").read_text()


def test_a_dead_app_is_detected_when_nothing_answers(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise OSError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    ok, detail = watchdog.health_ok(12000, timeout=1)
    assert ok is False
    assert "no answer" in detail


def test_an_unhealthy_body_is_not_treated_as_healthy(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self):
            return b'{"status":"degraded"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    ok, detail = watchdog.health_ok(12000, timeout=1)
    assert ok is False
    assert "degraded" in detail
