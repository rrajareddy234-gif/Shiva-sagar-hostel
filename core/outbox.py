"""Durable, idempotent WhatsApp outbound queue and state transitions."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import uuid
from datetime import timedelta
from pathlib import Path

from core.channel.adapter import Flow, Prompt
from core.db import Database

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
SEND_LEASE = timedelta(seconds=60)
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 30 * 60
PHONE_LIMIT_BASE_SECONDS = 60
PAIR_LIMIT_BASE_SECONDS = 5 * 60
PAIR_LIMIT_MAX_SECONDS = 2 * 60 * 60
TERMINAL_PAYLOAD_RETENTION = timedelta(days=30)
UNKNOWN_PAYLOAD_RETENTION = timedelta(days=90)


def prompt_payload(prompt: Prompt) -> dict:
    return {
        "body": prompt.body,
        "buttons": [{"id": b.id, "title": b.title} for b in prompt.buttons],
        "rows": [{"id": b.id, "title": b.title} for b in prompt.rows],
        "button_text": prompt.button_text,
    }


def flow_payload(flow: Flow) -> dict:
    return {
        "flow_id": flow.flow_id,
        "screen": flow.screen,
        "cta": flow.cta,
        "body": flow.body,
        "data": flow.data,
        "flow_token": flow.flow_token,
        "mode": flow.mode,
        "header": flow.header,
        "footer": flow.footer,
    }


def enqueue(db: Database, *, recipient: str, message_type: str, payload: dict,
            idempotency_key: str, source_inbound_id: str | None = None,
            source_event_type: str | None = None,
            source_event_id: str | None = None,
            expires_at=None) -> str:
    with db.transaction() as cur:
        return enqueue_tx(cur, recipient=recipient, message_type=message_type,
                          payload=payload, idempotency_key=idempotency_key,
                          source_inbound_id=source_inbound_id,
                          source_event_type=source_event_type,
                          source_event_id=source_event_id, expires_at=expires_at)


def enqueue_tx(cur, *, recipient: str, message_type: str, payload: dict,
               idempotency_key: str, source_inbound_id: str | None = None,
               source_event_type: str | None = None,
               source_event_id: str | None = None, expires_at=None,
               ordering_key: str | None = None) -> str:
    """Insert one logical message inside the caller's business transaction."""
    cur.execute("SELECT outbound_id FROM outbound_messages WHERE idempotency_key = %s",
                (idempotency_key,))
    existing = cur.fetchone()
    if existing:
        return str(existing["outbound_id"])
    if ordering_key is None:
        ordering_key = (str(source_inbound_id) if source_inbound_id else
                        f"{source_event_type}:{source_event_id}"
                        if source_event_type and source_event_id else None)

    cur.execute(
        "INSERT INTO outbound_recipient_sequences (recipient) VALUES (%s) "
        "ON CONFLICT (recipient) DO NOTHING", (recipient,),
    )
    cur.execute("SELECT next_sequence FROM outbound_recipient_sequences "
                "WHERE recipient = %s FOR UPDATE", (recipient,))
    sequence = cur.fetchone()["next_sequence"]
    cur.execute("UPDATE outbound_recipient_sequences SET next_sequence = next_sequence + 1 "
                "WHERE recipient = %s", (recipient,))
    cur.execute(
        """INSERT INTO outbound_messages
           (idempotency_key, source_inbound_id, source_event_type, source_event_id,
            recipient, recipient_sequence, ordering_key, message_type, payload, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING outbound_id""",
            (idempotency_key, source_inbound_id, source_event_type, source_event_id,
             recipient, sequence, ordering_key, message_type, json.dumps(payload, ensure_ascii=False),
         expires_at),
    )
    row = cur.fetchone()
    if row:
        return str(row["outbound_id"])
    cur.execute("SELECT outbound_id FROM outbound_messages WHERE idempotency_key = %s",
                (idempotency_key,))
    return str(cur.fetchone()["outbound_id"])


