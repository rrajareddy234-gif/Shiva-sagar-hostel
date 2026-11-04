"""Read-only data for the owner's dashboard.

Every figure comes from the same helpers the bot uses in the chat, so the page
and the WhatsApp answers can never disagree.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from core import availability, booking, payments
from core.db import Database
from core.errors import NotFound

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _generated_password() -> str:
    """A one-off password for a run where the owner set none.

    There is deliberately no fixed default: a guessable password on a public
    deployment would hand every guest's name, phone and booking to anyone who
    tried it. The value is logged once so the owner can read it back.
    """
    password = secrets.token_urlsafe(12)
    log.warning(
        "DASHBOARD_PASSWORD is not set. Generated a random one for this run: %s  "
        "(set DASHBOARD_PASSWORD to choose your own)", password)
    return password


def dashboard_password() -> str:
    """The password that protects the page.

    The owner sets it with DASHBOARD_PASSWORD. When it is not set the page is
    NOT left open with a known password; a random one is generated instead.
    """
    return os.environ.get("DASHBOARD_PASSWORD") or _generated_password()


def password_is_ok(supplied: str | None) -> bool:
    """Constant-time compare, so a wrong guess leaks no timing hint."""
    return hmac.compare_digest(supplied or "", dashboard_password())


@dataclass(frozen=True)
class GuestRow:
    """One guest, as the table shows them."""

    name: str
    phone: str
    booking: str
    status: str
    bed: str
    room: str
    check_in: str
    check_out: str
    bill: str
    paid: str
    due: str


def _rupees(value) -> str:
    return f"{value:,.0f}"


def _day(value) -> str:
    return value.strftime("%d %b %Y") if value else ""


def overview(db: Database, now: datetime) -> dict:
    """The numbers across the top of the page."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)

    beds = availability.availability_summary(db, now, day_end)
    pending = booking.pending_requests(db)
    return {
        "beds_total": beds["total_beds"],
        "beds_free": beds["available_beds"],
        "beds_occupied": beds["occupied_beds"],
        "in_house": len(booking.in_house(db)),
        "arrivals_today": len(booking.arrivals_on(db, now)),
        "departures_today": len(booking.overdue_checkouts(db, now)),
        "pending": len(pending),
        "to_verify": len(payments.unverified_payments(db)),
        "outstanding": len(payments.outstanding_bookings(db)),
        "revenue_month": _rupees(payments.revenue_between(db, month_start, day_end)),
    }


def pending_requests(db: Database) -> list[dict]:
    """Bookings waiting for the owner, ready to show as a small list."""
    rows = booking.pending_requests(db)
    return [
        {
            "reference": r["reference"],
            "name": r["full_name"] or "Unnamed",
            "phone": r["phone"],
            "bed": r["bed_id"],
            "check_in": _day(r["check_in_at"]),
            "check_out": _day(r["check_out_at"]),
            "amount": _rupees(r["charges_total"] or 0),
        }
        for r in rows
    ]


def _resolve(db: Database, reference: str) -> dict:
    """Find a booking by its reference, or by its id. Raise if there is none."""
    row = db.query_one(
        "SELECT * FROM bookings WHERE reference = %(ref)s OR booking_id::text = %(ref)s",
        {"ref": reference})
    if not row:
        raise NotFound(f"no booking found for {reference}")
    return row


def approve_booking(db: Database, reference: str, *, actor: str) -> dict:
    """Confirm a waiting booking, exactly as the owner's WhatsApp command does.

    The safety checks are the same ones the chat runs: a bed that was taken
    while the request waited is refused, and a bed that is not ready needs an
    owner override. Nothing here bypasses them.
    """
    row = _resolve(db, reference)
    booking.approve(db, str(row["booking_id"]), actor=actor, reason="owner dashboard")
    return {"reference": row["reference"], "status": "CONFIRMED"}


def reject_booking(db: Database, reference: str, *, actor: str) -> dict:
    """Reject a waiting booking, which frees its bed."""
    row = _resolve(db, reference)
    booking.reject(db, str(row["booking_id"]), actor=actor, reason="owner dashboard")
    return {"reference": row["reference"], "status": "REJECTED"}


def guests(db: Database, *, search: str | None = None, limit: int = 200) -> list[GuestRow]:
    """Every guest with their latest stay and money, newest first.

    `search` matches a name or any part of a phone number. An empty search
    returns everyone, so the table is never blank on first open.
    """
    term = (search or "").strip()
    digits = "".join(ch for ch in term if ch.isdigit())

    rows = db.query_all(
        """
        SELECT g.guest_id, g.full_name, g.phone, g.created_at,
               b.booking_id, b.reference, b.status, b.bed_id,
               bd.room_id, b.check_in_at, b.check_out_at
        FROM guests g
        LEFT JOIN LATERAL (
            SELECT booking_id, reference, status, bed_id, check_in_at, check_out_at
            FROM bookings WHERE guest_id = g.guest_id
            ORDER BY created_at DESC LIMIT 1
        ) b ON true
        LEFT JOIN beds bd ON bd.bed_id = b.bed_id
        WHERE g.is_owner IS NOT TRUE AND g.is_staff IS NOT TRUE
          AND (
              %(term)s = ''
              OR g.full_name ILIKE '%%' || %(term)s || '%%'
              OR (%(digits)s <> '' AND regexp_replace(g.phone, '\\D', '', 'g')
                  LIKE '%%' || %(digits)s || '%%')
          )
        ORDER BY g.created_at DESC
        LIMIT %(limit)s
        """,
        {"term": term, "digits": digits, "limit": limit})

    out: list[GuestRow] = []
    for row in rows:
        bill = paid = due = ""
        if row["reference"]:
            balance = payments.get_balance(db, str(row["booking_id"]))
            bill, paid, due = (_rupees(balance.charges_total),
                               _rupees(balance.verified_paid),
                               _rupees(balance.balance_due))
        out.append(GuestRow(
            name=row["full_name"] or "Unnamed",
            phone=row["phone"] or "",
            booking=row["reference"] or "",
            status=row["status"] or "",
            bed=row["bed_id"] or "",
            room=row["room_id"] or "",
            check_in=_day(row["check_in_at"]),
            check_out=_day(row["check_out_at"]),
            bill=bill, paid=paid, due=due,
        ))
    return out
