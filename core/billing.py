"""Billing: extras on a stay, the booking advance, and invoices.

Three rules hold here.

  * An extra belongs to one stay, and its price is frozen when it is added.
  * The advance is a minimum per guest, it counts toward the bill, and it is
    never refunded. It is stored on the payment, not on the invoice, so a refund
    can never sweep it up by accident.
  * An invoice is a document. Its lines are copied in and never recomputed, so a
    bill reprinted later shows exactly what the guest was given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core import payments
from core.db import Database
from core.errors import NotFound, PaymentError, ValidationError
from core.pricing import money

# The owner may change this; it lives in the settings table, not in code.
ADVANCE_PER_GUEST_KEY = "advance_per_guest"
DEFAULT_ADVANCE_PER_GUEST = Decimal("100")


def _audit(cur, actor: str, action: str, entity: str, entity_id: str,
           before=None, after=None) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


# --- the advance rule ------------------------------------------------------

def advance_per_guest(db: Database) -> Decimal:
    """The minimum advance for one guest. Owner-editable, default 100."""
    row = db.query_one("SELECT value FROM settings WHERE key = %s",
                       (ADVANCE_PER_GUEST_KEY,))
    if not row:
        return DEFAULT_ADVANCE_PER_GUEST
    value = row["value"]
    if isinstance(value, dict):
        value = value.get("amount", value.get("value"))
    return money(value)


def set_advance_per_guest(db: Database, amount, *, actor: str) -> Decimal:
    amount = money(amount)
    if amount < 0:
        raise ValidationError("the advance cannot be negative")
    before = advance_per_guest(db)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (ADVANCE_PER_GUEST_KEY, json.dumps({"amount": str(amount)})),
        )
        _audit(cur, actor, "advance_changed", "settings", ADVANCE_PER_GUEST_KEY,
               {"amount": str(before)}, {"amount": str(amount)})
    return amount


def advance_due(db: Database, guests_count: int) -> Decimal:
    """The advance to hold a bed for a party. Never negative, never zero-guessed."""
    count = int(guests_count or 1)
    if count < 1:
        raise ValidationError("there must be at least one guest")
    return money(advance_per_guest(db) * count)


def advance_notice(db: Database, guests_count: int) -> str:
    """The exact words the guest must see before paying. Not the model's."""
    amount = advance_due(db, guests_count)
    people = int(guests_count or 1)
    party = "person" if people == 1 else "people"
    return (f"To reserve your bed, pay a minimum advance of Rs {amount:,.0f} "
            f"({people} {party}). This advance is not refundable. It counts "
            f"toward your bill, and you may pay the full amount instead if you "
            f"prefer.")


# --- extras on a stay ------------------------------------------------------

