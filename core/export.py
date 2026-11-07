"""Owner data files: customers, bookings and payments as CSV.

Every figure is read from the database, so a file can never disagree with the
answer the bot gives in the chat.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from core import billing, payments
from core.db import Database
from core.pricing import money


@dataclass(frozen=True)
class DataFile:
    """A file ready to send. `content` is the exact bytes the owner receives."""

    filename: str
    content: bytes
    media_type: str = "text/csv"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8-sig")


def _rupees(value) -> str:
    return f"{money(value):.2f}"


def _safe(value) -> str:
    """Stop a spreadsheet treating guest input as a formula.

    Excel runs a cell that starts with =, +, - or @ as a formula, so a guest
    could type a name that reads the owner's other cells or runs a command.
    A leading apostrophe makes Excel treat the value as literal text.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _phone(value) -> str:
    """Digits only, so Excel shows a number rather than text.

    A leading '+' makes Excel read the cell as a formula, so the plus is
    dropped. The country code is still in the digits.
    """
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _csv(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    # utf-8-sig so Excel shows Indian names and the rupee sign correctly.
    buffer.write("\ufeff")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M")


def customer_file(db: Database, now: datetime) -> DataFile:
    """Every guest, with their latest stay and money. Owner only."""
    rows = db.query_all(
        """
        SELECT g.guest_id, g.full_name, g.phone, g.gender, g.notes, g.created_at,
               b.booking_id, b.reference, b.status, b.bed_id, b.check_in_at,
               b.check_out_at
        FROM guests g
        LEFT JOIN LATERAL (
            SELECT booking_id, reference, status, bed_id, check_in_at, check_out_at
            FROM bookings WHERE guest_id = g.guest_id
            ORDER BY created_at DESC LIMIT 1
        ) b ON true
        WHERE g.is_owner IS NOT TRUE AND g.is_staff IS NOT TRUE
        ORDER BY g.created_at DESC
        """)

    out: list[list[str]] = []
    for row in rows:
        bill = paid = due = ""
        if row["reference"]:
            balance = payments.get_balance(db, str(row["booking_id"]))
            bill, paid, due = (_rupees(balance.charges_total),
                               _rupees(balance.verified_paid),
                               _rupees(balance.balance_due))
        out.append([
            _safe(row["full_name"]),
            _phone(row["phone"]),
            _safe(row["gender"] or "UNKNOWN"),
            _safe(row["notes"]),
            row["created_at"].strftime("%Y-%m-%d"),
            _safe(row["reference"] or ""),
            _safe(row["status"] or ""),
            _safe(row["bed_id"] or ""),
            row["check_in_at"].strftime("%Y-%m-%d") if row["check_in_at"] else "",
            row["check_out_at"].strftime("%Y-%m-%d") if row["check_out_at"] else "",
            bill, paid, due,
        ])

    header = ["name", "phone", "gender", "notes", "since",
              "latest_booking", "status", "bed", "check_in", "check_out",
              "bill", "paid", "due"]
    return DataFile(f"customers-{_stamp(now)}.csv", _csv(header, out))


def booking_file(db: Database, now: datetime) -> DataFile:
    """Every booking, newest first, with its money. Owner only."""
    rows = db.query_all(
        """
        SELECT b.booking_id, b.reference, b.status, b.bed_id, bd.room_id,
               b.check_in_at, b.check_out_at, b.food_included,
               g.full_name, g.phone
        FROM bookings b
        LEFT JOIN guests g ON g.guest_id = b.guest_id
        LEFT JOIN beds bd ON bd.bed_id = b.bed_id
        ORDER BY b.created_at DESC
        """)
    out = []
    for row in rows:
        balance = payments.get_balance(db, str(row["booking_id"]))
        extras = billing.booking_extras_total(db, str(row["booking_id"]))
        out.append([
            _safe(row["reference"]),
            _safe(row["status"]),
            _safe(row["full_name"]),
            _phone(row["phone"]),
            _safe(row["bed_id"]),
            _safe(row["room_id"]),
            row["check_in_at"].strftime("%Y-%m-%d"),
            row["check_out_at"].strftime("%Y-%m-%d"),
            "yes" if row["food_included"] else "no",
            _rupees(balance.charges_total),
            _rupees(extras),
            _rupees(balance.verified_paid),
            _rupees(balance.balance_due),
        ])
    header = ["reference", "status", "guest", "phone", "bed", "room",
              "check_in", "check_out", "food", "bill", "extras",
              "paid", "due"]
    return DataFile(f"bookings-{_stamp(now)}.csv", _csv(header, out))


def payment_file(db: Database, now: datetime) -> DataFile:
    """Every payment, newest first, verified or not. Owner only."""
    rows = db.query_all(
        """
        SELECT p.amount, p.kind, p.method, p.txn_ref, p.verified, p.paid_at,
               b.reference AS booking_reference,
               COALESCE(g.full_name, bg.full_name) AS full_name,
               COALESCE(g.phone, bg.phone) AS phone
        FROM payments p
        LEFT JOIN bookings b ON b.booking_id = p.booking_id
        LEFT JOIN guests g ON g.guest_id = p.guest_id
        LEFT JOIN guests bg ON bg.guest_id = b.guest_id
        ORDER BY p.paid_at DESC
        """)
    out = []
    for row in rows:
        out.append([
            row["paid_at"].strftime("%Y-%m-%d %H:%M"),
            _rupees(row["amount"]),
            _safe(row["kind"]),
            _safe(row["method"]),
            _safe(row["txn_ref"]),
            "yes" if row["verified"] else "no",
            _safe(row["booking_reference"]),
            _safe(row["full_name"]),
            _phone(row["phone"]),
        ])
    header = ["date", "amount", "kind", "method", "reference", "verified",
              "booking", "guest", "phone"]
    return DataFile(f"payments-{_stamp(now)}.csv", _csv(header, out))


# The names the owner can ask for, mapped to the builder.
BUILDERS = {
    "customers": customer_file,
    "bookings": booking_file,
    "payments": payment_file,
}


def build(db: Database, what: str, now: datetime) -> DataFile | None:
    """The file for `what`, or None if that is not a kind we publish."""
    builder = BUILDERS.get(what.strip().lower())
    return builder(db, now) if builder else None
