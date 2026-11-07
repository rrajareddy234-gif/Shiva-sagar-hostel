"""Turn an inbound message into a reply and send it.

This is the one place that talks to both the handler and the channel. It is
deliberately small: decide the reply, send it, record it.

Failure policy: if sending fails, the message stays unprocessed so a later
delivery or a retry can pick it up. If handling fails, the guest still gets a
plain fallback so they are never left without a reply.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ai.adapter import AIClient
from core import alerts, handler, inbox, security
from core.channel.adapter import ChannelAdapter, InboundMessage, Prompt
from core.config import Settings
from core.db import Database
from core.export import DataFile

log = logging.getLogger(__name__)

HISTORY_TURNS = 12


def auto_replies_go_out(db: Database, settings: Settings, from_phone: str) -> bool:
    """Should this sender get a reply right now?

    False only for a guest while the pause is on. The owner and staff are always
    answered: the owner needs to be able to text "resume replies", and a staff
    member needs to keep working. Reading the switch from the database means the
    dashboard button and the WhatsApp command take effect immediately, with no
    restart.
    """
    role = security.role_for(from_phone, settings.owner_phone, set(settings.staff_phones))
    if role in (security.OWNER, security.STAFF):
        return True
    from core import auto_reply
    return not auto_reply.is_paused(db, default_enabled=settings.auto_reply_enabled)


def build_ai(settings: Settings) -> AIClient | None:
    """The configured AI client, or None when no key is set."""
    if not settings.ai_api_key:
        return None
    from ai.registry import build_ai_client
    try:
        return build_ai_client(settings.ai_provider, model=settings.ai_model,
                               api_key=settings.ai_api_key,
                               base_url=settings.ai_base_url,
                               timeout=settings.ai_timeout)
    except Exception as exc:
        log.error("could not build the AI client: %s", exc)
        return None


def history_for(db: Database, phone: str) -> list[dict]:
    """Recent conversation turns, oldest first, for the AI's context."""
    rows = db.query_all(
        """
        SELECT m.direction, m.body
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE c.phone = %s AND m.body IS NOT NULL
        ORDER BY m.message_id DESC
        LIMIT %s
        """,
        (phone, HISTORY_TURNS),
    )
    turns = [{"role": "user" if r["direction"] == "IN" else "assistant",
              "content": r["body"]} for r in reversed(rows)]
    return turns[:-1] if turns else turns


def dispatch(db: Database, channel: ChannelAdapter, settings: Settings,
             message: InboundMessage, ai: AIClient | None = None,
             message_id: str | None = None) -> bool:
    """Handle one inbound message and send the reply.

    Returns True when the message was handled, so the caller can mark it done.

    When automatic replies are paused, only *guests* are left unanswered. The
    owner and staff are still answered, which is what makes the pause
    controllable: the owner texts "resume replies" to switch it back on, and
    that reply has to reach them. Everything else still happens for every
    message -- it is stored, handled, bookings are created and the owner is
    alerted -- so a pause never loses a booking or a message.
    """
    auto = auto_replies_go_out(db, settings, message.from_phone)

    # A payment screenshot is an image with no text. It is not routed like a
    # message: the guest has paid and is waiting, so it is filed, forwarded to
    # the owner, and acknowledged. The bot never reads the image -- it cannot --
    # the owner checks the money.
    if message.media_id and message.message_type in ("image", "document"):
        _handle_payment_proof(db, channel=channel, settings=settings, message=message)
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    # A tap is a text message the guest did not have to type. It carries the
    # button id, so it is handled exactly like typed text.
    if not message.button_id and (message.message_type != "text"
                                  or not (message.text or "").strip()):
        # Media and other message types are acknowledged but not yet understood.
        if auto:
            _reply(db, channel, message.from_phone,
                   "Thanks for your message. Please send it as text so I can help, "
                   "or tell me your check-in date and how many nights you need.")
        else:
            log.info("auto-reply paused; media from %s stored without a reply",
                     message.from_phone)
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    try:
        outcome = handler.handle_message(
            db, phone=message.from_phone, text=inbox.bounded_text(message.text),
            owner_phone=settings.owner_phone, ai=ai,
            history=history_for(db, message.from_phone),
            button_id=message.button_id,
            staff_phones=settings.staff_phones)
    except Exception as exc:
        log.exception("handling failed for %s: %s", message.from_phone, exc)
        if auto:
            _reply(db, channel, message.from_phone,
                   "Sorry, something went wrong on my side. The owner has been alerted "
                   "and will get back to you.")
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    # An outage is silent from the guest's side, so the owner is told about it
    # here, where the outcome and the channel are both in hand. Both calls are
    # best-effort: a failed alert must not stop the guest's reply going out.
    #
    # These go to the owner, not the guest, so a pause on automatic replies does
    # not silence them: a booking request that arrived during a pause must still
    # reach the owner, or it would sit unseen.
    if outcome.escalate:
        alerts.note_failure(db, channel=channel, owner_phone=settings.owner_phone,
                            reason="AI unavailable")
    elif outcome.handled_by == "ai":
        alerts.note_recovery(db, channel=channel, owner_phone=settings.owner_phone)

    # A request the AI just created is put in front of the owner with one-tap
    # Approve/Reject buttons. Best effort: the guest's own reply is unaffected
    # if this cannot be sent.
    for event in outcome.events:
        if event.get("type") == "booking_requested":
            alerts.notify_new_request(
                db, channel=channel, owner_phone=settings.owner_phone,
                reference=event["reference"],
                summary=_request_summary(event))
        elif event.get("type") in ("booking_accepted", "booking_confirmed",
                                   "booking_rejected"):
            _notify_booking_event(db, channel=channel, settings=settings, event=event)

    if not auto:
        log.info("auto-reply paused; stored and handled %s from %s without replying",
                 message.message_type, message.from_phone)
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    sent = _reply(db, channel, message.from_phone, outcome.reply,
                  prompt=outcome.prompt, file=outcome.file)
    if not sent:
        return False

    if message_id:
        inbox.mark_processed(db, message_id)
    return True


