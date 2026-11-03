"""Owner command handling, with confirmation before anything irreversible.

The owner types a command in plain English. A command that changes data is
staged first and applied only after the owner confirms. The staged command is
stored against the conversation, so a confirmation must come from the same
number and is single-use.

This module never talks to the channel directly; it returns text to send.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import booking, payments, security
from core.db import Database
from core.errors import DomainError

CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "do it", "go ahead", "approve"}
CANCEL_WORDS = {"no", "cancel", "stop", "don't", "dont", "abort"}

# Words that must never be mistaken for a booking reference.
_FILLER = {"the", "a", "an", "that", "this", "it", "booking", "request", "guest",
           "payment", "for", "to", "and", "please"}

# Commands that change data and therefore need confirmation.
SENSITIVE = {"approve_booking", "reject_booking", "cancel_booking", "check_in",
             "check_out", "verify_payment", "record_payment", "mark_bed_ready"}

_PENDING_KEY = "pending_command"

# Booking references are stored uppercase: SPG-2026-000123.
_REFERENCE = re.compile(r"\b(spg-\d{4}-\d{6})\b", re.I)


def _reference_from(text: str, tail: str | None) -> str | None:
    """Pull a booking reference out of a command, ignoring filler words."""
    found = _REFERENCE.search(text)
    if found:
        return found.group(1).upper()
    candidate = (tail or "").strip()
    if candidate and candidate not in _FILLER:
        return candidate.upper()
    return None


@dataclass(frozen=True)
class CommandResult:
    text: str
    applied: bool = False
    action: str | None = None


def _set_pending(db: Database, phone: str, command: dict) -> None:
    from core import inbox
    state = inbox.get_conversation_state(db, phone)
    state[_PENDING_KEY] = command
    inbox.set_conversation_state(db, phone, state)


def _get_pending(db: Database, phone: str) -> dict | None:
    from core import inbox
    return inbox.get_conversation_state(db, phone).get(_PENDING_KEY)


def _clear_pending(db: Database, phone: str) -> None:
    from core import inbox
    state = inbox.get_conversation_state(db, phone)
    state.pop(_PENDING_KEY, None)
    inbox.set_conversation_state(db, phone, state)


def is_confirmation(text: str) -> bool:
    return (text or "").strip().lower() in CONFIRM_WORDS


def is_cancellation(text: str) -> bool:
    return (text or "").strip().lower() in CANCEL_WORDS


def parse_command(text: str) -> dict | None:
    """Recognise an owner command in plain English. None if it is not one."""
    lowered = (text or "").lower().strip()

    match = re.search(r"\bapprove\b\s*(?:booking|request)?\s*([A-Za-z0-9-]+)?", lowered)
    if match and "approve" in lowered:
        return {"action": "approve_booking",
                "reference": _reference_from(lowered, match.group(1)),
                "override": "override" in lowered}

    match = re.search(r"\breject\b\s*(?:booking|request)?\s*([A-Za-z0-9-]+)?", lowered)
    if match and "reject" in lowered:
        return {"action": "reject_booking",
                "reference": _reference_from(lowered, match.group(1))}

    match = re.search(r"\bcancel\b\s*(?:booking|request)?\s*([A-Za-z0-9-]+)?", lowered)
    if match and "cancel" in lowered:
        return {"action": "cancel_booking",
                "reference": _reference_from(lowered, match.group(1))}

    normalised = lowered.replace("check-in", "check in").replace("check-out", "check out")

    match = re.search(r"\bcheck\s*in\b\s*(?:booking|guest)?\s*([A-Za-z0-9-]+)?", normalised)
    if match and "check in" in normalised:
        return {"action": "check_in",
                "reference": _reference_from(lowered, match.group(1))}

    match = re.search(r"\bcheck\s*out\b\s*(?:booking|guest)?\s*([A-Za-z0-9-]+)?", normalised)
    if match and "check out" in normalised:
        return {"action": "check_out",
                "reference": _reference_from(lowered, match.group(1))}

    match = re.search(r"\bbed\s*([A-Za-z0-9-]+)\s*(?:is\s*)?ready\b", lowered)
    if match:
        return {"action": "mark_bed_ready", "bed_id": match.group(1).upper()}

    match = re.search(r"\bverify\s*payment\s*([A-Za-z0-9-]+)?", lowered)
    if match:
        return {"action": "verify_payment",
                "reference": _reference_from(lowered, match.group(1))}

    match = re.search(
        r"\brecord\s*(?:a\s*)?(?:payment|advance)?\s*(?:of\s*)?(?:rs\.?|inr)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:by|via)?\s*(upi|cash|bank transfer|bank)?"
        r".*?(?:for|against)\s*([A-Za-z0-9-]+)?", lowered)
    if match and "record" in lowered:
        method = (match.group(2) or "cash").upper().replace(" ", "_")
        return {"action": "record_payment", "amount": match.group(1),
                "method": method,
                "reference": _reference_from(lowered, match.group(3))}

    return None


def handle(db: Database, *, phone: str, text: str, owner_phone: str | None) -> CommandResult | None:
    """Handle an owner message that may be a command.

    Returns None when the message is not a command, so the caller can fall back
    to the question-answering path.
    """
    if security.role_for(phone, owner_phone) != security.OWNER:
        return None

    pending = _get_pending(db, phone)

    if pending and is_cancellation(text):
        _clear_pending(db, phone)
        return CommandResult(text="Cancelled. Nothing was changed.")

    if pending and is_confirmation(text):
        _clear_pending(db, phone)
        return _apply(db, phone, pending)

    if pending:
        # A new command replaces the staged one.
        _clear_pending(db, phone)

    command = parse_command(text)
    if not command:
        return None

    if command["action"] not in SENSITIVE:
        return _apply(db, phone, command)

    if command["action"] != "mark_bed_ready" and not command.get("reference"):
        # If exactly one booking is waiting, resolve it for the owner.
        resolved = _maybe_single_reference(db, command["action"])
        if resolved:
            command = {**command, "reference": resolved}
        else:
            return CommandResult(
                text="Which booking? Please send the reference, for example "
                     "SPG-2026-000012.")

    _set_pending(db, phone, command)
    return CommandResult(text=f"{_describe(command)}\n\nReply YES to confirm, or NO to cancel.")


def _maybe_single_reference(db: Database, action: str) -> str | None:
    """The one waiting booking's reference, when exactly one is waiting."""
    if action in ("verify_payment", "record_payment"):
        rows = payments.unverified_payments(db)
    else:
        rows = booking.pending_requests(db)
    if len(rows) == 1:
        return rows[0].get("reference") or rows[0].get("booking_reference")
    return None


