"""Booking state machine.

A booking request never becomes a confirmed booking silently. The owner approves
or rejects it, and every transition is written to the audit log.

States: REQUESTED -> CONFIRMED -> CHECKED_IN -> CHECKED_OUT
        REQUESTED -> REJECTED
        REQUESTED|CONFIRMED -> CANCELLED
        REQUESTED -> EXPIRED  (the temporary hold lapsed)
        CONFIRMED -> NO_SHOW

Concurrency: approval locks the booking row and re-checks the bed, and the
database exclusion constraint is the final guard. If two approvals race for the
same bed, one wins and the other is told the bed is taken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core import availability, pricing
from core.db import Database
from core.errors import (BedUnavailable, InvalidTransition, NotFound,
                         ValidationError)

DEFAULT_HOLD_MINUTES = 120

ALLOWED_TRANSITIONS = {
    "REQUESTED": {"CONFIRMED", "REJECTED", "CANCELLED", "EXPIRED"},
    "CONFIRMED": {"CHECKED_IN", "CANCELLED", "NO_SHOW"},
    "CHECKED_IN": {"CHECKED_OUT"},
    "CHECKED_OUT": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
    "NO_SHOW": set(),
    "EXPIRED": {"CONFIRMED", "REJECTED", "CANCELLED"},
}


@dataclass(frozen=True)
class BookingRequest:
    booking_id: str
    reference: str
    guest_id: str
    bed_id: str
    check_in_at: datetime
    check_out_at: datetime
    status: str
    charges_total: Decimal | None
    hold_id: str | None
    hold_expires_at: datetime | None
    quote: pricing.Quote


def _audit(cur, actor: str, action: str, entity: str, entity_id: str,
           before=None, after=None) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def get_or_create_guest(db: Database, *, phone: str, full_name: str | None = None,
                        gender: str | None = None) -> str:
    if not phone:
        raise ValidationError("a phone number is required")
    with db.transaction() as cur:
        cur.execute("SELECT guest_id FROM guests WHERE phone = %(phone)s", {"phone": phone})
        row = cur.fetchone()
        if row:
            if full_name or gender:
                cur.execute(
                    "UPDATE guests SET full_name = COALESCE(%(name)s, full_name), "
                    "gender = COALESCE(%(gender)s, gender) WHERE guest_id = %(id)s",
                    {"name": full_name, "gender": gender, "id": row["guest_id"]},
                )
            return str(row["guest_id"])
        cur.execute(
            "INSERT INTO guests (full_name, phone, gender) VALUES (%s, %s, %s) "
            "RETURNING guest_id",
            (full_name, phone, gender or "UNKNOWN"),
        )
        return str(cur.fetchone()["guest_id"])


def get_booking(db: Database, booking_id: str) -> dict:
    row = db.query_one("SELECT * FROM bookings WHERE booking_id = %s", (booking_id,))
    if not row:
        raise NotFound(f"booking {booking_id} not found")
    return row


def _require_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"a booking in state {current} cannot become {target}")


def create_request(db: Database, *, phone: str, full_name: str | None,
                   gender: str | None, bed_id: str, check_in_at: datetime,
                   check_out_at: datetime, food_included: bool | None = None,
                   mode: str = "daily", extras=0, discount=0,
                   stay_mode: str = "NIGHT",
                   hold_minutes: int = DEFAULT_HOLD_MINUTES,
                   source: str = "WHATSAPP") -> BookingRequest:
    """Create a booking request and a temporary hold. Does not confirm anything."""
    if check_out_at <= check_in_at:
        raise ValidationError("the checkout time must be after the check-in time")
    if hold_minutes <= 0:
        raise ValidationError("a hold must last at least a minute")

    bed = db.query_one(
        "SELECT b.bed_id, r.room_type FROM beds b JOIN rooms r ON r.room_id = b.room_id "
        "WHERE b.bed_id = %s",
        (bed_id,),
    )
    if not bed:
        raise NotFound(f"bed {bed_id} does not exist")

    guest_id = get_or_create_guest(db, phone=phone, full_name=full_name, gender=gender)
    quote = pricing.quote(db, room_type=bed["room_type"], food_included=food_included,
                          start=check_in_at, end=check_out_at, mode=mode,
                          stay_mode=stay_mode, extras=extras, discount=discount)
    food_included = quote.food_included

    with db.transaction() as cur:
        # Refuse if the bed is already held by someone else for this window.
        cur.execute(
            """
            SELECT 1 FROM bed_holds
            WHERE bed_id = %s AND status = 'ACTIVE' AND expires_at > now()
              AND tstzrange(start_at, end_at, '[)') && tstzrange(%s, %s, '[)')
            LIMIT 1
            """,
            (bed_id, check_in_at, check_out_at),
        )
        if cur.fetchone():
            raise BedUnavailable("that bed is currently held for another guest")

        cur.execute(
            """
            INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, status,
                                  food_included, agreed_daily_rate, charges_total, source)
            VALUES (%s, %s, %s, %s, 'REQUESTED', %s, %s, %s, %s)
            RETURNING booking_id, reference
            """,
            (guest_id, bed_id, check_in_at, check_out_at, food_included,
             quote.unit_rate, quote.charges_total, source),
        )
        booking = cur.fetchone()

        expires_at = datetime.now(check_in_at.tzinfo) + timedelta(minutes=hold_minutes)
        cur.execute(
            """
            INSERT INTO bed_holds (bed_id, guest_id, start_at, end_at, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, 'ACTIVE') RETURNING hold_id
            """,
            (bed_id, guest_id, check_in_at, check_out_at, expires_at),
        )
        hold_id = str(cur.fetchone()["hold_id"])

        _audit(cur, phone, "booking_requested", "booking", str(booking["booking_id"]),
               None, {"status": "REQUESTED", "bed_id": bed_id,
                      "check_in_at": check_in_at, "check_out_at": check_out_at})

    return BookingRequest(
        booking_id=str(booking["booking_id"]),
        reference=booking["reference"],
        guest_id=guest_id,
        bed_id=bed_id,
        check_in_at=check_in_at,
        check_out_at=check_out_at,
        status="REQUESTED",
        charges_total=quote.charges_total,
        hold_id=hold_id,
        hold_expires_at=expires_at,
        quote=quote,
    )


def approve(db: Database, booking_id: str, *, actor: str, override: bool = False,
            reason: str | None = None) -> dict:
    """Owner approval.

    A hard conflict with another confirmed or in-house booking is never
    bypassable: the database exclusion constraint guarantees it, and this check
    turns that into a clear message. `override` exists for exceptional cases
    where the owner knowingly places a guest on a bed that is not ready, such as
    one still marked for cleaning or maintenance. Every override is audited.
    """
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")

        _require_transition(booking["status"], "CONFIRMED")

        cur.execute(
            """
            SELECT 1 FROM bookings
            WHERE bed_id = %s
              AND status = ANY(ARRAY['CONFIRMED','CHECKED_IN'])
              AND booking_id <> %s
              AND tstzrange(check_in_at, check_out_at, '[)')
                  && tstzrange(%s, %s, '[)')
            LIMIT 1
            """,
            (booking["bed_id"], booking_id, booking["check_in_at"], booking["check_out_at"]),
        )
        if cur.fetchone():
            raise BedUnavailable("that bed was taken while the request was pending")

        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (booking["bed_id"],))
        bed = cur.fetchone()
        if bed and bed["status"] in ("CLEANING", "MAINTENANCE", "UNAVAILABLE") and not override:
            raise BedUnavailable(
                f"bed {booking['bed_id']} is marked {bed['status']}; "
                "use an owner override to place a guest anyway"
            )

        cur.execute(
            "UPDATE bookings SET status = 'CONFIRMED', approved_by = %s, approved_at = now() "
            "WHERE booking_id = %s",
            (actor, booking_id),
        )
        cur.execute(
            "UPDATE bed_holds SET status = 'CONVERTED' "
            "WHERE bed_id = %s AND status = 'ACTIVE' "
            "AND tstzrange(start_at, end_at, '[)') && tstzrange(%s, %s, '[)')",
            (booking["bed_id"], booking["check_in_at"], booking["check_out_at"]),
        )
        _audit(cur, actor, "booking_approved" + ("_override" if override else ""),
               "booking", booking_id, {"status": booking["status"]},
               {"status": "CONFIRMED", "override": override, "reason": reason})

    return get_booking(db, booking_id)


def reject(db: Database, booking_id: str, *, actor: str, reason: str | None = None) -> dict:
    return _close(db, booking_id, actor=actor, target="REJECTED", reason=reason,
                  action="booking_rejected")


def cancel(db: Database, booking_id: str, *, actor: str, reason: str | None = None) -> dict:
    return _close(db, booking_id, actor=actor, target="CANCELLED", reason=reason,
                  action="booking_cancelled")


def _close(db: Database, booking_id: str, *, actor: str, target: str,
           reason: str | None, action: str) -> dict:
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], target)
        cur.execute("UPDATE bookings SET status = %s WHERE booking_id = %s", (target, booking_id))
        cur.execute(
            "UPDATE bed_holds SET status = 'RELEASED' "
            "WHERE bed_id = %s AND status = 'ACTIVE' "
            "AND tstzrange(start_at, end_at, '[)') && tstzrange(%s, %s, '[)')",
            (booking["bed_id"], booking["check_in_at"], booking["check_out_at"]),
        )
        _audit(cur, actor, action, "booking", booking_id,
               {"status": booking["status"]}, {"status": target, "reason": reason})
    return get_booking(db, booking_id)


def check_in(db: Database, booking_id: str, *, actor: str) -> dict:
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "CHECKED_IN")
        cur.execute("UPDATE bookings SET status = 'CHECKED_IN' WHERE booking_id = %s",
                    (booking_id,))
        cur.execute("UPDATE beds SET status = 'OCCUPIED' WHERE bed_id = %s",
                    (booking["bed_id"],))
        _audit(cur, actor, "checked_in", "booking", booking_id,
               {"status": booking["status"]}, {"status": "CHECKED_IN"})
    return get_booking(db, booking_id)


def check_out(db: Database, booking_id: str, *, actor: str) -> dict:
    """Check out and put the bed into CLEANING, ready for the next guest."""
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "CHECKED_OUT")
        cur.execute("UPDATE bookings SET status = 'CHECKED_OUT' WHERE booking_id = %s",
                    (booking_id,))
        cur.execute("UPDATE beds SET status = 'CLEANING' WHERE bed_id = %s", (booking["bed_id"],))
        _audit(cur, actor, "checked_out", "booking", booking_id,
               {"status": booking["status"]}, {"status": "CHECKED_OUT"})
    return get_booking(db, booking_id)


def mark_bed_ready(db: Database, bed_id: str, *, actor: str) -> None:
    """Cleaning finished: the bed becomes AVAILABLE again."""
    with db.transaction() as cur:
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        bed = cur.fetchone()
        if not bed:
            raise NotFound(f"bed {bed_id} not found")
        cur.execute("UPDATE beds SET status = 'AVAILABLE' WHERE bed_id = %s", (bed_id,))
        _audit(cur, actor, "bed_ready", "bed", bed_id,
               {"status": bed["status"]}, {"status": "AVAILABLE"})


def expire_lapsed_requests(db: Database) -> int:
    """Expire requests whose hold has lapsed, so the owner sees what went stale."""
    with db.transaction() as cur:
        cur.execute(
            """
            UPDATE bookings SET status = 'EXPIRED'
            WHERE status = 'REQUESTED'
              AND NOT EXISTS (
                  SELECT 1 FROM bed_holds h
                  WHERE h.bed_id = bookings.bed_id AND h.status = 'ACTIVE'
                    AND h.expires_at > now()
                    AND tstzrange(h.start_at, h.end_at, '[)')
                        && tstzrange(bookings.check_in_at, bookings.check_out_at, '[)')
              )
            RETURNING booking_id
            """
        )
        expired = [str(r["booking_id"]) for r in cur.fetchall()]
        for booking_id in expired:
            _audit(cur, "system", "booking_expired", "booking", booking_id,
                   {"status": "REQUESTED"}, {"status": "EXPIRED"})
        return len(expired)


def pending_requests(db: Database) -> list[dict]:
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.status, bk.check_in_at, bk.check_out_at,
               bk.charges_total, g.full_name, g.phone, bk.bed_id
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status = 'REQUESTED'
        ORDER BY bk.created_at
        """
    )


def arrivals_on(db: Database, day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_in_at, bk.status,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CONFIRMED','CHECKED_IN')
          AND bk.check_in_at >= %s AND bk.check_in_at < %s
        ORDER BY bk.check_in_at
        """,
        (start, end),
    )


def departures_on(db: Database, day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_out_at, bk.status,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CONFIRMED','CHECKED_IN','CHECKED_OUT')
          AND bk.check_out_at >= %s AND bk.check_out_at < %s
        ORDER BY bk.check_out_at
        """,
        (start, end),
    )


def overdue_checkouts(db: Database, now: datetime | None = None) -> list[dict]:
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_out_at,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CHECKED_IN','CONFIRMED')
          AND bk.check_out_at < COALESCE(%s, now())
        ORDER BY bk.check_out_at
        """,
        (now,),
    )


def in_house(db: Database) -> list[dict]:
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_in_at, bk.check_out_at,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status = 'CHECKED_IN'
        ORDER BY bk.check_out_at
        """
    )
