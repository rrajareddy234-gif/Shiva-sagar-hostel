"""Enquiry tracking, duplicate-message protection and the message inbox.

Every inbound WhatsApp message is stored once, keyed by the provider's message
id. A repeated delivery is recognised and ignored, so a retry from Meta never
produces a second reply or a second booking.

Enquiries are tracked so the owner can see who was never answered.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.channel.adapter import DeliveryStatus, InboundMessage
from core.db import Database

log = logging.getLogger(__name__)

UNANSWERED_AFTER = timedelta(minutes=30)
INBOUND_MAX_ATTEMPTS = 6
INBOUND_BACKOFF_BASE_SECONDS = 30
INBOUND_BACKOFF_MAX_SECONDS = 30 * 60
INBOUND_BACKOFF_JITTER = 0.20
INBOUND_CLAIM_LEASE = timedelta(minutes=15)

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
            INSERT INTO messages (wa_message_id, conversation_id, direction, body, payload,
                                  dispatch_claimed_at)
            VALUES (%s, %s, 'IN', %s, %s::jsonb, now())
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
            INSERT INTO inbound_processing (message_id, state, attempt_count, next_retry_at)
            VALUES (%s, 'pending', 0, now() + interval '15 minutes')
            ON CONFLICT (message_id) DO NOTHING
            """, (row["message_id"],),
        )

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

def store_delivery_status(db: Database, status: DeliveryStatus, *,
                          owner_phone: str | None = None) -> None:
    """Persist one WhatsApp delivery-status event."""
    if not status.provider_message_id:
        log.warning("delivery status without provider message id; dropping")
        return

    allowed = {"sent", "delivered", "read", "failed"}
    if status.status not in allowed:
        log.warning("unknown delivery status %r; dropping", status.status)
        return

    with db.transaction() as cur:
        from core import outbox
        event_key = outbox.delivery_event_key(status.provider_message_id, status.status,
                                             status.timestamp, status.recipient_id)
        cur.execute(
            """INSERT INTO outbound_delivery_events
               (event_key, provider_message_id, status, status_timestamp, recipient_id)
               VALUES (%s,%s,%s,CASE WHEN %s::bigint IS NULL THEN NULL
                                     ELSE to_timestamp(%s::double precision) END,%s)
               ON CONFLICT (event_key) DO NOTHING""",
            (event_key, status.provider_message_id, status.status,
             status.timestamp, status.timestamp, status.recipient_id),
        )
        inserted = cur.rowcount == 1
        if inserted:
            cur.execute(
                """
                INSERT INTO message_status_events
                    (wa_message_id, status, status_timestamp, recipient_id, errors)
                VALUES (
                    %s, %s,
                    CASE WHEN %s::bigint IS NULL THEN NULL
                         ELSE to_timestamp(%s::double precision) END,
                    %s, %s::jsonb
                )
                """,
                (status.provider_message_id, status.status, status.timestamp,
                 status.timestamp, status.recipient_id, _json(status.errors)),
            )
            cur.execute("SELECT outbound_id FROM outbound_messages WHERE provider_message_id=%s",
                        (status.provider_message_id,))
            outbound = cur.fetchone()
            if outbound:
                outbox._apply_status(cur, str(outbound["outbound_id"]), status.status)
                if status.status == "failed":
                    cur.execute("SELECT * FROM outbound_messages WHERE outbound_id=%s",
                                (outbound["outbound_id"],))
                    failed = cur.fetchone()
                    errors = (status.errors or {}).get("items", [])
                    code = None
                    if errors and isinstance(errors[0], dict):
                        detail = errors[0].get("error") or errors[0]
                        if isinstance(detail, dict) and detail.get("code") is not None:
                            code = str(detail["code"])
                    outbox._enqueue_terminal_alert(cur, owner_phone, failed, "failed", code)


def mark_processed(db: Database, message_id: str,
                   claim_token: str | None = None) -> bool:
    """Complete processing; production workers must present their current token."""
    with db.transaction() as cur:
        if claim_token is not None:
            cur.execute(
                """UPDATE inbound_processing
                   SET state = 'processed', processed_at = now(), next_retry_at = NULL,
                       claim_token = NULL, claimed_until = NULL, updated_at = now()
                   WHERE message_id = %s AND state = 'processing' AND claim_token = %s""",
                (message_id, claim_token),
            )
            if cur.rowcount != 1:
                return False
        else:
            # Compatibility for direct deterministic dispatch callers/tests.
            cur.execute(
                """UPDATE inbound_processing SET state = 'processed', processed_at = now(),
                       next_retry_at = NULL, claim_token = NULL, claimed_until = NULL,
                       updated_at = now() WHERE message_id = %s""", (message_id,),
            )
        cur.execute("UPDATE messages SET processed_at = now(), dispatch_claimed_at = NULL "
                    "WHERE message_id = %s", (message_id,))
        return cur.rowcount == 1