def enqueue_many(db: Database, messages: list[dict], *, source_inbound_id: str | None = None,
                 claim_token: str | None = None) -> list[str]:
    """Atomically enqueue a reply sequence and complete its inbound message."""
    with db.transaction() as cur:
        ids = [enqueue_tx(cur, source_inbound_id=source_inbound_id, **message)
               for message in messages]
        if source_inbound_id is not None:
            if claim_token is None:
                cur.execute(
                    """UPDATE inbound_processing SET state='processed', processed_at=now(),
                       claim_token=NULL, claimed_until=NULL, next_retry_at=NULL, updated_at=now()
                       WHERE message_id=%s""", (source_inbound_id,),
                )
            else:
                cur.execute(
                    """UPDATE inbound_processing SET state='processed', processed_at=now(),
                       claim_token=NULL, claimed_until=NULL, next_retry_at=NULL, updated_at=now()
                       WHERE message_id=%s AND state='processing' AND claim_token=%s""",
                    (source_inbound_id, claim_token),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("inbound claim was lost before outbox enqueue")
            cur.execute("UPDATE messages SET processed_at=now(), dispatch_claimed_at=NULL "
                        "WHERE message_id=%s", (source_inbound_id,))
        return ids


def claim_next(db: Database, *, phone_scope: str, owner_phone: str | None = None,
               lease: timedelta = SEND_LEASE,
               owner_identities: list[str] | None = None,
               guest_ok: bool = True, owner_ok: bool = True) -> dict | None:
    """Claim one send with atomic global and recipient gates and per-recipient order.

    ``owner_identities`` turns on the send gate at the query itself: when it is
    a list of digit strings, only rows whose audience is currently allowed are
    eligible. A guest row is eligible only when ``guest_ok``, an owner/staff row
    only when ``owner_ok``. Filtering here -- rather than claiming and dropping
    -- means a blocked message is never selected, so it cannot spin the worker
    and cannot starve a message that is allowed to go out. ``None`` disables the
    filter, which is what the outbox's own tests use.
    """
    token = str(uuid.uuid4())
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO outbound_send_gates (scope_type, scope_key) VALUES ('phone', %s) "
            "ON CONFLICT DO NOTHING", (phone_scope,),
        )
        cur.execute("SELECT * FROM outbound_send_gates WHERE scope_type='phone' "
                    "AND scope_key=%s FOR UPDATE", (phone_scope,))
        phone_gate = cur.fetchone()
        now = cur.execute("SELECT now() AS now").fetchone()["now"]
        if phone_gate["blocked_until"] and phone_gate["blocked_until"] > now:
            return None
        if phone_gate["lease_until"] and phone_gate["lease_until"] > now:
            return None

        # A crash before the network boundary is safe to retry. After the
        # boundary it is ambiguous and must never be automatically resent.
        cur.execute(
            """WITH expired AS (
               SELECT outbound_id, send_started_at
               FROM outbound_messages
               WHERE state='sending' AND claimed_until <= now()
               ORDER BY claimed_until LIMIT 100 FOR UPDATE SKIP LOCKED
               )
               UPDATE outbound_messages o
               SET state = CASE WHEN expired.send_started_at IS NULL THEN 'pending' ELSE 'unknown' END,
                   attempt_count = CASE WHEN expired.send_started_at IS NULL
                                        THEN GREATEST(o.attempt_count - 1, 0) ELSE o.attempt_count END,
                   next_attempt_at = now(), claim_token=NULL, claimed_until=NULL,
                   payload_expires_at = CASE WHEN expired.send_started_at IS NULL THEN NULL
                                             ELSE now() + %s END,
                   last_error_code = CASE WHEN expired.send_started_at IS NULL THEN o.last_error_code
                                          ELSE 'worker_lost_after_send_started' END,
                   last_error_class = CASE WHEN expired.send_started_at IS NULL THEN o.last_error_class
                                           ELSE 'uncertain' END,
                   last_error = CASE WHEN expired.send_started_at IS NULL THEN o.last_error
                                     ELSE 'worker lease expired after send began' END,
                   updated_at=now()
               FROM expired WHERE o.outbound_id=expired.outbound_id
               RETURNING o.*""",
            (UNKNOWN_PAYLOAD_RETENTION,),
        )
        for expired_item in cur.fetchall():
            if expired_item["state"] == "unknown":
                _enqueue_terminal_alert(cur, owner_phone, expired_item, "unknown",
                                        "worker_lost_after_send_started")
        cur.execute("UPDATE outbound_send_gates SET lease_token=NULL, lease_until=NULL, "
                    "updated_at=now() WHERE lease_until <= now()")

        cur.execute(
            """SELECT o.* FROM outbound_messages o
               WHERE o.state='pending' AND o.payload IS NOT NULL
                 AND o.next_attempt_at <= now()
                 AND (o.expires_at IS NULL OR o.expires_at > now())
                 AND (
                     %(gated)s = false
                     OR (regexp_replace(o.recipient, '[^0-9]', '', 'g') = ANY(%(owners)s::text[])
                         AND %(owner_ok)s)
                     OR (NOT (regexp_replace(o.recipient, '[^0-9]', '', 'g') = ANY(%(owners)s::text[]))
                         AND %(guest_ok)s)
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM outbound_messages older
                     WHERE older.recipient=o.recipient
                       AND older.recipient_sequence < o.recipient_sequence
                       AND older.ordering_key=o.ordering_key
                       AND o.ordering_key IS NOT NULL
                       AND older.state IN ('pending','sending','unknown','failed'))
                 AND NOT EXISTS (
                     SELECT 1 FROM outbound_send_gates rg
                     WHERE rg.scope_type='recipient' AND rg.scope_key=o.recipient
                       AND ((rg.blocked_until IS NOT NULL AND rg.blocked_until > now())
                         OR (rg.lease_until IS NOT NULL AND rg.lease_until > now())))
               ORDER BY o.created_at, o.recipient_sequence
               LIMIT 1 FOR UPDATE OF o SKIP LOCKED""",
            {"gated": owner_identities is not None,
             "owners": owner_identities or [],
             "guest_ok": guest_ok, "owner_ok": owner_ok},
        )
        item = cur.fetchone()
        if item is None:
            return None
        cur.execute(
            "INSERT INTO outbound_send_gates (scope_type, scope_key) "
            "VALUES ('recipient', %s) ON CONFLICT DO NOTHING", (item["recipient"],),
        )
        cur.execute("SELECT * FROM outbound_send_gates WHERE scope_type='recipient' "
                    "AND scope_key=%s FOR UPDATE", (item["recipient"],))
        recipient_gate = cur.fetchone()
        if ((recipient_gate["blocked_until"] and recipient_gate["blocked_until"] > now)
                or (recipient_gate["lease_until"] and recipient_gate["lease_until"] > now)):
            return None
        attempt_id = str(uuid.uuid4())
        cur.execute(
            """UPDATE outbound_messages SET state='sending', attempt_count=attempt_count+1,
               claim_token=%s, claimed_until=now()+%s, attempt_id=%s,
               send_started_at=NULL, updated_at=now()
               WHERE outbound_id=%s RETURNING *""",
            (token, lease, attempt_id, item["outbound_id"]),
        )
        claimed = cur.fetchone()
        cur.execute("UPDATE outbound_send_gates SET lease_token=%s, lease_until=now()+%s, "
                    "updated_at=now() WHERE scope_type='phone' AND scope_key=%s",
                    (token, lease, phone_scope))
        cur.execute("UPDATE outbound_send_gates SET lease_token=%s, lease_until=now()+%s, "
                    "updated_at=now() WHERE scope_type='recipient' AND scope_key=%s",
                    (token, lease, item["recipient"]))
        claimed["claim_token"] = token
        claimed["attempt_id"] = attempt_id
        return claimed


