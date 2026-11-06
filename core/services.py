"""Walk-in services: a fresh-up and a short rest.

A fresh-up is a wash and change with no bed. A three-hour rest uses a bed for
its three hours and then gives it back, so the same bed can still be sold for
the night. Neither is a booking, so neither is counted as occupancy -- that is
the whole reason they live in their own table.

The two-step approval is the same one a booking gets: the owner accepts, the
guest pays, the owner confirms. Every amount comes from the rate card through
`core.pricing`; nothing here computes a price, and neither service is ever
discounted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import booking, pricing
from core.db import Database
from core.errors import BedUnavailable, NotFound, ValidationError
from core.pricing import money

# Statuses where the owner has not decided yet, so the guest is waiting.
WAITING_STATUSES = frozenset({"REQUESTED", "ACCEPTED", "PROOF_SENT"})
DONE_STATUSES = frozenset({"CONFIRMED", "COMPLETED"})


@dataclass(frozen=True)
class ServiceOrder:
    service_order_id: str
    reference: str
    service_code: str
    guest_id: str
    bed_id: str | None
    starts_at: datetime
    ends_at: datetime
    amount: object
    status: str


def _audit(cur, actor: str, action: str, entity_id: str, before=None, after=None) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, 'service_order', %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def get_order(db: Database, reference: str) -> dict:
    row = db.query_one(
        """
        SELECT so.*, g.phone, g.full_name, s.name AS service_name
        FROM service_orders so
        JOIN guests g ON g.guest_id = so.guest_id
        JOIN services s ON s.service_code = so.service_code
        WHERE so.reference = %s OR so.service_order_id::text = %s
        """,
        (reference, reference),
    )
    if not row:
        raise NotFound(f"no service order {reference}")
    return row


def live_order_for(db: Database, phone: str) -> dict | None:
    """This guest's service order that is still going, or None."""
    return db.query_one(
        """
        SELECT so.*, s.name AS service_name
        FROM service_orders so
        JOIN guests g ON g.guest_id = so.guest_id
        JOIN services s ON s.service_code = so.service_code
        WHERE g.phone = %s
          AND so.status NOT IN ('CANCELLED', 'REJECTED', 'COMPLETED')
        ORDER BY so.created_at DESC LIMIT 1
        """,
        (phone,),
    )


def create_order(db: Database, *, phone: str, service_code: str,
                 full_name: str | None = None, bed_id: str | None = None,
                 starts_at: datetime | None = None,
                 duration_minutes: int | None = None,
                 source: str = "WHATSAPP") -> ServiceOrder:
    """Record a service request. Priced from the rate card, never guessed."""
    service = pricing.get_service(db, service_code)
    starts_at = starts_at or datetime.now().astimezone()
    minutes = int(duration_minutes if duration_minutes is not None
                  else service.duration_minutes)
    if minutes <= 0:
        # A fresh-up has no duration; it is a moment, not a stay. Give it a
        # nominal window so the record is well-formed.
        minutes = 30
    ends_at = starts_at + timedelta(minutes=minutes)

    if service.needs_bed and not bed_id:
        raise ValidationError(f"{service.name} needs a bed assigned")
    if not service.needs_bed:
        bed_id = None

    amount = money(service.price)
    guest_id = booking.get_or_create_guest(db, phone=phone, full_name=full_name)

    with db.transaction() as cur:
        if bed_id:
            cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
            bed = cur.fetchone()
            if not bed:
                raise NotFound(f"bed {bed_id} does not exist")
            if bed["status"] in ("CLEANING", "MAINTENANCE", "UNAVAILABLE"):
                raise BedUnavailable(f"bed {bed_id} is marked {bed['status']}")
            cur.execute(
                """
                SELECT 1 FROM service_orders
                WHERE bed_id = %s AND status = 'CONFIRMED'
                  AND tstzrange(starts_at, ends_at, '[)') && tstzrange(%s, %s, '[)')
                LIMIT 1
                """,
                (bed_id, starts_at, ends_at),
            )
            if cur.fetchone():
                raise BedUnavailable(f"bed {bed_id} is busy for that window")
            # A confirmed bed booking also blocks a rest in the same window.
            cur.execute(
                """
                SELECT 1 FROM booking_beds
                WHERE bed_id = %s AND status IN ('CONFIRMED','CHECKED_IN')
                  AND tstzrange(check_in_at, check_out_at, '[)')
                      && tstzrange(%s, %s, '[)')
                LIMIT 1
                """,
                (bed_id, starts_at, ends_at),
            )
            if cur.fetchone():
                raise BedUnavailable(f"bed {bed_id} is booked for that window")

        cur.execute(
            """
            INSERT INTO service_orders (service_code, guest_id, bed_id, starts_at,
                                        ends_at, amount, status, source)
            VALUES (%s, %s, %s, %s, %s, %s, 'REQUESTED', %s)
            RETURNING service_order_id, reference
            """,
            (service_code, guest_id, bed_id, starts_at, ends_at, amount, source),
        )
        row = cur.fetchone()
        _audit(cur, phone, "service_requested", str(row["service_order_id"]), None,
               {"service_code": service_code, "amount": str(amount),
                "bed_id": bed_id, "starts_at": starts_at, "ends_at": ends_at})

    return ServiceOrder(
        service_order_id=str(row["service_order_id"]),
        reference=row["reference"],
        service_code=service_code,
        guest_id=guest_id,
        bed_id=bed_id,
        starts_at=starts_at,
        ends_at=ends_at,
        amount=amount,
        status="REQUESTED",
    )


