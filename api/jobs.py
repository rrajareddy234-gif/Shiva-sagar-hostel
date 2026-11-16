"""Scheduled jobs, exposed as HTTP endpoints.

n8n calls these on a schedule. The jobs live here, next to the data they read,
rather than as JavaScript inside n8n — so they are tested and version-controlled
like everything else.

Every endpoint requires the shared job token. Without it, anyone who found the
URL could read the hostel's figures.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core import availability, booking, inbox, payments, reports
from core.config import Settings
from core.db import Database

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs")


def _authorise(provided: str | None) -> None:
    expected = os.environ.get("JOB_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="JOB_TOKEN is not configured")
    # Constant-time compare, like the webhook's signature check: a plain `!=`
    # leaks how many leading characters matched, which is enough to recover a
    # token one byte at a time.
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid job token")


def _now() -> datetime:
    return datetime.now().astimezone()


@router.get("/daily-report")
def daily_report(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """The numbers the owner wants every morning."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    now = _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)

    summary = availability.availability_summary(db, now, day_end)
    report = {
        "generated_at": now.isoformat(),
        "beds": {
            "total": summary["total_beds"],
            "available": summary["available_beds"],
            "occupied": summary["occupied_beds"],
            "blocked": summary["blocked_beds"],
            "full_rooms": summary["full_rooms"],
        },
        "arrivals_today": len(booking.arrivals_on(db, now)),
        "departures_today": len(booking.departures_on(db, now)),
        "overdue_checkouts": len(booking.overdue_checkouts(db)),
        "pending_requests": len(booking.pending_requests(db)),
        "in_house": len(booking.in_house(db)),
        "payments_awaiting_verification": len(payments.unverified_payments(db)),
        "outstanding_balances": len(payments.outstanding_bookings(db)),
        "enquiries_today": len(inbox.enquiries_between(db, day_start, day_end)),
        "unanswered_enquiries": len(inbox.unanswered_enquiries(db, now)),
        "revenue_this_month": str(payments.revenue_between(db, month_start, day_end)),
        "collected_today": str(payments.revenue_between(db, day_start, day_end)),
    }
    report["text"] = reports.daily_report_text(report)
    return report