def mark_send_started(db: Database, item: dict) -> bool:
    with db.transaction() as cur:
        cur.execute(
            """UPDATE outbound_messages SET send_started_at=now(), updated_at=now()
               WHERE outbound_id=%s AND state='sending' AND claim_token=%s
                 AND attempt_id=%s AND claimed_until > now()""",
            (item["outbound_id"], item["claim_token"], item["attempt_id"]),
        )
        return cur.rowcount == 1


def release_claim(db: Database, item: dict) -> bool:
    """Return a claimed-but-unsent message to 'pending', undoing the attempt.

    Used by the send gate when a row was claimed and then found to be blocked.
    The attempt is not counted and the message is not sent, so it is neither
    marked delivered nor charged against the retry budget. It waits, pending,
    for the switches; nothing re-selects it in the meantime.
    """
    with db.transaction() as cur:
        cur.execute(
            """UPDATE outbound_messages
               SET state='pending', attempt_count=GREATEST(attempt_count - 1, 0),
                   claim_token=NULL, claimed_until=NULL, attempt_id=NULL,
                   send_started_at=NULL, next_attempt_at=now(), updated_at=now()
               WHERE outbound_id=%s AND state='sending' AND claim_token=%s
                 AND attempt_id=%s""",
            (item["outbound_id"], item["claim_token"], item["attempt_id"]),
        )
        released = cur.rowcount == 1
        if released:
            cur.execute(
                "UPDATE outbound_send_gates SET lease_token=NULL, lease_until=NULL, "
                "updated_at=now() WHERE lease_token=%s", (item["claim_token"],))
        return released


