"""Tell the owner when the AI has stopped answering.

An AI outage is silent by design: the guest still gets a polite holding reply,
so nothing looks wrong from the outside. The owner only finds out when a guest
complains. This module closes that gap by sending the owner one WhatsApp
message when replies start failing, and another when they work again.

Two properties matter more than speed:

* The owner is told **once** per outage, not once per failed message. A model
  that is down stays down for hours, and a message for every guest would be
  worse than the outage.
* A failure to alert must never break the guest's reply. Every call here is
  best-effort and swallows its own errors.

State lives in the `settings` table under one key, so it survives a restart.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.channel.adapter import ChannelAdapter
from core.db import Database

log = logging.getLogger(__name__)

STATE_KEY = "ai_alert"
DOWN = "down"
UP = "up"


def state(db: Database) -> str | None:
    """The last AI health we recorded: DOWN, UP, or None if we never saw it."""
    row = db.query_one("SELECT value FROM settings WHERE key = %s", (STATE_KEY,))
    if not row:
        return None
    value = row["value"]
    if isinstance(value, dict):
        return value.get("state")
    return value


def _set_state(db: Database, new_state: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (STATE_KEY, json.dumps({"state": new_state,
                                "changed_at": datetime.now(timezone.utc).isoformat()})),
    )


def _send(db: Database, channel: ChannelAdapter, phone: str, text: str) -> None:
    """Send one owner message, best effort. Never raises."""
    from core import inbox
    try:
        result = channel.send_text(phone, text)
        if not result.ok:
            log.error("could not alert the owner: %s", result.error)
            return
        inbox.record_outbound(db, phone, text, result.provider_message_id)
    except Exception as exc:  # an alert must never break the guest's reply
        log.error("could not alert the owner: %s", exc)


def note_failure(db: Database, *, channel: ChannelAdapter, owner_phone: str | None,
                 reason: str = "") -> None:
    """Record that the AI failed, alerting the owner the first time it does."""
    try:
        if state(db) == DOWN:
            return
        _set_state(db, DOWN)
    except Exception as exc:
        log.error("could not record the AI outage: %s", exc)
        return

    if not owner_phone:
        log.warning("the AI is down but OWNER_PHONE is not set, so nobody is told")
        return

    detail = f" ({reason})" if reason else ""
    _send(db, channel, owner_phone,
          f"Heads up: the AI assistant stopped answering{detail}. Guests are "
          f"getting a holding reply instead of real answers, so some enquiries "
          f"may be sitting unanswered. Check the AI key and quota. I will tell "
          f"you when it recovers.")


def note_recovery(db: Database, *, channel: ChannelAdapter,
                  owner_phone: str | None) -> None:
    """Record that the AI answered again, telling the owner once."""
    try:
        if state(db) != DOWN:
            return
        _set_state(db, UP)
    except Exception as exc:
        log.error("could not record the AI recovery: %s", exc)
        return

    if not owner_phone:
        return

    _send(db, channel, owner_phone,
          "Good news: the AI assistant is answering again. Guests are getting "
          "real replies.")