def claim_inbound(db: Database, *, message_id: str | None = None,
                  limit: int = 25, force: bool = False,
                  lease: timedelta = INBOUND_CLAIM_LEASE) -> list[dict]:
    """Claim due or lease-expired inbound jobs, fencing earlier workers."""
    with db.transaction() as cur:
        cur.execute(
            """UPDATE inbound_processing
               SET state = 'failed', failed_at = now(), next_retry_at = NULL,
                   claim_token = NULL, claimed_until = NULL,
                   last_error_code = COALESCE(last_error_code,
                       CASE WHEN dispatch_started_at IS NOT NULL
                            THEN 'worker_lease_expired_after_dispatch_started'
                            ELSE 'attempts_exhausted' END),
                   last_error = COALESCE(last_error,
                       CASE WHEN dispatch_started_at IS NOT NULL
                            THEN 'worker lease expired after business dispatch began'
                            ELSE 'maximum inbound attempts reached' END),
                   updated_at = now()
               WHERE ((state = 'pending' AND attempt_count >= %(max_attempts)s
                       AND (next_retry_at IS NULL OR next_retry_at <= now()))
                   OR (state = 'processing' AND claimed_until <= now()
                       AND (dispatch_started_at IS NOT NULL
                            OR attempt_count >= %(max_attempts)s)))
                 AND (%(message_id)s::uuid IS NULL OR message_id = %(message_id)s)""",
            {"max_attempts": INBOUND_MAX_ATTEMPTS, "message_id": message_id},
        )
        cur.execute(
            """WITH candidates AS (
                   SELECT p.processing_id
                   FROM inbound_processing p
                   JOIN messages m ON m.message_id = p.message_id
                   WHERE m.direction = 'IN' AND m.processed_at IS NULL
                     AND p.attempt_count < %(max_attempts)s
                     AND ((%(force)s AND p.state = 'pending')
                          OR (p.state = 'pending'
                              AND (p.next_retry_at IS NULL OR p.next_retry_at <= now()))
                          OR (p.state = 'processing' AND p.claimed_until <= now()
                              AND p.dispatch_started_at IS NULL))
                     AND (p.state = 'pending'
                          OR (p.state = 'processing' AND p.claimed_until <= now()))
                     AND (%(message_id)s::uuid IS NULL OR p.message_id = %(message_id)s)
                   ORDER BY COALESCE(p.next_retry_at, p.created_at), p.processing_id
                   LIMIT %(limit)s
                   FOR UPDATE OF p SKIP LOCKED
               )
               UPDATE inbound_processing p
               SET state = 'processing', attempt_count = p.attempt_count + 1,
                   claim_token = gen_random_uuid(), claimed_until = now() + %(lease)s,
                   dispatch_started_at = NULL, next_retry_at = NULL, updated_at = now()
               FROM candidates c, messages m, conversations v
               WHERE p.processing_id = c.processing_id
                 AND m.message_id = p.message_id
                 AND v.conversation_id = m.conversation_id
               RETURNING p.message_id, p.claim_token::text AS claim_token,
                         p.attempt_count, m.wa_message_id, m.body, m.payload, m.received_at,
                         m.conversation_id, v.phone""",
            {"max_attempts": INBOUND_MAX_ATTEMPTS, "force": force,
             "message_id": message_id, "limit": limit, "lease": lease},
        )
        return cur.fetchall()