def _backoff(attempt: int, *, base: int = BASE_BACKOFF_SECONDS,
             maximum: int = MAX_BACKOFF_SECONDS) -> timedelta:
    ceiling = min(maximum, base * (2 ** max(0, attempt - 1)))
    return timedelta(seconds=random.uniform(ceiling * 0.8,
                                             min(maximum, ceiling * 1.2)))


def complete_attempt(db: Database, item: dict, result, *, phone_scope: str,
                     owner_phone: str | None) -> str:
    """Persist a provider outcome and release/extend both gates atomically."""
    with db.transaction() as cur:
        # Match claim_next's phone -> recipient -> message lock order. In
        # particular, an expired sender and a late worker completion cannot
        # deadlock while exchanging the lease.
        for scope_type, scope_key in (("phone", phone_scope),
                                      ("recipient", item["recipient"])):
            cur.execute("SELECT 1 FROM outbound_send_gates WHERE scope_type=%s "
                        "AND scope_key=%s FOR UPDATE", (scope_type, scope_key))
        cur.execute("SELECT * FROM outbound_messages WHERE outbound_id=%s FOR UPDATE",
                    (item["outbound_id"],))
        current = cur.fetchone()
        if not current or current["attempt_id"] != uuid.UUID(str(item["attempt_id"])):
            return "stale"

        certainty = getattr(result, "certainty", "unknown")
        code = getattr(result, "error_code", None)
        http_status = getattr(result, "http_status", None)
        state = "unknown"
        delay = None
        phone_block = None
        recipient_block = None
        payload = current["payload"] or {}
        message_type = current["message_type"]

        if result.ok and result.provider_message_id:
            state = "accepted"
            cur.execute(
                """UPDATE outbound_messages SET state='accepted', provider_message_id=%s,
                   accepted_at=now(), payload_expires_at=now()+%s,
                   claim_token=NULL, claimed_until=NULL, updated_at=now()
                   WHERE outbound_id=%s""",
                (result.provider_message_id, TERMINAL_PAYLOAD_RETENTION,
                 current["outbound_id"]),
            )
            from core.inbox import record_outbound_tx
            body = _history_body(message_type, payload)
            record_outbound_tx(cur, current["recipient"], body,
                               result.provider_message_id)
            _reconcile_statuses(cur, str(current["outbound_id"]),
                                result.provider_message_id)
        elif result.ok:
            state = _set_unknown(cur, current, "accepted_without_provider_id",
                                 "provider response lacked message id")
        elif certainty == "unknown":
            state = _set_unknown(cur, current, code or "network_outcome_unknown",
                                 "provider acceptance could not be determined")
        else:
            rate_code = str(code or "")
            if rate_code == "130429":
                delay = _provider_delay(result, current["attempt_count"],
                                        PHONE_LIMIT_BASE_SECONDS, MAX_BACKOFF_SECONDS)
                phone_block = delay
            elif rate_code == "131056":
                delay = _provider_delay(result, current["attempt_count"],
                                        PAIR_LIMIT_BASE_SECONDS, PAIR_LIMIT_MAX_SECONDS)
                recipient_block = delay
            elif _is_permanent(http_status, rate_code):
                state = "failed"
                cur.execute(
                    """UPDATE outbound_messages SET state='failed', failed_at=now(),
                       payload_expires_at=now()+%s, last_error_code=%s,
                       last_error_class='permanent', last_error=%s,
                       claim_token=NULL, claimed_until=NULL, updated_at=now()
                       WHERE outbound_id=%s""",
                    (TERMINAL_PAYLOAD_RETENTION, rate_code or str(http_status or "provider_error"),
                     "permanent provider rejection", current["outbound_id"]),
                )
            else:
                delay = _backoff(current["attempt_count"])

            if delay is not None:
                if (message_type in ("document", "interactive")
                        and payload.get("fallback_text")
                        and certainty == "rejected"
                        and rate_code not in ("130429", "131056")
                        and http_status != 429):
                    payload = {"text": payload["fallback_text"],
                               "file_ref": payload.get("file_ref")}
                    message_type = "text"
                    cur.execute("UPDATE outbound_messages SET payload=%s::jsonb, message_type='text' "
                                "WHERE outbound_id=%s",
                                (json.dumps(payload, ensure_ascii=False), current["outbound_id"]))
                if current["attempt_count"] >= MAX_ATTEMPTS:
                    state = "failed"
                    cur.execute(
                        """UPDATE outbound_messages SET state='failed', failed_at=now(),
                           payload_expires_at=now()+%s, last_error_code=%s,
                           last_error_class='exhausted', last_error='maximum attempts exhausted',
                           claim_token=NULL, claimed_until=NULL, updated_at=now()
                           WHERE outbound_id=%s""",
                        (TERMINAL_PAYLOAD_RETENTION, rate_code or "retry_exhausted",
                         current["outbound_id"]),
                    )
                else:
                    state = "pending"
                    cur.execute(
                        """UPDATE outbound_messages SET state='pending', next_attempt_at=now()+%s,
                           last_error_code=%s, last_error_class=%s,
                           last_error='provider rejected message; retry scheduled',
                           claim_token=NULL, claimed_until=NULL, updated_at=now()
                           WHERE outbound_id=%s""",
                        (delay, rate_code or str(http_status or "transient_rejection"),
                         "rate_limit" if rate_code in ("130429", "131056") else "transient",
                         current["outbound_id"]),
                    )

        _release_gates(cur, phone_scope, current["recipient"], item["claim_token"],
                       phone_block=phone_block, recipient_block=recipient_block)
        if state in ("failed", "unknown"):
            _enqueue_terminal_alert(cur, owner_phone, current, state, code)
        return state