def _handle_payment_proof(db: Database, *, channel: ChannelAdapter,
                          settings: Settings, message) -> None:
    """File a guest's payment screenshot and pass it to the owner to check.

    Which booking it belongs to is the guest's most recent booking that is
    waiting on payment. That is the only booking a screenshot can sensibly be
    about, and guessing beyond it -- at an older booking, say -- would be worse
    than asking. When there is no such booking the guest is told so.
    """
    from core import booking, booking_notices

    row = db.query_one(
        """
        SELECT bk.* FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE g.phone = %s AND bk.status = 'ACCEPTED'
        ORDER BY bk.created_at DESC LIMIT 1
        """,
        (message.from_phone,))

    if not row:
        _reply(db, channel, message.from_phone,
               "Thank you for the screenshot. I could not find a booking of yours "
               "waiting for payment. Please send the booking reference, or make "
               "your booking first.")
        return

    content = None
    media_type = message.media_type or "image/jpeg"
    try:
        fetched = channel.fetch_media(message.media_id)
        if fetched:
            content, fetched_type = fetched
            media_type = fetched_type or media_type
    except Exception as exc:
        log.warning("could not download proof for %s: %s", row["reference"], exc)

    path = _store_proof(settings.payment_proof_dir, row["reference"], content, media_type)

    try:
        booking.mark_proof_sent(db, str(row["booking_id"]), proof_path=path)
    except Exception as exc:
        log.warning("could not mark proof sent for %s: %s", row["reference"], exc)
        _reply(db, channel, message.from_phone,
               "Thank you for the screenshot. Something went wrong saving it; "
               "please send it again or contact the owner.")
        return

    updated = booking.get_booking(db, str(row["booking_id"]))
    booking_notices.notify_proof_received(
        db, updated, channel=channel, owner_phone=settings.owner_phone,
        content=content, filename=f"{row['reference']}-payment{_suffix(media_type)}",
        media_type=media_type)

    # The owner is asked to check the money with a one-tap Confirm.
    alerts.notify_new_request(
        db, channel=channel, owner_phone=settings.owner_phone,
        reference=row["reference"], step="proof",
        summary=(f"{row['reference']}: the guest says they have paid and sent a "
                 f"screenshot. Check the payment, then Confirm to finish the "
                 f"booking."))


def _suffix(media_type: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "application/pdf": ".pdf"}.get(media_type, ".jpg")


def _store_proof(directory: str, reference: str, content: bytes | None,
                 media_type: str) -> str | None:
    """Write the screenshot to disk. Returns the path, or None if unavailable.

    A failure to store is not fatal: the booking is still marked as awaiting the
    owner's check, and the image may have reached them through the chat anyway.
    """
    if not content:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{reference}-{stamp}{_suffix(media_type)}"
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path
    except Exception as exc:
        log.warning("could not store payment proof for %s: %s", reference, exc)
        return None