def add_extra(db: Database, *, booking_id: str, name: str, quantity: int = 1,
              actor: str) -> dict:
    """Add an extra to a stay, copying the shop price at this moment."""
    if quantity is None or int(quantity) < 1:
        raise ValidationError("an extra quantity must be at least one")
    quantity = int(quantity)

    booking = db.query_one("SELECT booking_id FROM bookings WHERE booking_id = %s",
                           (booking_id,))
    if not booking:
        raise NotFound(f"booking {booking_id} not found")

    row = db.query_one(
        "SELECT extra_id, name, price FROM extras WHERE lower(name) = lower(%s) "
        "AND active", (name,))
    if not row:
        raise NotFound(f"no such extra {name!r}; ask the owner to add it")

    unit_price = money(row["price"])
    amount = money(unit_price * quantity)
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO booking_extras (booking_id, extra_id, name, quantity,
                                        unit_price, amount, added_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING booking_extra_id
            """,
            (booking_id, row["extra_id"], row["name"], quantity, unit_price,
             amount, actor),
        )
        extra_id = str(cur.fetchone()["booking_extra_id"])
        _audit(cur, actor, "extra_added", "booking", booking_id, None,
               {"name": row["name"], "quantity": quantity, "amount": str(amount)})
    return {"booking_extra_id": extra_id, "name": row["name"], "quantity": quantity,
            "unit_price": str(unit_price), "amount": str(amount)}


def list_booking_extras(db: Database, booking_id: str) -> list[dict]:
    return db.query_all(
        "SELECT booking_extra_id, name, quantity, unit_price, amount, created_at "
        "FROM booking_extras WHERE booking_id = %s ORDER BY created_at",
        (booking_id,))


def booking_extras_total(db: Database, booking_id: str) -> Decimal:
    row = db.query_one(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM booking_extras "
        "WHERE booking_id = %s", (booking_id,))
    return money(row["total"])


def remove_extra(db: Database, booking_extra_id: str, *, actor: str) -> None:
    row = db.query_one("SELECT * FROM booking_extras WHERE booking_extra_id = %s",
                       (booking_extra_id,))
    if not row:
        raise NotFound("that extra is not on this booking")
    with db.transaction() as cur:
        cur.execute("DELETE FROM booking_extras WHERE booking_extra_id = %s",
                    (booking_extra_id,))
        _audit(cur, actor, "extra_removed", "booking", str(row["booking_id"]),
               {"name": row["name"], "amount": str(row["amount"])}, None)


# --- invoices --------------------------------------------------------------

@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    number: str
    booking_id: str
    reference: str | None
    status: str
    subtotal: Decimal
    extras_total: Decimal
    discount: Decimal
    total: Decimal
    non_refundable: Decimal
    paid: Decimal
    balance_due: Decimal
    lines: list[dict]

    @property
    def settled(self) -> bool:
        return self.balance_due <= 0


def _load_invoice(db: Database, where: str, params: dict) -> Invoice:
    row = db.query_one(
        f"""
        SELECT i.*, b.reference
        FROM invoices i
        LEFT JOIN bookings b ON b.booking_id = i.booking_id
        WHERE {where}
        """, params)
    if not row:
        raise NotFound("no invoice found")

    lines = db.query_all(
        "SELECT kind, description, quantity, unit_price, amount, non_refundable "
        "FROM invoice_lines WHERE invoice_id = %s ORDER BY position, line_id",
        (row["invoice_id"],))

    balance = payments.get_balance(db, str(row["booking_id"]))
    return Invoice(
        invoice_id=str(row["invoice_id"]),
        number=row["number"],
        booking_id=str(row["booking_id"]),
        reference=row["reference"],
        status=row["status"],
        subtotal=money(row["subtotal"]),
        extras_total=money(row["extras_total"]),
        discount=money(row["discount"]),
        total=money(row["total"]),
        non_refundable=money(row["non_refundable"]),
        paid=money(balance.verified_paid),
        balance_due=money(balance.balance_due),
        lines=lines,
    )


def invoice_for_booking(db: Database, booking_id: str) -> Invoice:
    return _load_invoice(db, "i.booking_id = %(v)s", {"v": booking_id})


def get_invoice(db: Database, number_or_id: str) -> Invoice:
    return _load_invoice(
        db, "(i.number = %(v)s OR i.invoice_id::text = %(v)s)", {"v": number_or_id})


def issue_invoice(db: Database, *, booking_id: str, actor: str,
                  discount=0, notes: str | None = None) -> Invoice:
    """Bill a booking. The lines are copied in and frozen.

    Re-issuing returns the existing invoice, so a booking never has two bills.
    """
    booking = db.query_one(
        "SELECT booking_id, guest_id, reference, charges_total, food_included "
        "FROM bookings WHERE booking_id = %s", (booking_id,))
    if not booking:
        raise NotFound(f"booking {booking_id} not found")

    existing = db.query_one(
        "SELECT invoice_id FROM invoices WHERE booking_id = %s AND status <> 'VOID'",
        (booking_id,))
    if existing:
        return _load_invoice(db, "i.invoice_id = %(v)s",
                             {"v": str(existing["invoice_id"])})

    room_charge = money(booking["charges_total"] or 0)
    extras = list_booking_extras(db, booking_id)
    extras_sum = money(sum((money(e["amount"]) for e in extras), Decimal("0")))
    discount = money(discount)
    if discount < 0:
        raise ValidationError("a discount cannot be negative")
    total = money(room_charge + extras_sum - discount)
    if total < 0:
        raise ValidationError("the discount cannot exceed the bill")

    # Non-refundable money already taken for this booking, e.g. the advance.
    taken = db.query_one(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments "
        "WHERE booking_id = %s AND refundable = false AND kind = 'PAYMENT'",
        (booking_id,))
    non_refundable = money(taken["total"])

    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO invoices (booking_id, guest_id, status, subtotal, extras_total,
                                  discount, total, non_refundable, issued_by, notes)
            VALUES (%s, %s, 'ISSUED', %s, %s, %s, %s, %s, %s, %s)
            RETURNING invoice_id
            """,
            (booking_id, booking["guest_id"], room_charge, extras_sum, discount,
             total, non_refundable, actor, notes),
        )
        invoice_id = str(cur.fetchone()["invoice_id"])

        position = 0
        stay = (f"Accommodation {booking['reference']}"
                + (" with food" if booking["food_included"] else ""))
        cur.execute(
            "INSERT INTO invoice_lines (invoice_id, kind, description, quantity, "
            "unit_price, amount, non_refundable, position) "
            "VALUES (%s, 'ROOM', %s, 1, %s, %s, false, %s)",
            (invoice_id, stay, room_charge, room_charge, position))
        for extra in extras:
            position += 1
            cur.execute(
                "INSERT INTO invoice_lines (invoice_id, kind, description, quantity, "
                "unit_price, amount, non_refundable, position) "
                "VALUES (%s, 'EXTRA', %s, %s, %s, %s, false, %s)",
                (invoice_id, extra["name"], extra["quantity"], extra["unit_price"],
                 extra["amount"], position))
        if discount > 0:
            position += 1
            cur.execute(
                "INSERT INTO invoice_lines (invoice_id, kind, description, quantity, "
                "unit_price, amount, non_refundable, position) "
                "VALUES (%s, 'DISCOUNT', 'Discount', 1, %s, %s, false, %s)",
                (invoice_id, -discount, -discount, position))

        _audit(cur, actor, "invoice_issued", "invoice", invoice_id, None,
               {"booking": booking["reference"], "total": str(total)})

    return _load_invoice(db, "i.invoice_id = %(v)s", {"v": invoice_id})


