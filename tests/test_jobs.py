"""Scheduled job endpoint tests.

These are what n8n calls on a schedule. They must return the real figures, and
must refuse to answer without the job token.
"""

from __future__ import annotations

import pytest

from tests.conftest import channel, client  # noqa: F401  (shared fixtures)

JOB_TOKEN = "test-job-token"
AUTH = {"X-Job-Token": JOB_TOKEN}


@pytest.fixture(autouse=True)
def _job_token(monkeypatch):
    monkeypatch.setenv("JOB_TOKEN", JOB_TOKEN)


# --- authorisation ---------------------------------------------------------

def test_daily_report_requires_a_token(client):
    assert client.get("/jobs/daily-report").status_code == 401


def test_daily_report_rejects_a_wrong_token(client):
    assert client.get("/jobs/daily-report",
                      headers={"X-Job-Token": "wrong"}).status_code == 401


def test_missing_configuration_is_reported(client, monkeypatch):
    monkeypatch.delenv("JOB_TOKEN", raising=False)
    assert client.get("/jobs/daily-report", headers=AUTH).status_code == 503


@pytest.mark.parametrize("path", ["/jobs/unanswered-enquiries", "/jobs/upcoming",
                                  "/jobs/health-check", "/jobs/pending-requests"])
def test_every_job_requires_a_token(client, path):
    assert client.get(path).status_code == 401


# --- daily report ----------------------------------------------------------

def test_daily_report_returns_the_beds(client):
    body = client.get("/jobs/daily-report", headers=AUTH).json()
    assert body["beds"]["total"] == 10
    assert body["beds"]["available"] == 10


def test_daily_report_counts_pending_requests(client, db):
    from datetime import datetime, timedelta

    from core import booking
    from tests.conftest import IST
    start = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    booking.create_request(db, phone="+919000003000", full_name="Report Guest",
                           gender="MALE", bed_id="1NA-B1", check_in_at=start,
                           check_out_at=start + timedelta(days=2))
    body = client.get("/jobs/daily-report", headers=AUTH).json()
    assert body["pending_requests"] == 1


def test_daily_report_revenue_excludes_unverified_money(client, db):
    body = client.get("/jobs/daily-report", headers=AUTH).json()
    assert body["revenue_this_month"] == "0.00"
    assert body["collected_today"] == "0.00"


def test_daily_report_counts_enquiries(client, db):
    body = client.get("/jobs/daily-report", headers=AUTH).json()
    assert body["enquiries_today"] == 0


# --- unanswered enquiries --------------------------------------------------

def test_unanswered_enquiries_lists_the_phone(client, db):
    from core import inbox
    from core.channel.adapter import InboundMessage
    inbox.store_inbound(db, InboundMessage(provider_message_id="wamid.JOB1",
                                           from_phone="+919000003111",
                                           text="is a room free?",
                                           message_type="text"))
    db.execute("UPDATE enquiries SET last_message_at = now() - interval '2 hours'")
    body = client.get("/jobs/unanswered-enquiries", headers=AUTH).json()
    assert body["count"] == 1
    assert body["enquiries"][0]["phone"] == "+919000003111"
    assert body["enquiries"][0]["waiting_minutes"] >= 119


def test_unanswered_enquiries_empty_when_none(client):
    body = client.get("/jobs/unanswered-enquiries", headers=AUTH).json()
    assert body["count"] == 0


# --- upcoming --------------------------------------------------------------

def test_upcoming_returns_empty_lists(client):
    body = client.get("/jobs/upcoming", headers=AUTH).json()
    assert body["arrivals_tomorrow"] == []
    assert body["departures_today"] == []
    assert body["overdue"] == []


def test_upcoming_reports_an_overdue_checkout(client, db):
    from datetime import datetime, timedelta

    from core import booking
    from tests.conftest import IST
    owner = "+919999999999"
    start = datetime.now(IST) - timedelta(days=5)
    request = booking.create_request(db, phone="+919000003222", full_name="Late Guest",
                                     gender="MALE", bed_id="1NA-B1",
                                     check_in_at=start,
                                     check_out_at=start + timedelta(days=1))
    booking.approve(db, request.booking_id, actor=owner)
    booking.check_in(db, request.booking_id, actor=owner)
    body = client.get("/jobs/upcoming", headers=AUTH).json()
    assert len(body["overdue"]) == 1
    assert body["overdue"][0]["guest"] == "Late Guest"


# --- health check ----------------------------------------------------------

def test_health_check_is_ok_on_a_clean_database(client):
    response = client.get("/jobs/health-check", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_health_check_flags_a_stuck_message(client, db):
    from core import inbox
    from core.channel.adapter import InboundMessage
    inbox.store_inbound(db, InboundMessage(provider_message_id="wamid.STUCK",
                                           from_phone="+919000003333",
                                           text="hello", message_type="text"))
    db.execute("UPDATE messages SET received_at = now() - interval '30 minutes'")
    response = client.get("/jobs/health-check", headers=AUTH)
    assert response.status_code == 503
    assert any("unprocessed" in p for p in response.json()["problems"])


def test_health_check_flags_an_expired_hold(client, db):
    """A hold that expired but is still ACTIVE means the release job is not running."""
    db.execute(
        """
        INSERT INTO bed_holds (bed_id, start_at, end_at, expires_at, status, created_at)
        VALUES ('1NA-B1', now() + interval '1 day', now() + interval '2 days',
                now() - interval '10 minutes', 'ACTIVE', now() - interval '2 hours')
        """)
    response = client.get("/jobs/health-check", headers=AUTH)
    assert response.status_code == 503
    assert any("expired holds" in p for p in response.json()["problems"])


# --- pending requests ------------------------------------------------------

def test_pending_requests_job(client, db):
    from datetime import datetime, timedelta

    from core import booking
    from tests.conftest import IST
    start = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    request = booking.create_request(db, phone="+919000003444", full_name="Pending Guest",
                                     gender="MALE", bed_id="1NA-B1",
                                     check_in_at=start,
                                     check_out_at=start + timedelta(days=1))
    body = client.get("/jobs/pending-requests", headers=AUTH).json()
    assert body["count"] == 1
    assert body["requests"][0]["reference"] == request.reference
    assert body["requests"][0]["guest"] == "Pending Guest"


# --- notify the owner ------------------------------------------------------

def test_notify_owner_sends_a_message(client, channel):
    response = client.post("/jobs/notify-owner", json={"text": "2 beds left"},
                           headers=AUTH)
    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert channel.sent == [("+919999999999", "2 beds left")]


def test_notify_owner_records_the_message(client, db):
    client.post("/jobs/notify-owner", json={"text": "2 beds left"}, headers=AUTH)
    row = db.query_one("SELECT body, direction FROM messages WHERE direction = 'OUT'")
    assert row["body"] == "2 beds left"


def test_notify_owner_requires_a_token(client):
    response = client.post("/jobs/notify-owner", json={"text": "hi"})
    assert response.status_code == 401


def test_notify_owner_rejects_an_empty_message(client):
    response = client.post("/jobs/notify-owner", json={"text": ""}, headers=AUTH)
    assert response.status_code == 422


def test_notify_owner_reports_a_send_failure(client, channel):
    channel.fail = True
    response = client.post("/jobs/notify-owner", json={"text": "hello"}, headers=AUTH)
    assert response.status_code == 502
