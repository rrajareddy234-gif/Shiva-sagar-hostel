"""What the AI is told about the person it is talking to.

The database is the memory. This module does not summarise and does not guess:
it reads a few rows and renders them as plain lines. Nothing here is invented,
and nothing here is written by a model.

Money is deliberately absent. A balance is computed live by a tool when it is
asked for, so a remembered figure can never go stale and be quoted as fact.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core import availability, booking, payments, security
from core.db import Database


def _guest_block(db: Database, phone: str) -> list[str]:
    guest = db.query_one(
        "SELECT full_name, gender, notes, created_at FROM guests WHERE phone = %s",
        (phone,))
    if not guest:
        return ["You have not spoken to this number before."]

    lines = [f"Name: {guest['full_name'] or 'not given yet'}"]
    if guest["gender"] and guest["gender"] != "UNKNOWN":
        lines.append(f"Gender: {guest['gender'].lower()}")
    lines.append(f"Enquiring since: {guest['created_at']:%d %b %Y}")

    count = db.query_one(
        "SELECT count(*) AS n FROM bookings b JOIN guests g ON g.guest_id = b.guest_id "
        "WHERE g.phone = %s", (phone,))
    lines.append(f"Bookings so far: {count['n']}")

    current = db.query_one(
        """
        SELECT b.reference, b.status, b.bed_id, b.check_in_at, b.check_out_at,
               b.food_included, b.payment_terms, b.advance_due
        FROM bookings b JOIN guests g ON g.guest_id = b.guest_id
        WHERE g.phone = %s
          AND b.status IN ('REQUESTED','CONFIRMED','CHECKED_IN')
        ORDER BY b.created_at DESC LIMIT 1
        """,
        (phone,))
    if current:
        lines.append(
            f"Current stay: {current['reference']} is {current['status']} "
            f"in bed {current['bed_id']}, "
            f"{current['check_in_at']:%d %b} to {current['check_out_at']:%d %b}, "
            f"{'with' if current['food_included'] else 'without'} food")
        if current["status"] == "REQUESTED":
            lines.append("The owner has not confirmed this request yet.")

    if guest["notes"]:
        lines.append(f"Owner's note: {guest['notes']}")
    return lines


def _owner_block(db: Database, now: datetime) -> list[str]:
    """A small live snapshot for the owner. Counts, not money totals."""
    summary = availability.availability_summary(db, now, now + timedelta(days=1))
    pending = booking.pending_requests(db)
    unverified = payments.unverified_payments(db)
    dues = payments.outstanding_bookings(db)
    in_house = booking.in_house(db)
    return [
        f"Beds free now: {summary['available_beds']} of {summary['total_beds']}",
        f"Guests in house: {len(in_house)}",
        f"Booking requests awaiting approval: {len(pending)}",
        f"Payments awaiting verification: {len(unverified)}",
        f"Bookings with a balance: {len(dues)}",
    ]


def memory_for(db: Database, *, phone: str, role: str,
               now: datetime | None = None) -> str:
    """The block of facts the AI is given about the caller. Never guesses."""
    now = now or datetime.now().astimezone()
    if role == security.OWNER:
        body = _owner_block(db, now)
        who = "You are speaking with the owner."
    elif role == security.STAFF:
        body = _owner_block(db, now)
        who = ("You are speaking with a staff member. They may check guests in and "
               "out, add extras and record payments, but only the owner verifies money.")
    else:
        body = _guest_block(db, phone)
        who = "You are speaking with a guest or enquirer."

    lines = [
        "WHAT YOU KNOW ABOUT THIS PERSON (from the hostel database; these are facts):",
        who,
        *body,
        "Amounts are never quoted from memory. Call a tool for any price, balance "
        "or bill.",
    ]
    return "\n".join(lines)