def settle_invoice(db: Database, invoice_id: str, *, actor: str) -> Invoice:
    """Mark an invoice paid, once verified money covers it."""
    invoice = _load_invoice(db, "i.invoice_id = %(v)s", {"v": invoice_id})
    if invoice.status == "VOID":
        raise PaymentError("that invoice has been voided")
    if not invoice.settled:
        raise PaymentError(
            f"Rs {invoice.balance_due:,.0f} is still outstanding on {invoice.number}")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE invoices SET status = 'PAID', paid_at = now() WHERE invoice_id = %s",
            (invoice_id,))
        _audit(cur, actor, "invoice_paid", "invoice", invoice_id,
               {"status": invoice.status}, {"status": "PAID"})
    return _load_invoice(db, "i.invoice_id = %(v)s", {"v": invoice_id})


def render_invoice(invoice: Invoice) -> str:
    """A plain-text bill, safe to send to a guest."""
    lines = [f"Bill {invoice.number}"]
    if invoice.reference:
        lines.append(f"Booking {invoice.reference}")
    lines.append("")
    for line in invoice.lines:
        qty = f"{line['quantity']:g} x " if line["kind"] == "EXTRA" else ""
        lines.append(f"{line['description']}: {qty}Rs {money(line['amount']):,.0f}")
    lines.append("")
    lines.append(f"Total: Rs {invoice.total:,.0f}")
    lines.append(f"Paid: Rs {invoice.paid:,.0f}")
    if invoice.non_refundable > 0:
        lines.append(f"  (of which Rs {invoice.non_refundable:,.0f} advance is "
                     f"not refundable)")
    lines.append(f"Balance due: Rs {invoice.balance_due:,.0f}")
    if invoice.settled:
        lines.append("")
        lines.append("This bill is settled. Thank you.")
    return "\n".join(lines)
