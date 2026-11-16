"""Claim-token protected execution for stored inbound messages."""

from __future__ import annotations

import logging

from ai.adapter import AIClient
from core import inbox
from core.channel.adapter import ChannelAdapter
from core.config import Settings
from core.db import Database
from core.dispatcher import dispatch

log = logging.getLogger(__name__)


def process_message(db: Database, channel: ChannelAdapter, settings: Settings,
                    message_id: str, ai: AIClient | None = None) -> bool:
    """Claim and execute one freshly stored message; duplicates cannot dispatch."""
    rows = inbox.claim_inbound(db, message_id=message_id, force=True, limit=1)
    if not rows:
        return False
    row = rows[0]
    token = row["claim_token"]
    try:
        message = inbox.inbound_from_row(db, row)
        handled = dispatch(db, channel, settings, message, ai,
                           str(row["message_id"]), claim_token=token)
        state = db.query_one(
            "SELECT state FROM inbound_processing WHERE message_id = %s",
            (row["message_id"],),
        )
        if state and state["state"] == "processed":
            return True
        if not handled:
            inbox.fail_inbound_attempt(
                db, str(row["message_id"]), token,
                RuntimeError("dispatch did not complete"), retryable=True,
            )
        return handled
    except Exception as exc:
        log.exception("inbound processing failed for stored message")
        inbox.fail_inbound_attempt(db, str(row["message_id"]), token, exc,
                                   retryable=inbox.inbound_error_is_retryable(exc))
        return False
