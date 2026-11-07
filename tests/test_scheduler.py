"""The scheduler that replaced n8n.

Two things are worth testing here, and they are the two that n8n's JavaScript
never could be: *when* a job runs, and *whether* a message is sent. Both are
pure decisions, so neither test waits for a clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from deploy.scheduler import JOBS, Job, Scheduler, due_jobs

IST = ZoneInfo("Asia/Kolkata")


def _at(hour: int, minute: int, day: int = 6) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


# --- when a job runs ------------------------------------------------------

def test_an_interval_job_runs_the_first_time_and_then_waits():
    job = Job("health", "/jobs/health-check", every_minutes=10)
    assert job.due(_at(9, 0), None) is True
    assert job.due(_at(9, 5), _at(9, 0)) is False
    assert job.due(_at(9, 10), _at(9, 0)) is True


def test_a_daily_job_does_not_run_before_its_time():
    job = Job("report", "/jobs/daily-report", at="07:30")
    assert job.due(_at(7, 29), None) is False
    assert job.due(_at(7, 30), None) is True


def test_a_daily_job_does_not_run_twice_in_one_day():
    """The scheduler is checked every minute; the 07:30 report is sent once."""
    job = Job("report", "/jobs/daily-report", at="07:30")
    assert job.due(_at(7, 31), _at(7, 30)) is False
    assert job.due(_at(23, 0), _at(7, 30)) is False


def test_a_daily_job_runs_again_the_next_day():
    job = Job("report", "/jobs/daily-report", at="07:30")
    assert job.due(_at(7, 31, day=7), _at(7, 30, day=6)) is True


def test_a_job_with_no_schedule_never_runs():
    assert Job("broken", "/jobs/x").due(_at(12, 0), None) is False


def test_every_shipped_job_is_due_on_a_fresh_start():
    """A restart must not silently skip today's report."""
    due = {job.name for job in due_jobs(_at(23, 0), {})}
    assert {"health-check", "pending-requests", "unanswered-enquiries",
            "daily-report", "upcoming"} <= due


def test_jobs_are_deduplicated_by_their_recorded_run_time():
    now = _at(23, 0)
    runs = {job.name: now for job in JOBS}
    assert due_jobs(now, runs) == []


def test_the_health_check_keeps_running_while_the_rest_sleep():
    runs = {job.name: _at(23, 0) for job in JOBS}
    due = {job.name for job in due_jobs(_at(23, 10), runs)}
    assert due == {"health-check"}


# --- whether a message is sent --------------------------------------------

def test_a_job_with_nothing_to_say_sends_nothing(monkeypatch):
    """Silence is a decision, not an accident: see core/reports.py."""
    scheduler = Scheduler("http://app", "token", notify=True)
    monkeypatch.setattr(scheduler, "fetch", lambda job: {"ok": True, "text": None})
    sent = []
    monkeypatch.setattr(scheduler, "notify_owner", sent.append)

    scheduler.run_job(Job("health", "/jobs/health-check"))

    assert sent == []


def test_a_job_with_something_to_say_sends_it(monkeypatch):
    scheduler = Scheduler("http://app", "token", notify=True)
    monkeypatch.setattr(scheduler, "fetch", lambda job: {"text": "2 requests waiting"})
    sent = []
    monkeypatch.setattr(scheduler, "notify_owner", sent.append)

    scheduler.run_job(Job("pending", "/jobs/pending-requests"))

    assert sent == ["2 requests waiting"]


def test_no_notify_still_runs_the_job_but_sends_nothing(monkeypatch):
    """--no-notify is how the jobs are exercised without messaging the owner."""
    scheduler = Scheduler("http://app", "token", notify=False)
    monkeypatch.setattr(scheduler, "fetch", lambda job: {"text": "hello"})
    sent = []
    monkeypatch.setattr(scheduler, "notify_owner", sent.append)

    scheduler.run_job(Job("pending", "/jobs/pending-requests"))

    assert sent == []


def test_a_failed_fetch_becomes_a_readable_alert_not_a_traceback(monkeypatch):
    scheduler = Scheduler("http://app", "token")
    monkeypatch.setattr(scheduler, "fetch", lambda job: {
        "error": "pending-requests could not be reached: refused"})
    sent = []
    monkeypatch.setattr(scheduler, "notify_owner", sent.append)

    scheduler.run_job(Job("pending", "/jobs/pending-requests"))

    assert sent and "could not be reached" in sent[0]


def test_an_alert_body_is_still_delivered(monkeypatch):
    """A job that fails with a message must deliver the message.

    /jobs/health-check answers 503 with a body naming the problem. That body is
    the whole point of the call, so a non-200 must not swallow it.
    """
    scheduler = Scheduler("http://app", "token")
    monkeypatch.setattr(scheduler, "fetch", lambda job: {
        "ok": False, "problems": ["database: down"],
        "text": "Sagar PG alert: database: down"})
    sent = []
    monkeypatch.setattr(scheduler, "notify_owner", sent.append)

    scheduler.run_job(Job("health", "/jobs/health-check"))

    assert sent and "database: down" in sent[0]