@router.get("/unanswered-enquiries")
def unanswered(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Enquiries nobody has replied to yet."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    rows = inbox.unanswered_enquiries(db, _now())
    payload = {
        "count": len(rows),
        "enquiries": [
            {"phone": r["phone"],
             "last_message_at": r["last_message_at"].isoformat(),
             "waiting_minutes": int((_now() - r["last_message_at"]).total_seconds() // 60)}
            for r in rows
        ],
    }
    payload["text"] = reports.unanswered_enquiries_text(payload)
    return payload


@router.get("/upcoming")
def upcoming(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Arrivals and departures in the next day, for reminders."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    tomorrow = _now() + timedelta(days=1)
    payload = {
        "arrivals_tomorrow": [
            {"reference": r["reference"], "bed_id": r["bed_id"],
             "guest": r["full_name"] or r["phone"],
             "check_in_at": r["check_in_at"].isoformat()}
            for r in booking.arrivals_on(db, tomorrow)
        ],
        "departures_today": [
            {"reference": r["reference"], "bed_id": r["bed_id"],
             "guest": r["full_name"] or r["phone"],
             "check_out_at": r["check_out_at"].isoformat()}
            for r in booking.departures_on(db, _now())
        ],
        "overdue": [
            {"reference": r["reference"], "bed_id": r["bed_id"],
             "guest": r["full_name"] or r["phone"],
             "check_out_at": r["check_out_at"].isoformat()}
            for r in booking.overdue_checkouts(db)
        ],
    }
    payload["text"] = reports.upcoming_text(payload)
    return payload


@router.get("/health-check")
def health_check(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """What the monitoring workflow watches."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    problems = []

    try:
        db.query_one("SELECT 1 AS ok")
    except Exception as exc:
        problems.append(f"database: {exc}")

    if not problems:
        stale = db.query_one(
            """
            SELECT count(*) AS n FROM messages m
            LEFT JOIN inbound_processing p ON p.message_id = m.message_id
            WHERE m.direction = 'IN' AND m.processed_at IS NULL
              AND COALESCE(p.state, 'pending') <> 'failed'
              AND (p.message_id IS NULL
                   OR (p.state = 'pending'
                       AND (p.next_retry_at IS NULL OR p.next_retry_at <= now()))
                   OR (p.state = 'processing' AND p.claimed_until <= now()))
              AND m.received_at < now() - interval '15 minutes'
            """)
        if stale and stale["n"]:
            problems.append(f"{stale['n']} inbound messages unprocessed for over 15 minutes")

        failed_inbound = db.query_one(
            "SELECT count(*) AS n FROM inbound_processing WHERE state = 'failed'")
        if failed_inbound and failed_inbound["n"]:
            problems.append(f"{failed_inbound['n']} inbound messages exhausted retry processing")

    payload = {"ok": not problems, "problems": problems}
    payload["text"] = reports.health_check_text(payload)
    return JSONResponse(payload, status_code=200 if not problems else 503)


@router.post("/retry-unprocessed")
def retry_unprocessed(request: Request,
                      x_job_token: str | None = Header(default=None)) -> dict:
    """Retry inbound messages whose dispatch lease expired without completion."""
    _authorise(x_job_token)
    from core import inbox
    from core.dispatcher import dispatch

    db: Database = request.app.state.db
    rows = inbox.claim_stale_unprocessed(db)
    succeeded = 0
    failed = 0
    for row in rows:
        token = row["claim_token"]
        try:
            message = inbox.inbound_from_row(db, row)
            dispatch(db, request.app.state.channel,
                     request.app.state.settings, message,
                     request.app.state.ai, str(row["message_id"]),
                     claim_token=token)
            current = db.query_one(
                "SELECT state FROM inbound_processing WHERE message_id = %s",
                (row["message_id"],),
            )
            if current and current["state"] == "processed":
                succeeded += 1
                continue
            state = inbox.fail_inbound_attempt(
                db, str(row["message_id"]), token,
                RuntimeError("dispatch did not complete"), retryable=True,
            )
            if state == "failed":
                failed += 1
        except Exception as exc:
            log.exception("retry dispatch failed for stored message")
            state = inbox.fail_inbound_attempt(
                db, str(row["message_id"]), token, exc,
                retryable=inbox.inbound_error_is_retryable(exc),
            )
            if state == "failed":
                failed += 1
    return {"claimed": len(rows), "completed": succeeded,
            "failed": failed, "still_pending": len(rows) - succeeded - failed}


@router.get("/pending-requests")
def pending_requests(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Booking requests waiting for the owner."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    payload = {
        "count": len(booking.pending_requests(db)),
        "requests": [
            {"reference": r["reference"], "bed_id": r["bed_id"],
             "guest": r["full_name"] or r["phone"],
             "check_in_at": r["check_in_at"].isoformat(),
             "check_out_at": r["check_out_at"].isoformat()}
            for r in booking.pending_requests(db)
        ],
    }
    payload["text"] = reports.pending_requests_text(payload)
    return payload


@router.post("/notify-owner")
def notify_owner(request: Request, payload: dict,
                 x_job_token: str | None = Header(default=None)) -> dict:
    """Send the owner a WhatsApp message.

    n8n calls this instead of talking to Meta itself, so the WhatsApp
    credentials live in one place and every owner notification is recorded.
    """
    _authorise(x_job_token)
    db: Database = request.app.state.db
    settings: Settings = request.app.state.settings

    if not settings.owner_phone:
        raise HTTPException(status_code=503, detail="OWNER_PHONE is not configured")

    text = (payload or {}).get("text")
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    import uuid

    from core import outbox
    key = (payload or {}).get("idempotency_key") or f"job-notification:{uuid.uuid4()}"
    outbox.enqueue(db, recipient=settings.owner_phone, message_type="text",
                   payload={"text": text}, idempotency_key=key,
                   source_event_type="scheduled_notification")
    return {"queued": True, "to": settings.owner_phone, "idempotency_key": key}


@router.post("/send-outbound")
def send_outbound(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Send due durable outbox messages. The scheduler is the only caller."""
    _authorise(x_job_token)
    from core.outbound_worker import run_batch
    return run_batch(request.app.state.db, request.app.state.channel,
                     request.app.state.settings)


@router.post("/outbound-retention")
def outbound_retention(request: Request,
                       x_job_token: str | None = Header(default=None)) -> dict:
    """Scrub expired message bodies and private proof copies."""
    _authorise(x_job_token)
    from core import outbox
    count = outbox.scrub_expired_payloads(request.app.state.db,
                                           request.app.state.settings.payment_proof_dir)
    return {"scrubbed": count}
