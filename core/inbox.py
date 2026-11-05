"""Enquiry tracking, duplicate-message protection and the message inbox.

Every inbound WhatsApp message is stored once, keyed by the provider's message
id. A repeated delivery is recognised and ignored, so a retry from Meta never
produces a second reply or a second booking.

Enquiries are tracked so the owner can see who was never answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.channel.adapter import InboundMessage
from core.db import Database

log = logging.getLogger(__name__)

UNANSWERED_AFTER = timedelta(minutes=30)

# A real WhatsApp text message is at most 4096 characters. Anything far larger
# is not a guest typing; it is a malformed or hostile payload. Storing it would
# let one request bloat the database and the AI prompt, so it is trimmed at the
# edge, once, before it reaches any handler.
MAX_TEXT_LENGTH = 4096


def bounded_text(text: str | None) -> str | None:
    """Clip an inbound body to a sane length. None and short text pass through."""
    if text is None:
        return None
    return text[:MAX_TEXT_LENGTH] if len(text) > MAX_TEXT_LENGTH else text


@dataclass(frozen=True)
class StoredMessage:
    message_id: str
    conversation_id: str
    is_new: bool


def store_inbound(db: Database, message: InboundMessage) -> StoredMessage | None:
    """Persist an inbound message exactly once.

    Returns None if this provider message id was already stored, which is the
    signal to skip all further processing for this delivery.
    """
    if not message.provider_message_id:
        log.warning("inbound message without a provider id; dropping")
        return None

    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO conversations (phone) VALUES (%s) "
            "ON CONFLICT (phone) DO UPDATE SET last_activity = now() "
            "RETURNING conversation_id",
            (message.from_phone,),
        )
        conversation_id = str(cur.fetchone()["conversation_id"])

        # The WhatsApp display name arrives with every message. Record it as the
        # guest's name only if we have nothing better: it must never overwrite a
        # name the guest typed or the owner entered. Stored on the guest row, so
        # a returning guest is recognised and never asked again.
        profile_name = (message.profile_name or "").strip()
        if profile_name and len(profile_name) <= 120:
            cur.execute(
                """
                INSERT INTO guests (full_name, phone, name_source, wa_profile_name)
                VALUES (%s, %s, 'WHATSAPP_PROFILE', %s)
                ON CONFLICT (phone) DO UPDATE
                SET wa_profile_name = EXCLUDED.wa_profile_name,
                    full_name = CASE
                        WHEN guests.full_name IS NULL
                          OR btrim(guests.full_name) = ''
                          OR guests.name_source IN ('UNKNOWN', 'WHATSAPP_PROFILE')
                        THEN EXCLUDED.full_name
                        ELSE guests.full_name
                    END,
                    name_source = CASE
                        WHEN guests.name_source IN ('UNKNOWN', 'WHATSAPP_PROFILE')
                        THEN 'WHATSAPP_PROFILE'
                        ELSE guests.name_source
                    END
                """,
                (profile_name, message.from_phone, profile_name),
            )

        cur.execute(
            """
            INSERT INTO messages (wa_message_id, conversation_id, direction, body, payload)
            VALUES (%s, %s, 'IN', %s, %s::jsonb)
            ON CONFLICT (wa_message_id) DO NOTHING
            RETURNING message_id
            """,
            (message.provider_message_id, conversation_id, bounded_text(message.text),
             _json(message.raw)),
        )
        row = cur.fetchone()
        if row is None:
            return None  # duplicate delivery

        cur.execute(
            """
            INSERT INTO enquiries (guest_id, phone, status, last_message_at)
            SELECT g.guest_id, %(phone)s, 'OPEN', now()
            FROM (SELECT 1) x
            LEFT JOIN guests g ON g.phone = %(phone)s
            WHERE NOT EXISTS (
                SELECT 1 FROM enquiries e
                WHERE e.phone = %(phone)s AND e.status IN ('OPEN', 'RESPONDED', 'AWAITING_OWNER')
            )
            RETURNING enquiry_id
            """,
            {"phone": message.from_phone},
        )
        cur.execute(
            "UPDATE enquiries SET last_message_at = now() "
            "WHERE phone = %s AND status IN ('OPEN','RESPONDED','AWAITING_OWNER')",
            (message.from_phone,),
        )
        return StoredMessage(str(row["message_id"]), conversation_id, True)


def mark_processed(db: Database, message_id: str) -> None:
    db.execute("UPDATE messages SET processed_at = now() WHERE message_id = %s", (message_id,))


def record_outbound(db: Database, phone: str, body: str,
                    provider_message_id: str | None = None) -> None:
    """Record a reply we sent, and mark the guest's enquiry as responded."""
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO conversations (phone) VALUES (%s) "
            "ON CONFLICT (phone) DO UPDATE SET last_activity = now() "
            "RETURNING conversation_id",
            (phone,),
        )
        conversation_id = str(cur.fetchone()["conversation_id"])
        cur.execute(
            "INSERT INTO messages (wa_message_id, conversation_id, direction, body, processed_at) "
            "VALUES (%s, %s, 'OUT', %s, now())",
            (provider_message_id, conversation_id, body),
        )
        cur.execute(
            "UPDATE enquiries SET responded = true, responded_at = now(), status = 'RESPONDED' "
            "WHERE phone = %s AND status = 'OPEN'",
            (phone,),
        )


def unanswered_enquiries(db: Database, now: datetime | None = None,
                         threshold: timedelta = UNANSWERED_AFTER) -> list[dict]:
    """Enquiries with no reply sent within the threshold."""
    return db.query_all(
        """
        SELECT enquiry_id, phone, first_message_at, last_message_at, status
        FROM enquiries
        WHERE status = 'OPEN'
          AND responded = false
          AND last_message_at < COALESCE(%s, now()) - %s
        ORDER BY last_message_at
        """,
        (now, threshold),
    )


def open_enquiries(db: Database) -> list[dict]:
    return db.query_all(
        "SELECT enquiry_id, phone, first_message_at, last_message_at, responded, status "
        "FROM enquiries WHERE status IN ('OPEN','RESPONDED','AWAITING_OWNER') "
        "ORDER BY last_message_at DESC"
    )


def enquiries_between(db: Database, start: datetime, end: datetime) -> list[dict]:
    return db.query_all(
        "SELECT * FROM enquiries WHERE first_message_at >= %s AND first_message_at < %s "
        "ORDER BY first_message_at",
        (start, end),
    )


def get_conversation_state(db: Database, phone: str) -> dict:
    row = db.query_one("SELECT state FROM conversations WHERE phone = %s", (phone,))
    return (row or {}).get("state") or {}


def set_conversation_state(db: Database, phone: str, state: dict) -> None:
    db.execute(
        "INSERT INTO conversations (phone, state) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (phone) DO UPDATE SET state = %s::jsonb, last_activity = now()",
        (phone, _json(state), _json(state)),
    )


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)