def _set_unknown(cur, current, code: str, message: str) -> str:
    cur.execute(
        """UPDATE outbound_messages SET state='unknown', payload_expires_at=now()+%s,
           last_error_code=%s, last_error_class='uncertain', last_error=%s,
           claim_token=NULL, claimed_until=NULL, updated_at=now()
           WHERE outbound_id=%s""",
        (UNKNOWN_PAYLOAD_RETENTION, code, message, current["outbound_id"]),
    )
    return "unknown"


def _release_gates(cur, phone_scope: str, recipient: str, token: str, *,
                   phone_block=None, recipient_block=None) -> None:
    for scope_type, scope_key, block in (
        ("phone", phone_scope, phone_block), ("recipient", recipient, recipient_block),
    ):
        cur.execute(
            """UPDATE outbound_send_gates SET lease_token=NULL, lease_until=NULL,
               blocked_until=CASE WHEN %s::interval IS NULL THEN blocked_until
                                  ELSE GREATEST(COALESCE(blocked_until, now()), now()+%s::interval) END,
               updated_at=now()
               WHERE scope_type=%s AND scope_key=%s AND lease_token=%s""",
            (block, block, scope_type, scope_key, token),
        )


def _enqueue_terminal_alert(cur, owner_phone: str | None, current: dict,
                            state: str, code: str | None) -> None:
    if not owner_phone or current["source_event_type"] == "outbound_failure_alert":
        return
    text = (f"WhatsApp message {current['outbound_id']} is {state}. "
            f"Error code: {code or current['last_error_code'] or 'unknown'}. "
            "Review the outbox before taking action; unknown messages were not resent.")
    enqueue_tx(
        cur, recipient=owner_phone, message_type="text", payload={"text": text},
        idempotency_key=f"outbound-failure:{current['outbound_id']}",
        source_event_type="outbound_failure_alert",
        source_event_id=str(current["outbound_id"]),
    )


def _provider_delay(result, attempt: int, base: int, maximum: int) -> timedelta:
    supplied = getattr(result, "retry_after_seconds", None)
    if supplied is not None:
        return timedelta(seconds=min(maximum, max(1, int(supplied))))
    return _backoff(attempt, base=base, maximum=maximum)


def _is_permanent(http_status: int | None, code: str) -> bool:
    if code in ("130429", "131056") or http_status == 429:
        return False
    return http_status is not None and 400 <= http_status < 500


def _history_body(message_type: str, payload: dict) -> str:
    if message_type == "text":
        return payload.get("text", "")
    if message_type == "interactive":
        return (payload.get("prompt") or {}).get("body", "")
    if message_type == "flow":
        return payload.get("body", "")
    if message_type == "template":
        return payload.get("template_name", "")
    return payload.get("caption", payload.get("filename", "[document]"))


