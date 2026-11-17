"""Hold a guest to "wait for the owner" until the owner has decided.

After a guest makes a booking request, the honest answer to almost anything they
send is the same: the request is with the owner, please wait. Two things must not
happen in that window:

  * A second booking request. A guest who taps Book a Room again, or who taps the
    whole flow through again, must not pile up a duplicate request and two bed
    holds for the owner to sort out.
  * An invented answer. The AI must not promise the bed is confirmed, or quote a
    figure the pricing engine did not compute.

So the guest's messages are answered here, in code, before the router or the AI
ever see them. The reply is always the truth: what the request is, and that the
owner will confirm it. Nothing here computes an amount.
"""

from __future__ import annotations

from core import booking, hostel_facts
from core.db import Database

# Statuses where the owner has not yet decided. Until the request leaves one of
# these, the guest is waiting.
WAITING_STATUSES = frozenset({"REQUESTED", "ACCEPTED", "PROOF_SENT"})

# Statuses where the bed is booked. The guest may still send messages, but a new
# request would be a double booking.
BOOKED_STATUSES = frozenset({"CONFIRMED", "CHECKED_IN"})

# Statuses where a walk-in service is settled.
DONE_STATUSES = frozenset({"CONFIRMED", "COMPLETED"})

STATUS_WORDS = {
    "REQUESTED": "waiting for the owner to confirm your reservation and payment",
    "ACCEPTED": "waiting for the owner to confirm your payment",
    "PROOF_SENT": "waiting for the owner to check your payment",
    "CONFIRMED": "confirmed",
    "CHECKED_IN": "confirmed, and you are checked in",
}


def reply_for(db: Database, *, phone: str, text: str | None = None) -> str | None:
    """The holding reply, or None when the guest has no live booking or service.

    Returns None the moment the owner has cancelled or rejected the request, so a
    guest who was refused is free to ask questions and book again.
    """
    live = booking.live_booking_for(db, phone)
    if not live:
        return _service_reply(db, phone=phone)
    status = live["status"]
    if status not in WAITING_STATUSES and status not in BOOKED_STATUSES:
        return None

    reference = live["reference"]
    upi = hostel_facts.get(db, "payment.upi_id") or "8688397996@postbank"
    check_in = _day(live.get("check_in_at"))
    check_out = _day(live.get("check_out_at"))
    dates = f"{check_in} to {check_out}" if check_in and check_out else "your dates"

    if status in BOOKED_STATUSES:
        head = (f"Your booking {reference} is {STATUS_WORDS[status]}. "
                f"Room for {dates}. Total Rs {_rupees(live.get('charges_total'))}.")
        tail = ("If you need to change it, please tell the owner and they will "
                "update it for you.")
    else:
        head = (f"Your booking request {reference} for {dates} is with the owner. "
                f"It is {STATUS_WORDS[status]}. "
                f"Total Rs {_rupees(live.get('charges_total'))}.")
        tail = ("Please wait for the owner's final confirmation. Your bed is not "
                "held until they confirm.")

    lines = [
        head,
        tail,
        "",
        f"If you have not paid yet, pay to UPI: {upi} and send the screenshot here.",
        "I cannot start another booking until the owner decides on this one. "
        "You can still ask me about prices, directions or facilities.",
    ]
    return "\n".join(lines)


def _day(value) -> str | None:
    return value.date().isoformat() if value else None


def _service_reply(db: Database, *, phone: str) -> str | None:
    """The holding reply for a live walk-in service, or None.

    A fresh-up or a rest follows the same two approvals as a booking, so the
    same promise holds: whatever the guest sends, the answer is "wait for the
    owner". Its price is quoted from the record, never recomputed here.
    """
    from core import services
    order = services.live_order_for(db, phone)
    if not order:
        return None
    status = order["status"]
    if status not in WAITING_STATUSES and status not in DONE_STATUSES:
        return None
    reference = order["reference"]
    name = order["service_name"]
    amount = _rupees(order["amount"])
    upi = hostel_facts.get(db, "payment.upi_id") or "8688397996@postbank"
    if status in DONE_STATUSES:
        head = (f"Your {name} {reference} is confirmed. Amount Rs {amount}.")
        tail = ("If you need anything else, please tell the owner and they will "
                "help you.")
    else:
        head = (f"Your {name} request {reference} is with the owner. "
                f"Amount Rs {amount}. It is "
                f"{STATUS_WORDS.get(status, 'waiting for the owner to confirm')}.")
        tail = ("Please wait for the owner's final confirmation. It is not held "
                "until they confirm.")
    return "\n".join([
        head,
        tail,
        "",
        f"If you have not paid yet, pay to UPI: {upi} and send the screenshot here.",
        "I cannot start another request until the owner decides on this one. "
        "You can still ask me about prices, directions or facilities.",
    ])


def _rupees(value) -> str:
    if value is None:
        return "0"
    try:
        from decimal import Decimal
        amount = Decimal(str(value))
    except Exception:
        return str(value)
    whole = int(amount)
    paise = int((amount - whole) * 100)
    return f"{whole:,}" + (f".{paise:02d}" if paise else "")