def _describe(command: dict) -> str:
    action = command["action"]
    ref = command.get("reference") or command.get("bed_id")
    if action == "approve_booking":
        return f"Approve booking {ref} and confirm it to the guest?"
    if action == "reject_booking":
        return f"Reject booking {ref} and release the bed?"
    if action == "cancel_booking":
        return f"Cancel booking {ref} and release the bed?"
    if action == "check_in":
        return f"Check in booking {ref}?"
    if action == "check_out":
        return f"Check out booking {ref} and mark the bed for cleaning?"
    if action == "mark_bed_ready":
        return f"Mark bed {ref} as clean and available?"
    if action == "verify_payment":
        return f"Verify payment {ref} as received?"
    if action == "record_payment":
        return (f"Record a payment of Rs {command.get('amount')} by "
                f"{command.get('method')} for booking {ref}?")
    return f"Apply {action} to {ref}?"


def _apply(db: Database, actor: str, command: dict) -> CommandResult:
    """Apply a staged or non-sensitive command and report the outcome."""
    action = command["action"]
    try:
        if action == "approve_booking":
            row = _resolve_booking(db, command.get("reference"))
            booking.approve(db, str(row["booking_id"]), actor=actor,
                            override=bool(command.get("override")),
                            reason="owner command")
            return CommandResult(
                text=f"Booking {row['reference']} is confirmed. "
                     f"The guest can be told their bed is held.",
                applied=True, action=action)

        if action == "reject_booking":
            row = _resolve_booking(db, command.get("reference"))
            booking.reject(db, str(row["booking_id"]), actor=actor, reason="owner command")
            return CommandResult(
                text=f"Booking {row['reference']} is rejected and the bed is free.",
                applied=True, action=action)

        if action == "cancel_booking":
            row = _resolve_booking(db, command.get("reference"))
            booking.cancel(db, str(row["booking_id"]), actor=actor, reason="owner command")
            return CommandResult(text=f"Booking {row['reference']} is cancelled.",
                                 applied=True, action=action)

        if action == "check_in":
            row = _resolve_booking(db, command.get("reference"))
            booking.check_in(db, str(row["booking_id"]), actor=actor)
            return CommandResult(
                text=f"{row['reference']} checked in to bed {row['bed_id']}.",
                applied=True, action=action)

        if action == "check_out":
            row = _resolve_booking(db, command.get("reference"))
            booking.check_out(db, str(row["booking_id"]), actor=actor)
            return CommandResult(
                text=f"{row['reference']} checked out. Bed {row['bed_id']} is now "
                     f"marked for cleaning.",
                applied=True, action=action)

        if action == "mark_bed_ready":
            booking.mark_bed_ready(db, command["bed_id"], actor=actor)
            return CommandResult(text=f"Bed {command['bed_id']} is available again.",
                                 applied=True, action=action)

        if action == "verify_payment":
            row = _resolve_payment(db, command.get("reference"))
            payments.verify_payment(db, str(row["payment_id"]), actor=actor,
                                    note="owner command")
            return CommandResult(text="Payment verified as received.",
                                 applied=True, action=action)

        if action == "record_payment":
            row = _resolve_booking(db, command.get("reference"))
            payment_id = payments.record_payment(
                db, booking_id=str(row["booking_id"]), guest_id=None,
                amount=command["amount"], method=command.get("method") or "CASH",
                actor=actor, notes="owner command")
            return CommandResult(
                text=f"Recorded Rs {command['amount']} against {row['reference']}. "
                     f"Send 'verify payment {row['reference']}' once you have confirmed "
                     f"it, or verify it now with 'verify payment {row['reference']}'.",
                applied=True, action=action)

    except DomainError as exc:
        return CommandResult(text=f"That did not work: {exc.message}")

    return CommandResult(text="I did not recognise that command.")


