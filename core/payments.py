"""Payment ledger.

A screenshot is not proof of payment. Money recorded here starts unverified.
Only `verify_payment`, called by an authorised owner or staff member, or a
trusted gateway callback, marks a payment verified. Balances and revenue count
only verified money.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.db import Database
from core.errors import NotFound, PaymentError, ValidationError
from core.pricing import money

VALID_METHODS = ("UPI", "CASH", "BANK_TRANSFER", "GATEWAY", "OTHER")


@dataclass(frozen=True)
class Balance:
    charges_total: Decimal
    verified_paid: Decimal
    unverified_pending: Decimal
    balance_due: Decimal

    @property
    def settled(self) -> bool:
        return self.balance_due <= 0


def _audit(cur, actor: str, action: str, entity: str, entity_id: str,
           before=None, after=None) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def record_payment(db: Database, *, booking_id: str | None, guest_id: str | None,
                   amount, method: str, actor: str, txn_ref: str | None = None,
                   kind: str = "PAYMENT", notes: str | None = None,
                   verified: bool = False, refundable: bool = True,
                   paid_at: datetime | None = None) -> str:
    """Record a payment or refund. Unverified unless a trusted source sets it.

    `refundable=False` marks money the owner will not return, such as the
    booking advance. It is still real money and still counts toward the bill.
    """
    amount = money(amount)
    if amount == 0:
        raise ValidationError("a payment amount cannot be zero")
    if kind == "PAYMENT" and amount < 0:
        raise ValidationError("a payment must be a positive amount; use a refund instead")
    if kind == "REFUND" and amount > 0:
        raise ValidationError("a refund must be recorded as a negative amount")
    if method not in VALID_METHODS:
        raise ValidationError(f"unknown payment method {method!r}")
    if kind not in ("PAYMENT", "REFUND", "ADJUSTMENT"):
        raise ValidationError(f"unknown payment kind {kind!r}")

    if booking_id:
        booking = db.query_one("SELECT booking_id FROM bookings WHERE booking_id = %s",
                               (booking_id,))
        if not booking:
            raise NotFound(f"booking {booking_id} not found")

    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO payments (booking_id, guest_id, amount, kind, method, txn_ref,
                                  paid_at, verified, verified_by, verified_at,
                                  refundable, notes)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s, %s, %s, %s, %s)
            RETURNING payment_id
            """,
            (booking_id, guest_id, amount, kind, method, txn_ref, paid_at,
             verified, actor if verified else None, datetime.now() if verified else None,
             refundable, notes),
        )
        payment_id = str(cur.fetchone()["payment_id"])
        _audit(cur, actor, "payment_recorded", "payment", payment_id, None,
               {"amount": str(amount), "method": method, "kind": kind,
                "verified": verified, "refundable": refundable, "txn_ref": txn_ref})
    return payment_id


def verify_payment(db: Database, payment_id: str, *, actor: str,
                   note: str | None = None) -> dict:
    """Mark a recorded payment as verified. Owner or staff action only."""
    with db.transaction() as cur:
        cur.execute("SELECT * FROM payments WHERE payment_id = %s FOR UPDATE", (payment_id,))
        payment = cur.fetchone()
        if not payment:
            raise NotFound(f"payment {payment_id} not found")
        if payment["verified"]:
            raise PaymentError("that payment is already verified")
        cur.execute(
            "UPDATE payments SET verified = true, verified_by = %s, verified_at = now() "
            "WHERE payment_id = %s",
            (actor, payment_id),
        )
        _audit(cur, actor, "payment_verified", "payment", payment_id,
               {"verified": False}, {"verified": True, "note": note})
    return db.query_one("SELECT * FROM payments WHERE payment_id = %s", (payment_id,))


def get_balance(db: Database, booking_id: str) -> Balance:
    booking = db.query_one(
        "SELECT charges_total FROM bookings WHERE booking_id = %s", (booking_id,)
    )
    if not booking:
        raise NotFound(f"booking {booking_id} not found")

    rows = db.query_all(
        """
        SELECT amount, kind, verified FROM payments WHERE booking_id = %s
        """,
        (booking_id,),
    )
    verified_paid = Decimal("0")
    unverified = Decimal("0")
    for row in rows:
        amount = money(row["amount"])
        if row["verified"]:
            verified_paid += amount
        else:
            unverified += amount

    charges = money(booking["charges_total"] or 0)
    return Balance(
        charges_total=charges,
        verified_paid=money(verified_paid),
        unverified_pending=money(unverified),
        balance_due=money(charges - verified_paid),
    )


def unverified_payments(db: Database) -> list[dict]:
    return db.query_all(
        """
        SELECT p.payment_id, p.amount, p.method, p.txn_ref, p.paid_at,
               bk.reference, g.full_name, g.phone
        FROM payments p
        LEFT JOIN bookings bk ON bk.booking_id = p.booking_id
        LEFT JOIN guests g ON g.guest_id = p.guest_id
        WHERE p.verified = false
        ORDER BY p.paid_at
        """
    )


def outstanding_bookings(db: Database) -> list[dict]:
    """Bookings that still owe money, counting only verified payments."""
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.charges_total, bk.status,
               g.full_name, g.phone,
               COALESCE(SUM(CASE WHEN p.verified THEN p.amount ELSE 0 END), 0) AS paid,
               bk.charges_total - COALESCE(SUM(CASE WHEN p.verified THEN p.amount ELSE 0 END), 0)
                   AS balance_due
        FROM bookings bk
        JOIN guests g ON g.guest_id = bk.guest_id
        LEFT JOIN payments p ON p.booking_id = bk.booking_id
        WHERE bk.status IN ('CONFIRMED','CHECKED_IN','CHECKED_OUT','REQUESTED')
        GROUP BY bk.booking_id, bk.reference, bk.charges_total, bk.status, g.full_name, g.phone
        HAVING bk.charges_total - COALESCE(SUM(CASE WHEN p.verified THEN p.amount ELSE 0 END), 0) > 0
        ORDER BY balance_due DESC
        """
    )


def revenue_between(db: Database, start: datetime, end: datetime,
                    *, include_refunds: bool = True) -> Decimal:
    """Verified revenue in a period. Refunds reduce it; unverified money is excluded."""
    row = db.query_one(
        """
        SELECT COALESCE(SUM(CASE
                   WHEN kind = 'REFUND' AND %(refunds)s THEN amount
                   ELSE amount
               END), 0) AS total
        FROM payments
        WHERE verified = true AND paid_at >= %(start)s AND paid_at < %(end)s
        """,
        {"start": start, "end": end, "refunds": include_refunds},
    )
    return money(row["total"])