def _reconcile_statuses(cur, outbound_id: str, provider_message_id: str) -> None:
    cur.execute(
        """SELECT status FROM outbound_delivery_events WHERE provider_message_id=%s
           ORDER BY status_timestamp DESC NULLS LAST, received_at DESC LIMIT 1""",
        (provider_message_id,),
    )
    event = cur.fetchone()
    if event:
        _apply_status(cur, outbound_id, event["status"])


def apply_status(db: Database, provider_message_id: str, status: str) -> None:
    with db.transaction() as cur:
        cur.execute("SELECT outbound_id FROM outbound_messages WHERE provider_message_id=%s "
                    "FOR UPDATE", (provider_message_id,))
        row = cur.fetchone()
        if row:
            _apply_status(cur, str(row["outbound_id"]), status)


def _apply_status(cur, outbound_id: str, status: str) -> None:
    mapping = {"sent": "accepted", "delivered": "delivered", "read": "read",
               "failed": "failed"}
    target = mapping.get(status)
    if not target:
        return
    rank = {"accepted": 1, "delivered": 2, "read": 3, "failed": 4}
    cur.execute("SELECT state FROM outbound_messages WHERE outbound_id=%s FOR UPDATE",
                (outbound_id,))
    current = cur.fetchone()
    if not current or current["state"] == "cancelled":
        return
    if current["state"] in ("failed", "read") or rank[target] < rank.get(current["state"], 0):
        return
    cur.execute(
        """UPDATE outbound_messages SET state=%s,
           delivered_at=CASE WHEN %s='delivered' THEN COALESCE(delivered_at,now()) ELSE delivered_at END,
           read_at=CASE WHEN %s='read' THEN COALESCE(read_at,now()) ELSE read_at END,
           failed_at=CASE WHEN %s='failed' THEN COALESCE(failed_at,now()) ELSE failed_at END,
           payload_expires_at=CASE WHEN %s IN ('delivered','read','failed')
                                   THEN COALESCE(payload_expires_at,now()+%s)
                                   ELSE payload_expires_at END,
           updated_at=now() WHERE outbound_id=%s""",
        (target, target, target, target, target, TERMINAL_PAYLOAD_RETENTION, outbound_id),
    )


def delivery_event_key(provider_message_id: str, status: str,
                       status_timestamp, recipient_id: str | None) -> str:
    value = "|".join((provider_message_id, status, str(status_timestamp or ""),
                      recipient_id or ""))
    return hashlib.sha256(value.encode()).hexdigest()


def attach_file(root: str, content: bytes, filename: str) -> str:
    """Write an attachment to a private spool and return its opaque basename."""
    suffix = Path(filename).suffix[:12]
    directory = Path(root) / "outbound_outbox"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    name = f"{uuid.uuid4().hex}{suffix}"
    path = directory / name
    fd = path.open("xb")
    try:
        path.chmod(0o600)
        fd.write(content)
    finally:
        fd.close()
    return name


def read_attachment(root: str, reference: str) -> bytes:
    if Path(reference).name != reference:
        raise ValueError("invalid private attachment reference")
    path = Path(root) / "outbound_outbox" / reference
    return path.read_bytes()


def scrub_expired_payloads(db: Database, root: str, *, limit: int = 100) -> int:
    """Clear terminal payloads and their private files after retention."""
    with db.transaction() as cur:
        cur.execute(
            """WITH due AS (
               SELECT outbound_id, payload->>'file_ref' AS file_ref
               FROM outbound_messages
               WHERE payload IS NOT NULL AND payload_expires_at <= now()
                 AND state IN ('accepted','delivered','read','failed','unknown','cancelled')
               ORDER BY payload_expires_at LIMIT %s FOR UPDATE SKIP LOCKED)
               UPDATE outbound_messages o SET payload=NULL, updated_at=now()
               FROM due WHERE o.outbound_id=due.outbound_id
               RETURNING due.file_ref""", (limit,),
        )
        refs = [row["file_ref"] for row in cur.fetchall() if row["file_ref"]]
    for ref in refs:
        try:
            path = Path(root) / "outbound_outbox" / ref
            if path.parent.resolve() == (Path(root) / "outbound_outbox").resolve():
                path.unlink(missing_ok=True)
        except OSError:
            log.exception("could not remove expired private outbox attachment")
    return len(refs)