def _resolve_booking(db: Database, reference: str | None) -> dict:
    """Find a booking by reference, or the single one if only one is waiting."""
    from core.errors import NotFound, ValidationError
    if not reference:
        rows = booking.pending_requests(db)
        if len(rows) == 1:
            return rows[0]
        raise ValidationError("please include the booking reference")
    row = db.query_one(
        "SELECT * FROM bookings WHERE reference = %(ref)s OR booking_id::text = %(ref)s",
        {"ref": reference})
    if not row:
        raise NotFound(f"no booking found for {reference}")
    return row


def _resolve_payment(db: Database, reference: str | None) -> dict:
    """Find an unverified payment by booking reference or payment id."""
    from core.errors import NotFound, ValidationError
    if not reference:
        rows = payments.unverified_payments(db)
        if len(rows) == 1:
            return rows[0]
        raise ValidationError("please include the booking reference")
    row = db.query_one(
        """
        SELECT p.* FROM payments p
        LEFT JOIN bookings bk ON bk.booking_id = p.booking_id
        WHERE p.verified = false
          AND (bk.reference = %(ref)s OR p.payment_id::text = %(ref)s)
        ORDER BY p.paid_at DESC LIMIT 1
        """,
        {"ref": reference})
    if not row:
        raise NotFound(f"no unverified payment found for {reference}")
    return row
