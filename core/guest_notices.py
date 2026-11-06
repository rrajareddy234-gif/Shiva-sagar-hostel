"""The exact words a guest receives about their own booking.

The owner asked for the guest to be told *everything they chose*, not just a
bed number: room, food, dates, nights, people, beds, the money, and how to pay.
Those messages live here, built from the booking row and the rate card, so the
wording has one home and cannot drift between the chat, the dashboard and the
email.

Two rules hold throughout:

* Every amount is read from the stored booking. Nothing is recomputed here and
  nothing is guessed -- the pricing engine already decided the figures, and this
  module only formats them.
* The payment message names the UPI payee and tells the guest to check that
  name before paying. A guest who pays a lookalike UPI id has lost money, and
  the one place that can be prevented is the sentence we send them.
"""

from __future__ import annotations

from decimal import Decimal

from core import hostel_facts
from core.db import Database
from core.pricing import money

# Read from hostel_facts so the owner can change them without a deploy.
UPI_KEY = "payment.upi_id"
PAYEE_KEY = "payment.payee_name"
QR_KEY = "payment.qr_image_url"

# Only used when the fact is missing entirely, so a guest is never left without
# a way to pay. The owner's own values are always preferred.
DEFAULT_UPI = "8688397996@postbank"
DEFAULT_PAYEE = "GUTTIKONDA RAJAVARDHAN REDDY"

ROOM_LABEL = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory"}


def _rupees(value) -> str:
    return f"Rs {money(value or Decimal('0')):,.2f}"


def _details(db: Database, booking: dict) -> list[str]:
    """Every choice the guest made, as lines. Shared by both messages."""
    beds = db.query_all(
        "SELECT bed_id FROM booking_beds WHERE booking_id = %s ORDER BY bed_id",
        (booking["booking_id"],))
    bed_list = [r["bed_id"] for r in beds] or [booking.get("bed_id")]

    room = db.query_one(
        "SELECT r.room_type FROM beds b JOIN rooms r ON r.room_id = b.room_id "
        "WHERE b.bed_id = %s",
        (booking.get("bed_id"),))
    room_type = (room or {}).get("room_type")
    room_label = ROOM_LABEL.get(room_type, room_type or "room")

    check_in = booking["check_in_at"]
    check_out = booking["check_out_at"]
    nights = (check_out.date() - check_in.date()).days
    guests = int(booking.get("guests_count") or 1)
    total = booking.get("charges_total")
    advance = booking.get("advance_due") or Decimal("0")
    terms = booking.get("payment_terms") or "ADVANCE"

    lines = [
        f"Reference: {booking['reference']}",
        f"Room: {room_label}",
        f"Food: {'with food' if booking.get('food_included') else 'no food'}",
        f"Check-in: {check_in.date().isoformat()}",
        f"Check-out: {check_out.date().isoformat()} ({nights} night(s))",
        f"Guests: {guests}",
        f"Bed(s): {', '.join(bed_list)}",
        f"Total: {_rupees(total)}",
    ]
    if terms == "ADVANCE":
        lines.append(f"Pay now to reserve: {_rupees(advance)}")
        lines.append(f"Balance at check-in: {_rupees(money((total or 0) - advance))}")
    else:
        lines.append(f"Pay now (full): {_rupees(total)}")
    return lines


def acceptance_message(db: Database, booking: dict) -> str:
    """What the guest gets the moment the owner accepts: pay, and how.

    The payee name is not decoration. It is the one check a guest can make
    before sending money, so it is stated plainly and the guest is told to stop
    if it does not match.
    """
    upi = hostel_facts.get(db, UPI_KEY) or DEFAULT_UPI
    payee = hostel_facts.get(db, PAYEE_KEY) or DEFAULT_PAYEE
    qr = hostel_facts.get(db, QR_KEY)

    lines = [
        f"Good news -- your request {booking['reference']} is ACCEPTED.",
        "",
        "Your booking details:",
        *_details(db, booking),
        "",
        f"Pay to UPI ID: {upi}",
        f"The name shown must be: {payee}",
        "",
        "IMPORTANT: before you pay, check that the name shown on the UPI ID is "
        f"{payee}. If it shows any other name, do NOT pay and tell us here.",
        "",
        "After paying, send the payment screenshot in this chat.",
        "Your bed is held until we confirm your payment.",
    ]
    if qr:
        lines.append(f"Scan the QR: {qr}")
    return "\n".join(lines)


def final_confirmation_message(db: Database, booking: dict) -> str:
    """What the guest gets after the owner checks the payment.

    The same details again, so the guest has one message with everything in it
    rather than having to scroll back to the acceptance.
    """
    lines = [
        f"Your booking {booking['reference']} is CONFIRMED. Your bed is held.",
        "",
        "Your booking details:",
        *_details(db, booking),
        "",
        "Please keep this message. Show it at check-in.",
    ]
    return "\n".join(lines)


def proof_received_message(booking: dict) -> str:
    """A short acknowledgement, so the guest knows the screenshot arrived."""
    return (
        f"Thank you -- we have received your payment screenshot for "
        f"{booking['reference']}. The owner will check it and confirm your "
        f"booking shortly."
    )


def booking_details(db: Database, booking: dict) -> str:
    """The booking's details alone, with no instruction to pay.

    Used for the owner's copy of a payment screenshot: the owner needs to see
    what the guest booked next to the proof, but must not be told to pay.
    """
    return "\n".join(_details(db, booking))


def rejection_message(db: Database, booking: dict) -> str:
    """What the guest gets when the owner turns the booking down.

    A guest who was asked to pay and then hears nothing would keep waiting, and
    a guest who did pay needs to know to ask for their money back. The message
    says plainly that the bed is not held and gives them the one thing to do
    next, so a rejection is never silence.
    """
    lines = [
        f"Your booking request {booking['reference']} could not be taken, so the "
        f"bed is not held for you.",
        "",
        "The details you asked for:",
        *_details(db, booking),
        "",
        "If you have already paid, please reply here and the owner will arrange "
        "the refund.",
        "You are welcome to ask about other dates or rooms and we will try again.",
    ]
    return "\n".join(lines)