def _notify_booking_event(db: Database, *, channel: ChannelAdapter,
                          settings: Settings, event: dict) -> None:
    """Tell the guest about an owner decision, and copy the owner by email.

    Best effort: the owner's own reply has already been produced, and the
    booking has already changed. A failure to reach WhatsApp or the mail server
    must not undo either.
    """
    from core import booking, booking_notices
    try:
        row = booking.get_booking(db, event["booking_id"])
    except Exception as exc:
        log.warning("could not load booking %s to notify: %s", event.get("booking_id"), exc)
        return
    action = {"booking_accepted": "accepted", "booking_confirmed": "confirmed",
              "booking_rejected": "rejected"}.get(event["type"])
    if action is None:
        return
    try:
        booking_notices.notify_decision(db, row, channel=channel, action=action)
    except Exception as exc:
        log.warning("could not notify about %s: %s", row.get("reference"), exc)


def _request_summary(event: dict) -> str:
    """The owner's approval message for one booking request.

    Every figure comes from the request the engine created; nothing is computed
    here, so the owner sees exactly the amount the pricing engine produced.
    """
    beds = ", ".join(event.get("bed_ids") or [])
    guests = event.get("guests") or 1
    party = f"{guests} guest(s)" if guests != 1 else "1 guest"
    terms = event.get("payment_terms") or "ADVANCE"
    if terms == "FULL":
        # The guest pays the whole amount up front, so there is no separate
        # reservation to show; a "Reservation due: Rs 0" line would only confuse.
        pay_line = f"Pay now (full): Rs {event.get('charges_total')}"
    else:
        pay_line = f"Reservation due: Rs {event.get('advance_due')}"
    lines = [
        f"New booking request {event['reference']}",
        f"{event.get('guest_name') or 'Guest'} - {party}",
        f"Bed(s): {beds}",
        f"{event.get('check_in')} to {event.get('check_out')}",
        f"Total: Rs {event.get('charges_total')}",
        pay_line,
        "",
        "Approve or reject with a tap below.",
    ]
    return "\n".join(lines)


def _reply(db: Database, channel: ChannelAdapter, phone: str, text: str,
           prompt: Prompt | None = None, file: DataFile | None = None) -> bool:
    """Send the answer, then the tappable answers, when the router offered them.

    A prompt is a nicety, never a replacement for the answer. It used to be sent
    *instead of* `text`, which silently dropped the answer: "we have 4 beds free,
    - AC: 1 free, ..." became the bare question "Would you like to book a room?",
    and the whole 798-character rate card became "Which room type would you
    like?". The guest was left wondering what had happened.

    So the body goes first and the prompt follows, as a second message. The one
    exception is a prompt whose body *is* the answer -- the food question, "AC
    room. With food or without?". There the prompt replaces the text, because
    sending both would repeat the same sentence twice.
    """
    if file is not None:
        # The file carries the answer; the text is only a caption. If the file
        # cannot be sent, say so instead of silently sending a caption that
        # promises a file the owner never receives.
        result = channel.send_document(phone, file.filename, file.content,
                                       file.media_type, caption=text)
        if result.ok:
            inbox.record_outbound(db, phone, text, result.provider_message_id)
            return True
        log.warning("document send failed for %s: %s", phone, result.error)
        text = ("I could not send the file just now. Please ask me again in a "
                "moment. Nothing has changed in your records.")

    usable = prompt is not None and prompt.is_valid()
    carries_the_answer = usable and prompt.body.strip() == text.strip()

    if not carries_the_answer:
        result = channel.send_text(phone, text)
        if not result.ok:
            log.error("could not send to %s: %s", phone, result.error)
            return False
        inbox.record_outbound(db, phone, text, result.provider_message_id)

    if not usable:
        return True

    result = channel.send_interactive(phone, prompt)
    if result.ok:
        inbox.record_outbound(db, phone, prompt.body, result.provider_message_id)
        return True
    if not result.retryable:
        # Sending anything else now would be another message to the same guest
        # while Meta is throttling this pair, which is counted against the same
        # limit and deepens the block.
        log.error("could not send the buttons to %s, and a retry would worsen "
                  "the throttle: %s", phone, result.error)
        return True
    log.warning("interactive send failed for %s: %s", phone, result.error)
    if carries_the_answer:
        # The prompt held the only copy of the answer, so the guest has heard
        # nothing yet. Fall back to plain text; if that fails the message stays
        # unprocessed and can be retried.
        result = channel.send_text(phone, text)
        if not result.ok:
            log.error("could not send to %s: %s", phone, result.error)
            return False
        inbox.record_outbound(db, phone, text, result.provider_message_id)
    return True
