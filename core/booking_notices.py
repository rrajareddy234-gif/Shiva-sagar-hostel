"""Send the guest and the owner what each booking step owes them.

The chat (dispatcher) and the dashboard both change a booking's status, and both
must then tell the guest and email the owner. Putting that in one place means the
two paths cannot drift: a booking accepted from a tap and one accepted from a
typed command produce the same two messages.

Every function here is best effort and never raises. A booking that was accepted
must stay accepted even if WhatsApp or the mail server is briefly unreachable --
losing the notification is bad, but losing the booking is worse.
"""

from __future__ import annotations

import logging

from core import guest_notices, owner_email
from core.channel.adapter import ChannelAdapter
from core.db import Database

log = logging.getLogger(__name__)


def _guest_phone(db: Database, booking: dict) -> str | None:
    row = db.query_one("SELECT phone FROM guests WHERE guest_id = %s",
                       (booking["guest_id"],))
    return row["phone"] if row else None


def _guest(db: Database, booking: dict) -> dict:
    """The guest's name and phone, for the owner's copy.

    The owner has to check the money against a person. A booking reference alone
    does not say who paid, so every email carries the name and number.
    """
    row = db.query_one(
        "SELECT full_name, phone FROM guests WHERE guest_id = %s",
        (booking["guest_id"],))
    return {"name": (row or {}).get("full_name"),
            "phone": (row or {}).get("phone")}


def _tell_guest(db: Database, channel: ChannelAdapter | None, booking: dict,
                text: str) -> bool:
    phone = _guest_phone(db, booking)
    if not phone or channel is None:
        return False
    try:
        result = channel.send_text(phone, text)
        if not result.ok:
            log.warning("could not tell guest %s about %s: %s",
                        phone, booking["reference"], result.error)
            return False
        return True
    except Exception as exc:
        log.warning("could not tell guest %s about %s: %s",
                    phone, booking["reference"], exc)
        return False


def notify_accepted(db: Database, booking: dict, *,
                    channel: ChannelAdapter | None) -> bool:
    """The owner's first approval: tell the guest to pay, and copy the owner."""
    text = guest_notices.acceptance_message(db, booking)
    sent = _tell_guest(db, channel, booking, text)
    owner_email.booking_email("accepted", booking, text, guest=_guest(db, booking))
    return sent


def notify_confirmed(db: Database, booking: dict, *,
                     channel: ChannelAdapter | None) -> bool:
    """The owner's second approval: the final confirmation, and the copy."""
    text = guest_notices.final_confirmation_message(db, booking)
    sent = _tell_guest(db, channel, booking, text)
    owner_email.booking_email("confirmed", booking, text, guest=_guest(db, booking))
    return sent


def notify_rejected(db: Database, booking: dict, *,
                    channel: ChannelAdapter | None) -> bool:
    """The owner turned the booking down: tell the guest, and copy the owner."""
    text = guest_notices.rejection_message(db, booking)
    sent = _tell_guest(db, channel, booking, text)
    owner_email.booking_email("rejected", booking, text, guest=_guest(db, booking))
    return sent


def notify_proof_received(db: Database, booking: dict, *, channel: ChannelAdapter | None,
                          owner_phone: str | None, content: bytes | None = None,
                          filename: str | None = None,
                          media_type: str = "image/jpeg") -> bool:
    """Forward the guest's payment screenshot to the owner, and email a copy.

    The owner is the one who decides whether the payment is real. This hands
    them the image and asks for their second approval; it does not judge the
    payment itself.
    """
    forwarded = False
    if owner_phone and channel is not None and content:
        try:
            result = channel.send_document(
                owner_phone, filename or "payment-proof.jpg", content,
                media_type=media_type,
                caption=(f"Payment screenshot for {booking['reference']} from the "
                         f"guest. Check it, then approve to confirm the booking."))
            forwarded = bool(result.ok)
        except Exception as exc:
            log.warning("could not forward proof for %s: %s",
                        booking["reference"], exc)

    _tell_guest(db, channel, booking, guest_notices.proof_received_message(booking))
    proof_name = filename or f"{booking['reference']}-payment.jpg"
    owner_email.booking_email(
        "proof", booking, guest_notices.booking_details(db, booking),
        guest=_guest(db, booking),
        attachment=(proof_name, content, media_type) if content else None)
    return forwarded


def notify_decision(db: Database, booking: dict, *, channel: ChannelAdapter | None,
                    action: str) -> bool:
    """Tell the guest about one owner decision, from whichever path made it.

    The chat and the dashboard both change a booking's status, and both owe the
    guest the same message. Routing both through this one function is what stops
    a decision taken on the page from leaving the guest uninformed -- the drift
    the module exists to prevent. An unknown action is logged and ignored rather
    than raising, because the booking has already changed and a missing notice
    must not undo it.
    """
    notifiers = {
        "accepted": notify_accepted,
        "confirmed": notify_confirmed,
        "rejected": notify_rejected,
    }
    notify = notifiers.get(action)
    if notify is None:
        log.warning("no guest notice for decision %r on %s",
                    action, booking.get("reference"))
        return False
    return notify(db, booking, channel=channel)