def _transition(db: Database, reference: str, *, target: str, actor: str,
                from_statuses: tuple[str, ...], stamp: str | None = None) -> dict:
    """Move one order to a new status, refusing an illegal jump.

    `stamp` names the timestamp column to set alongside the status (e.g.
    'accepted_at'). It is chosen from a fixed set of literals by the callers,
    never built from input, so it cannot carry anything into the statement.
    """
    stamps = {"accepted_at", "final_confirmed_at", "payment_proof_at"}
    if stamp is not None and stamp not in stamps:
        raise ValidationError(f"unknown timestamp {stamp!r}")
    with db.transaction() as cur:
        cur.execute("SELECT * FROM service_orders WHERE reference = %s FOR UPDATE",
                    (reference,))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"no service order {reference}")
        if row["status"] not in from_statuses:
            raise ValidationError(
                f"a service order in state {row['status']} cannot become {target}")
        # One literal statement per stamp, so nothing is ever built from input.
        statements = {
            "accepted_at": "UPDATE service_orders SET status = %s, accepted_at = now() "
                           "WHERE reference = %s",
            "final_confirmed_at": "UPDATE service_orders SET status = %s, "
                                  "final_confirmed_at = now() WHERE reference = %s",
            "payment_proof_at": "UPDATE service_orders SET status = %s, "
                                "payment_proof_at = now() WHERE reference = %s",
        }
        if stamp:
            cur.execute(statements[stamp], (target, reference))
        else:
            cur.execute(
                "UPDATE service_orders SET status = %s WHERE reference = %s",
                (target, reference),
            )
        _audit(cur, actor, f"service_{target.lower()}", str(row["service_order_id"]),
               {"status": row["status"]}, {"status": target})
    return get_order(db, reference)


def accept(db: Database, reference: str, *, actor: str) -> dict:
    return _transition(db, reference, target="ACCEPTED", actor=actor,
                       from_statuses=("REQUESTED",), stamp="accepted_at")


def mark_proof_sent(db: Database, reference: str, *, proof_path: str | None = None,
                    actor: str = "guest") -> dict:
    return _transition(db, reference, target="PROOF_SENT", actor=actor,
                       from_statuses=("ACCEPTED",), stamp="payment_proof_at")


def confirm(db: Database, reference: str, *, actor: str) -> dict:
    return _transition(db, reference, target="CONFIRMED", actor=actor,
                       from_statuses=("REQUESTED", "ACCEPTED", "PROOF_SENT"),
                       stamp="final_confirmed_at")


def reject(db: Database, reference: str, *, actor: str) -> dict:
    return _transition(db, reference, target="REJECTED", actor=actor,
                       from_statuses=("REQUESTED", "ACCEPTED", "PROOF_SENT"))


def pending_orders(db: Database) -> list[dict]:
    """Service orders waiting on the owner, whatever the reason."""
    return db.query_all(
        """
        SELECT so.reference, so.status, so.starts_at, so.ends_at, so.amount,
               so.bed_id, s.name AS service_name, g.full_name, g.phone
        FROM service_orders so
        JOIN guests g ON g.guest_id = so.guest_id
        JOIN services s ON s.service_code = so.service_code
        WHERE so.status IN ('REQUESTED', 'PROOF_SENT')
        ORDER BY so.created_at
        """
    )
