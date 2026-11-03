"""Scheduled jobs, exposed as HTTP endpoints.

n8n calls these on a schedule. The jobs live here, next to the data they read,
rather than as JavaScript inside n8n — so they are tested and version-controlled
like everything else.

Every endpoint requires the shared job token. Without it, anyone who found the
URL could read the hostel's figures.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core import availability, booking, inbox, payments
from core.config import Settings
from core.db import Database

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs")


def _authorise(provided: str | None) -> None:
    expected = os.environ.get("JOB_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="JOB_TOKEN is not configured")
    if provided != expected:
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
    return report


@router.get("/unanswered-enquiries")
def unanswered(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Enquiries nobody has replied to yet."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    rows = inbox.unanswered_enquiries(db, _now())
    return {
        "count": len(rows),
        "enquiries": [
            {"phone": r["phone"],
             "last_message_at": r["last_message_at"].isoformat(),
             "waiting_minutes": int((_now() - r["last_message_at"]).total_seconds() // 60)}
            for r in rows
        ],
    }


@router.get("/upcoming")
def upcoming(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Arrivals and departures in the next day, for reminders."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    tomorrow = _now() + timedelta(days=1)
    return {
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
            SELECT count(*) AS n FROM messages
            WHERE direction = 'IN' AND processed_at IS NULL
              AND received_at < now() - interval '15 minutes'
            """)
        if stale and stale["n"]:
            problems.append(f"{stale['n']} inbound messages unprocessed for over 15 minutes")

        expired_holds = db.query_one(
            "SELECT count(*) AS n FROM bed_holds "
            "WHERE status = 'ACTIVE' AND expires_at < now()")
        if expired_holds and expired_holds["n"]:
            problems.append(f"{expired_holds['n']} expired holds are still ACTIVE")

    return JSONResponse({"ok": not problems, "problems": problems},
                        status_code=200 if not problems else 503)


@router.get("/pending-requests")
def pending_requests(request: Request, x_job_token: str | None = Header(default=None)) -> dict:
    """Booking requests waiting for the owner."""
    _authorise(x_job_token)
    db: Database = request.app.state.db
    return {
        "count": len(booking.pending_requests(db)),
        "requests": [
            {"reference": r["reference"], "bed_id": r["bed_id"],
             "guest": r["full_name"] or r["phone"],
             "check_in_at": r["check_in_at"].isoformat(),
             "check_out_at": r["check_out_at"].isoformat()}
            for r in booking.pending_requests(db)
        ],
    }
