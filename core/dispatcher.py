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
import uuid
from datetime import UTC, datetime

from ai.adapter import AIClient
from core import alerts, handler, inbox, outbox, security
from core.channel.adapter import (
    ChannelAdapter,
    Flow,
    InboundMessage,
    Prompt,
)
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

    A guest also needs the deployment master switch (``SEND_ENABLED``). This is
    the dispatcher's early check, so a disabled deployment does not even build a
    guest reply; the delivery layer (``core.outbound_worker``) enforces the same
    gate again, which is what actually stops the send.
    """
    role = security.role_for(from_phone, settings.owner_phone, set(settings.staff_phones))
    if role in (security.OWNER, security.STAFF):
        return True
    from core import auto_reply, send_policy
    if not send_policy.env_allows(settings):
        return False
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
             message_id: str | None = None,
             claim_token: str | None = None) -> bool:
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

    # A submitted calendar Flow arrives as an interactive whose answers are a
    # JSON object, not free text. It is folded into the booking and the next
    # question is asked, so the Flow replaces only the date question. A Flow the
    # guest cancelled carries no usable range; the dates are then asked for
    # again, rather than answered with the generic "send it as text" reply.
    if message.message_type == "interactive" and not message.button_id:
        from core.channel.adapter import flow_date_range, flow_response_data
        interactive = message.raw.get("interactive") or {}
        if "nfm_reply" in interactive:
            check_in, check_out = flow_date_range(flow_response_data(message.raw))
            return _handle_flow_submission(
                db, channel=channel, settings=settings, message=message,
                check_in=check_in or "", check_out=check_out or "",
                message_id=message_id, claim_token=claim_token, auto=auto)

    # A payment screenshot is an image with no text. It is not routed like a
    # message: the guest has paid and is waiting, so it is filed, forwarded to
    # the owner, and acknowledged. The bot never reads the image -- it cannot --
    # the owner checks the money.
    if message.media_id and message.message_type in ("image", "document"):
        if message_id and not inbox.mark_dispatch_started(db, message_id, claim_token):
            return False
        _handle_payment_proof(db, channel=channel, settings=settings, message=message,
                              message_id=message_id, claim_token=claim_token)
        if message_id:
            inbox.mark_processed(db, message_id, claim_token)
        return True

    # A tap is a text message the guest did not have to type. It carries the
    # button id, so it is handled exactly like typed text.
    if not message.button_id and (message.message_type != "text"
                                  or not (message.text or "").strip()):
        if message_id and not inbox.mark_dispatch_started(db, message_id, claim_token):
            return False
        # Media and other message types are acknowledged but not yet understood.
        if auto:
            _reply(db, channel, message.from_phone,
                   "Thanks for your message. Please send it as text so I can help, "
                   "or tell me your check-in date and how many nights you need.",
                   source_inbound_id=message_id, claim_token=claim_token,
                   private_root=settings.payment_proof_dir, settings=settings)
        else:
            log.info("auto-reply paused; media from %s stored without a reply",
                     message.from_phone)
        if message_id and not auto:
            inbox.mark_processed(db, message_id, claim_token)
        return True

    if message_id and not inbox.mark_dispatch_started(db, message_id, claim_token):
        return False

    try:
        outcome = handler.handle_message(
            db, phone=message.from_phone, text=inbox.bounded_text(message.text),
            owner_phone=settings.owner_phone, ai=ai,
            history=history_for(db, message.from_phone),
            button_id=message.button_id,
            staff_phones=settings.staff_phones,
            flow_id=settings.whatsapp_flow_id,
            flow_screen=settings.whatsapp_flow_calendar_screen)
    except Exception as exc:
        log.exception("handling failed for %s: %s", message.from_phone, exc)
        if auto:
            _reply(db, channel, message.from_phone,
                   "Sorry, something went wrong on my side. The owner has been alerted "
                   "and will get back to you.", source_inbound_id=message_id,
                   claim_token=claim_token, private_root=settings.payment_proof_dir,
                   settings=settings)
        elif message_id:
            inbox.mark_processed(db, message_id, claim_token)
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

    # The owner's alerts and the guest's reply are queued in the outbox; send
    # them now so both reach their phone promptly. This runs even while guest
    # replies are paused, because a booking request must still reach the owner.
    _drain(db, channel, settings=settings, private_root=settings.payment_proof_dir)

    if not auto:
        log.info("auto-reply paused; stored and handled %s from %s without replying",
                 message.message_type, message.from_phone)
        if message_id:
            inbox.mark_processed(db, message_id, claim_token)
        return True

    sent = _reply(db, channel, message.from_phone, outcome.reply,
                  prompt=outcome.prompt, file=outcome.file, flow=outcome.flow,
                  source_inbound_id=message_id, claim_token=claim_token,
                  private_root=settings.payment_proof_dir, settings=settings)
    return sent


def _handle_payment_proof(db: Database, *, channel: ChannelAdapter,
                          settings: Settings, message,
                          message_id: str | None = None,
                          claim_token: str | None = None) -> None:
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
               "your booking first.", source_inbound_id=message_id,
               claim_token=claim_token, private_root=settings.payment_proof_dir,
               settings=settings)
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
    proof_ref = None
    proof_filename = f"{row['reference']}-payment{_suffix(media_type)}"
    if content and settings.owner_phone:
        try:
            from core.outbound_worker import stage_attachment
            proof_ref = stage_attachment(settings.payment_proof_dir, content=content,
                                         filename=proof_filename)
        except Exception as exc:
            log.warning("could not stage private outbound proof for %s: %s",
                        row["reference"], type(exc).__name__)

    try:
        booking.mark_proof_sent(db, str(row["booking_id"]), proof_path=path,
                                owner_phone=settings.owner_phone,
                                outbound_file_ref=proof_ref, filename=proof_filename,
                                media_type=media_type)
    except Exception as exc:
        if proof_ref:
            from core.outbound_worker import discard_staged_attachment
            discard_staged_attachment(settings.payment_proof_dir, proof_ref)
        log.warning("could not mark proof sent for %s: %s", row["reference"], exc)
        _reply(db, channel, message.from_phone,
               "Thank you for the screenshot. Something went wrong saving it; "
               "please send it again or contact the owner.",
               source_inbound_id=message_id, claim_token=claim_token,
               private_root=settings.payment_proof_dir, settings=settings)
        return

    updated = booking.get_booking(db, str(row["booking_id"]))
    booking_notices.notify_proof_received(
        db, updated, channel=channel, owner_phone=settings.owner_phone,
        content=content, filename=f"{row['reference']}-payment{_suffix(media_type)}",
        media_type=media_type, attachment_path=path,
        outbound_proof_queued=bool(proof_ref and settings.owner_phone))

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


def _handle_flow_submission(db: Database, *, channel: ChannelAdapter,
                            settings: Settings, message,
                            check_in: str, check_out: str,
                            message_id: str | None = None,
                            claim_token: str | None = None,
                            auto: bool = True) -> bool:
    """Fold a submitted calendar Flow into the booking and answer with the next
    question. One Flow submission is one inbound message, so it gets one reply."""
    from core import booking_flow

    if message_id and not inbox.mark_dispatch_started(db, message_id, claim_token):
        return False

    try:
        result = booking_flow.apply_flow_dates(
            db, phone=message.from_phone, check_in=check_in, check_out=check_out,
            flow_id=settings.whatsapp_flow_id,
            flow_screen=settings.whatsapp_flow_calendar_screen)
    except Exception as exc:
        log.exception("flow submission failed for %s: %s", message.from_phone, exc)
        if auto:
            _reply(db, channel, message.from_phone,
                   "Sorry, something went wrong on my side. The owner has been "
                   "alerted and will get back to you.",
                   source_inbound_id=message_id, claim_token=claim_token,
                   private_root=settings.payment_proof_dir, settings=settings)
        elif message_id:
            inbox.mark_processed(db, message_id, claim_token)
        return True

    _drain(db, channel, settings=settings, private_root=settings.payment_proof_dir)

    if not auto:
        log.info("auto-reply paused; stored and handled flow submission from %s",
                 message.from_phone)
        if message_id:
            inbox.mark_processed(db, message_id, claim_token)
        return True

    return _reply(db, channel, message.from_phone, result.reply,
                  prompt=result.prompt, flow=result.flow,
                  source_inbound_id=message_id, claim_token=claim_token,
                  private_root=settings.payment_proof_dir, settings=settings)


def _store_proof(directory: str, reference: str, content: bytes | None,
                 media_type: str) -> str | None:
    """Write the screenshot to disk. Returns the path, or None if unavailable.

    A failure to store is not fatal: the booking is still marked as awaiting the
    owner's check, and the image may have reached them through the chat anyway.
    """
    if not content:
        return None
    try:
        os.makedirs(directory, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{reference}-{stamp}{_suffix(media_type)}"
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
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
           prompt: Prompt | None = None, file: DataFile | None = None,
           flow: Flow | None = None,
           source_inbound_id: str | None = None,
           claim_token: str | None = None,
           private_root: str = "payment_proofs",
           settings: Settings | None = None) -> bool:
    """Queue and send exactly one guest-facing response for this inbound message.

    The conversation is interactive-only: one inbound message produces one
    logical reply. When the router offers choices, the answer and the choices
    travel in ONE WhatsApp interactive message -- never a text answer followed
    by a separate prompt. The reply is written to the durable outbox (which also
    completes the inbound message, so a Meta retry cannot reply twice) and then
    sent once; if the send fails, the outbox keeps the payload and the worker
    retries it under the normal bounded policy.

    Meta caps an interactive body at 1024 characters (`MAX_INTERACTIVE_BODY`).
    When the answer plus the question would exceed that, the answer is shortened
    at a line boundary so the guest still gets everything in one interactive
    message. A text fallback is never sent for a failed interactive: the outbox
    retries the interactive payload, and a second message would violate the
    interactive-only contract and worsen Meta's pair throttling.

    A guest reply with no choices of its own -- an informational answer, an
    acknowledgement, an error -- is given a terminal set of actions
    (`router.terminal_prompt`) so the conversation stays interactive-only: one
    inbound message still produces exactly one interactive message. The owner
    and staff are answered in plain text; the rule is about guest conversations.

    A Flow (a native form such as the calendar range picker) is its own single
    message; the guest's answers arrive back on the webhook, not as text. A file
    is its own single document message.
    """
    if (prompt is None and flow is None and file is None
            and (text or "").strip() and _is_guest(phone, settings)):
        from core import router
        prompt = router.terminal_prompt()
    key = (f"inbound:{source_inbound_id}:guest" if source_inbound_id
           else f"reply:{uuid.uuid4()}:guest")
    file_ref = None
    if file is not None:
        file_ref = outbox.attach_file(private_root, file.content, file.filename)
        messages = [{
            "recipient": phone,
            "message_type": "document",
            "payload": {
                "file_ref": file_ref, "filename": file.filename,
                "media_type": file.media_type, "caption": text,
            },
            "idempotency_key": f"{key}:document",
        }]
        try:
            outbox.enqueue_many(db, messages, source_inbound_id=source_inbound_id,
                                claim_token=claim_token)
        except Exception:
            from core.outbound_worker import discard_staged_attachment
            discard_staged_attachment(private_root, file_ref)
            raise
    elif flow is not None and flow.is_valid():
        messages = [{
            "recipient": phone,
            "message_type": "flow",
            "payload": outbox.flow_payload(flow),
            "idempotency_key": f"{key}:flow",
        }]
        outbox.enqueue_many(db, messages, source_inbound_id=source_inbound_id,
                            claim_token=claim_token)
    elif prompt is not None and prompt.is_valid():
        # Interactive-only: the answer and the choices travel in ONE WhatsApp
        # interactive message. The prompt's own question is skipped when it
        # repeats the answer, so the food question is not said twice.
        answer = (text or "").strip()
        question = (prompt.body or "").strip()
        body = _fit_interactive(answer, question)
        combined = Prompt(body=body, buttons=prompt.buttons,
                          rows=prompt.rows, button_text=prompt.button_text)
        if not combined.is_valid():
            raise ValueError("interactive response has no valid choices")
        messages = [{
            "recipient": phone,
            "message_type": "interactive",
            "payload": {"prompt": outbox.prompt_payload(combined)},
            "idempotency_key": f"{key}:interactive",
        }]
        outbox.enqueue_many(db, messages, source_inbound_id=source_inbound_id,
                            claim_token=claim_token)
    else:
        messages = [{
            "recipient": phone,
            "message_type": "text",
            "payload": {"text": text},
            "idempotency_key": f"{key}:text",
        }]
        outbox.enqueue_many(db, messages, source_inbound_id=source_inbound_id,
                            claim_token=claim_token)

    _drain(db, channel, settings=settings, private_root=private_root)
    # The reply is durably queued (and the inbound completed) before this
    # returns; the drain only tries to deliver it promptly.
    return True


def _is_guest(phone: str, settings: Settings | None) -> bool:
    """True when this number is a guest, so the interactive-only rule applies.

    The owner and staff are answered in plain text: the rule is about guest
    conversations. With no settings (a direct `_reply` caller) the sender is
    treated as a guest.
    """
    if settings is None or not settings.owner_phone:
        return True
    role = security.role_for(phone, settings.owner_phone, set(settings.staff_phones))
    return role == security.CUSTOMER


def _drain(db: Database, channel: ChannelAdapter, *, settings: Settings | None,
           private_root: str) -> dict:
    """Send the pending outbox now so a reply reaches the guest promptly.

    The outbox stays the durable record: a failure here leaves the row pending
    for the scheduled worker to retry, so the reply is never lost.
    """
    from core.outbound_worker import run_batch
    worker_settings = settings or _fallback_worker_settings(channel, private_root)
    return run_batch(db, channel, worker_settings)


def _fallback_worker_settings(channel: ChannelAdapter, private_root: str):
    """Worker settings for direct `_reply` callers that have no app settings.

    This shim is reached only when a caller invokes `_reply`/`_drain` with no
    `Settings` at all -- a low-level or test caller, never the running app, whose
    dispatcher always passes real settings. The send gate is still read from the
    environment here, so even a direct caller cannot message a guest unless
    `SEND_ENABLED` is explicitly true; the defaults stay closed.
    """
    from types import SimpleNamespace

    from core.config import _env_bool
    return SimpleNamespace(
        meta_phone_number_id=channel.name, owner_phone=None, staff_phones=(),
        payment_proof_dir=private_root,
        send_enabled=_env_bool("SEND_ENABLED", False),
        owner_notify_enabled=_env_bool("OWNER_NOTIFY_ENABLED", True),
        auto_reply_enabled=_env_bool("AUTO_REPLY_ENABLED", True),
    )


def _fit_interactive(answer: str, question: str) -> str:
    """Compose the one interactive body for a router reply."""
    from core.channel.adapter import compose_interactive_body
    return compose_interactive_body(answer, question)