def fail_inbound_attempt(db: Database, message_id: str, claim_token: str,
                         error: BaseException, *, retryable: bool = True) -> str | None:
    """Schedule bounded retry or terminal failure, only for the owning worker."""
    with db.transaction() as cur:
        cur.execute(
            """UPDATE inbound_processing
               SET state = CASE WHEN %(retryable)s AND attempt_count < %(max_attempts)s
                                      AND dispatch_started_at IS NULL
                                THEN 'pending' ELSE 'failed' END,
                   next_retry_at = CASE WHEN %(retryable)s AND attempt_count < %(max_attempts)s
                                              AND dispatch_started_at IS NULL
                                        THEN now() + %(delay)s ELSE NULL END,
                   last_error_code = %(error_code)s, last_error = %(error_text)s,
                   failed_at = CASE WHEN NOT %(retryable)s OR attempt_count >= %(max_attempts)s
                                         OR dispatch_started_at IS NOT NULL
                                   THEN now() ELSE NULL END,
                   claim_token = NULL, claimed_until = NULL, updated_at = now()
               WHERE message_id = %(message_id)s AND state = 'processing'
                 AND claim_token = %(claim_token)s
               RETURNING state""",
            {"retryable": retryable, "max_attempts": INBOUND_MAX_ATTEMPTS,
             "delay": _retry_delay_seconds(db, message_id, claim_token),
             "error_code": type(error).__name__[:100],
             "error_text": type(error).__name__[:500],
             "message_id": message_id, "claim_token": claim_token},
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("UPDATE messages SET dispatch_claimed_at = NULL WHERE message_id = %s",
                    (message_id,))
        return row["state"]


def mark_dispatch_started(db: Database, message_id: str, claim_token: str | None) -> bool:
    """Record the at-most-once business side-effect boundary for a worker."""
    if claim_token is None:
        return True
    return db.execute(
        """UPDATE inbound_processing SET dispatch_started_at = now(), updated_at = now()
           WHERE message_id = %s AND state = 'processing' AND claim_token = %s
             AND claimed_until > now()""", (message_id, claim_token),
    ) == 1


def inbound_error_is_retryable(error: BaseException) -> bool:
    """Avoid repeating deterministic input/programming/constraint failures."""
    if isinstance(error, (ValueError, TypeError, KeyError, AssertionError)):
        return False
    if type(error).__name__ in {
        "UniqueViolation", "ForeignKeyViolation", "CheckViolation",
        "NotNullViolation", "InvalidTextRepresentation",
    }:
        return False
    return True


def _retry_delay_seconds(db: Database, message_id: str, claim_token: str) -> timedelta:
    row = db.query_one(
        "SELECT attempt_count FROM inbound_processing WHERE message_id = %s "
        "AND state = 'processing' AND claim_token = %s", (message_id, claim_token),
    )
    attempt = (row or {}).get("attempt_count", 1)
    ceiling = min(INBOUND_BACKOFF_MAX_SECONDS,
                  INBOUND_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    low = ceiling * (1 - INBOUND_BACKOFF_JITTER)
    high = min(INBOUND_BACKOFF_MAX_SECONDS,
               ceiling * (1 + INBOUND_BACKOFF_JITTER))
    seconds = random.uniform(low, high)
    return timedelta(seconds=seconds)


def claim_stale_unprocessed(db: Database, *, limit: int = 25,
                            stale_after: timedelta = timedelta(minutes=15)) -> list[dict]:
    """Atomically claim messages whose dispatch has not completed.

    A fresh inbound row starts claimed by its webhook task. If that task could
    not send a reply or died, the scheduler may reclaim it after the lease. The
    provider message id remains unique, so webhook retries still cannot create
    a second inbound record.
    """
    # Retain this public helper name for the existing job endpoint and tests.
    # Fresh rows are backfilled with the same 15-minute initial delay.
    return claim_inbound(db, limit=limit)


def inbound_from_row(db: Database, row: dict) -> InboundMessage:
    """Rebuild the normalized message needed to retry a stored dispatch."""
    conversation = db.query_one(
        "SELECT phone FROM conversations WHERE conversation_id = %s",
        (row["conversation_id"],),
    )
    raw = row.get("payload") or {}
    msg_type = raw.get("type", "text")
    media = raw.get(msg_type) or {}
    interactive = raw.get("interactive") or {}
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    timestamp = raw.get("timestamp")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp = None
    return InboundMessage(
        provider_message_id=row["wa_message_id"],
        from_phone=(conversation or {}).get("phone", ""),
        text=row.get("body"),
        message_type=msg_type,
        timestamp=timestamp,
        raw=raw,
        button_id=reply.get("id"),
        media_id=media.get("id") if msg_type in ("image", "document") else None,
        media_type=media.get("mime_type") if msg_type in ("image", "document") else None,
    )


def record_outbound(db: Database, phone: str, body: str,
                    provider_message_id: str | None = None) -> None:
    """Record a reply we sent, and mark the guest's enquiry as responded."""
    with db.transaction() as cur:
        record_outbound_tx(cur, phone, body, provider_message_id)


def record_outbound_tx(cur, phone: str, body: str,
                       provider_message_id: str | None = None) -> None:
    """Record provider acceptance in the caller's transaction."""
    cur.execute(
        "INSERT INTO conversations (phone) VALUES (%s) "
        "ON CONFLICT (phone) DO UPDATE SET last_activity = now() "
        "RETURNING conversation_id", (phone,),
    )
    conversation_id = str(cur.fetchone()["conversation_id"])
    cur.execute(
        "INSERT INTO messages (wa_message_id, conversation_id, direction, body, processed_at) "
        "VALUES (%s, %s, 'OUT', %s, now()) ON CONFLICT (wa_message_id) DO NOTHING",
        (provider_message_id, conversation_id, body),
    )
    cur.execute(
        "UPDATE enquiries SET responded = true, responded_at = now(), status = 'RESPONDED' "
        "WHERE phone = %s AND status = 'OPEN'", (phone,),
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


def merge_conversation_state(db: Database, phone: str, patch: dict) -> None:
    """Merge top-level keys into the stored state in one statement.

    A read-then-write of the whole document loses a concurrent writer's key:
    two taps arriving together both read the old state and the second write
    drops the first. `||` merges inside the database, so each writer only ever
    changes its own keys.
    """
    db.execute(
        "INSERT INTO conversations (phone, state) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (phone) DO UPDATE SET "
        "state = conversations.state || EXCLUDED.state, last_activity = now()",
        (phone, _json(patch)),
    )


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)
