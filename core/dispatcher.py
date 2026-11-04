"""Turn an inbound message into a reply and send it.

This is the one place that talks to both the handler and the channel. It is
deliberately small: decide the reply, send it, record it.

Failure policy: if sending fails, the message stays unprocessed so a later
delivery or a retry can pick it up. If handling fails, the guest still gets a
plain fallback so they are never left without a reply.
"""

from __future__ import annotations

import logging

from ai.adapter import AIClient
from core import alerts, handler, inbox
from core.channel.adapter import ChannelAdapter, InboundMessage, Prompt
from core.export import DataFile
from core.config import Settings
from core.db import Database

log = logging.getLogger(__name__)

HISTORY_TURNS = 12


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
    """
    # A tap is a text message the guest did not have to type. It carries the
    # button id, so it is handled exactly like typed text.
    if not message.button_id and (message.message_type != "text"
                                  or not (message.text or "").strip()):
        # Media and other message types are acknowledged but not yet understood.
        _reply(db, channel, message.from_phone,
               "Thanks for your message. Please send it as text so I can help, "
               "or tell me your check-in date and how many nights you need.")
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    try:
        outcome = handler.handle_message(
            db, phone=message.from_phone, text=message.text,
            owner_phone=settings.owner_phone, ai=ai,
            history=history_for(db, message.from_phone),
            button_id=message.button_id,
            staff_phones=settings.staff_phones)
    except Exception as exc:
        log.exception("handling failed for %s: %s", message.from_phone, exc)
        _reply(db, channel, message.from_phone,
               "Sorry, something went wrong on my side. The owner has been alerted "
               "and will get back to you.")
        if message_id:
            inbox.mark_processed(db, message_id)
        return True

    # An outage is silent from the guest's side, so the owner is told about it
    # here, where the outcome and the channel are both in hand. Both calls are
    # best-effort: a failed alert must not stop the guest's reply going out.
    if outcome.escalate:
        alerts.note_failure(db, channel=channel, owner_phone=settings.owner_phone,
                            reason="AI unavailable")
    elif outcome.handled_by == "ai":
        alerts.note_recovery(db, channel=channel, owner_phone=settings.owner_phone)

    sent = _reply(db, channel, message.from_phone, outcome.reply,
                  prompt=outcome.prompt, file=outcome.file)
    if not sent:
        return False

    if message_id:
        inbox.mark_processed(db, message_id)
    return True


def _reply(db: Database, channel: ChannelAdapter, phone: str, text: str,
           prompt: Prompt | None = None, file: DataFile | None = None) -> bool:
    """Send the reply, preferring tappable answers when the router offered them.

    A prompt is a nicety, never a requirement: if it cannot be sent, the guest
    still gets the plain text. An invalid prompt is not attempted at all.
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

    if prompt is not None and prompt.is_valid():
        result = channel.send_interactive(phone, prompt)
        if result.ok:
            inbox.record_outbound(db, phone, prompt.body, result.provider_message_id)
            return True
        log.warning("interactive send failed for %s, falling back to text: %s",
                    phone, result.error)

    result = channel.send_text(phone, text)
    if not result.ok:
        log.error("could not send to %s: %s", phone, result.error)
        return False
    inbox.record_outbound(db, phone, text, result.provider_message_id)
    return True
