from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from core import inbox, outbox
from core.channel.adapter import SendResult
from core.db import Database


def _enqueue(db, recipient="+919000000001", key="k1", text="hello"):
    return outbox.enqueue(db, recipient=recipient, message_type="text",
                          payload={"text": text}, idempotency_key=key)


def test_idempotency_and_recipient_order_are_stable(db):
    first = _enqueue(db)
    assert _enqueue(db) == first
    second = _enqueue(db, key="k2", text="second")
    a = outbox.claim_next(db, phone_scope="phone-id")
    assert str(a["outbound_id"]) == first
    assert outbox.mark_send_started(db, a)
    outbox.complete_attempt(db, a, SendResult(True, provider_message_id="wamid.1"),
                            phone_scope="phone-id", owner_phone=None)
    b = outbox.claim_next(db, phone_scope="phone-id")
    assert str(b["outbound_id"]) == second


def test_permanent_failure_blocks_followup_in_same_ordered_group(db):
    common = {"recipient": "+919000000001", "source_event_type": "booking",
              "source_event_id": "booking-1"}
    outbox.enqueue(db, message_type="text", payload={"text": "answer"},
                   idempotency_key="group:first", **common)
    outbox.enqueue(db, message_type="interactive", payload={"prompt": {"body": "tap"}},
                   idempotency_key="group:second", **common)
    first = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, first)
    outbox.complete_attempt(db, first,
                            SendResult(False, certainty="rejected", http_status=400),
                            phone_scope="phone-id", owner_phone=None)
    assert outbox.claim_next(db, phone_scope="phone-id") is None


def test_pre_send_worker_crash_releases_claim_for_retry(db):
    outbound_id = _enqueue(db)
    outbox.claim_next(db, phone_scope="phone-id")
    with db.transaction() as cur:
        cur.execute("UPDATE outbound_messages SET claimed_until=now()-interval '1 second' "
                    "WHERE outbound_id=%s", (outbound_id,))
        cur.execute("UPDATE outbound_send_gates SET lease_until=now()-interval '1 second'")
    again = outbox.claim_next(db, phone_scope="phone-id")
    assert again and str(again["outbound_id"]) == outbound_id
    assert again["attempt_count"] == 1


def test_concurrent_workers_share_one_atomic_phone_send_slot(db):
    from tests.conftest import TEST_DSN
    _enqueue(db)
    _enqueue(db, recipient="+919000000002", key="k2")

    def claim(_):
        return outbox.claim_next(Database(TEST_DSN), phone_scope="phone-id")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    assert sum(item is not None for item in claims) == 1


def test_post_send_worker_crash_becomes_unknown_and_is_never_reclaimed(db):
    outbound_id = _enqueue(db)
    item = outbox.claim_next(db, phone_scope="phone-id")
    assert outbox.mark_send_started(db, item)
    with db.transaction() as cur:
        cur.execute("UPDATE outbound_messages SET claimed_until=now()-interval '1 second' "
                    "WHERE outbound_id=%s", (outbound_id,))
        cur.execute("UPDATE outbound_send_gates SET lease_until=now()-interval '1 second'")
    assert outbox.claim_next(db, phone_scope="phone-id") is None
    assert db.query_one("SELECT state FROM outbound_messages WHERE outbound_id=%s",
                        (outbound_id,))["state"] == "unknown"


def test_meta_130429_sets_phone_gate_without_changing_payload(db):
    outbound_id = _enqueue(db)
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    state = outbox.complete_attempt(
        db, item, SendResult(False, error="rate limited", certainty="rejected",
                             error_code="130429", http_status=429),
        phone_scope="phone-id", owner_phone=None)
    assert state == "pending"
    assert db.query_one("SELECT blocked_until > now() AS blocked FROM outbound_send_gates "
                        "WHERE scope_type='phone' AND scope_key='phone-id'")["blocked"]
    row = db.query_one("SELECT payload,state FROM outbound_messages WHERE outbound_id=%s",
                       (outbound_id,))
    assert row["payload"] == {"text": "hello"}
    assert row["state"] == "pending"


def test_meta_131056_sets_recipient_gate_only(db):
    _enqueue(db, recipient="+919000000001", key="one")
    _enqueue(db, recipient="+919000000002", key="two")
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    outbox.complete_attempt(
        db, item, SendResult(False, error="pair limit", certainty="rejected",
                             error_code="131056", http_status=400),
        phone_scope="phone-id", owner_phone=None)
    assert db.query_one("SELECT blocked_until > now() AS blocked FROM outbound_send_gates "
                        "WHERE scope_type='recipient' AND scope_key=%s",
                        (item["recipient"],))["blocked"]
    # A different recipient remains eligible after the phone-wide send slot is released.
    other = outbox.claim_next(db, phone_scope="phone-id")
    assert other and other["recipient"] != item["recipient"]


def test_permanent_rejection_and_retry_exhaustion_stop(db):
    permanent = _enqueue(db, key="permanent")
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    assert outbox.complete_attempt(
        db, item, SendResult(False, certainty="rejected", http_status=400,
                             error_code="100"), phone_scope="phone-id",
        owner_phone=None) == "failed"
    assert db.query_one("SELECT state FROM outbound_messages WHERE outbound_id=%s",
                        (permanent,))["state"] == "failed"

    exhausted = _enqueue(db, key="exhausted")
    for attempt in range(outbox.MAX_ATTEMPTS):
        item = outbox.claim_next(db, phone_scope="phone-id")
        assert item
        outbox.mark_send_started(db, item)
        state = outbox.complete_attempt(
            db, item, SendResult(False, certainty="rejected", http_status=500,
                                 error_code="server_error"),
            phone_scope="phone-id", owner_phone=None)
        if attempt < outbox.MAX_ATTEMPTS - 1:
            with db.transaction() as cur:
                cur.execute("UPDATE outbound_messages SET next_attempt_at=now() "
                            "WHERE outbound_id=%s", (exhausted,))
        else:
            assert state == "failed"
    assert db.query_one("SELECT state,attempt_count FROM outbound_messages WHERE outbound_id=%s",
                        (exhausted,)) == {"state": "failed", "attempt_count": outbox.MAX_ATTEMPTS}


def test_delivery_callbacks_are_distinct_deduplicated_and_resolve_unknown(db):
    outbound_id = _enqueue(db)
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    status = inbox.DeliveryStatus(provider_message_id="wamid.callback", status="delivered",
                                 timestamp=1770000000, recipient_id="recipient", errors=[])
    inbox.store_delivery_status(db, status)
    inbox.store_delivery_status(db, status)
    outbox.complete_attempt(db, item,
                            SendResult(True, provider_message_id="wamid.callback"),
                            phone_scope="phone-id", owner_phone=None)
    row = db.query_one("SELECT state FROM outbound_messages WHERE outbound_id=%s", (outbound_id,))
    assert row["state"] == "delivered"
    assert db.query_one("SELECT count(*) AS n FROM outbound_delivery_events")["n"] == 1


def test_failed_delivery_callback_queues_one_owner_alert(db):
    outbound_id = _enqueue(db)
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    outbox.complete_attempt(db, item, SendResult(True, provider_message_id="wamid.failed"),
                            phone_scope="phone-id", owner_phone=None)
    status = inbox.DeliveryStatus(provider_message_id="wamid.failed", status="failed",
                                 timestamp=1770000001, recipient_id="recipient",
                                 errors={"items": [{"error": {"code": 131000}}]})
    inbox.store_delivery_status(db, status, owner_phone="+919000000999")
    inbox.store_delivery_status(db, status, owner_phone="+919000000999")
    assert db.query_one("SELECT state FROM outbound_messages WHERE outbound_id=%s",
                        (outbound_id,))["state"] == "failed"
    assert db.query_one("""SELECT count(*) AS n FROM outbound_messages
                         WHERE source_event_type='outbound_failure_alert'""")["n"] == 1


def test_attachment_bytes_never_enter_payload_json(db, tmp_path):
    content = b"private payment proof bytes"
    ref = outbox.attach_file(str(tmp_path), content, "proof.jpg")
    _enqueue(db, key="proof", text="caption")
    with db.transaction() as cur:
        cur.execute("UPDATE outbound_messages SET message_type='document',payload=%s::jsonb "
                    "WHERE idempotency_key='proof'", (json.dumps({"file_ref": ref}),))
    stored = db.query_one("SELECT payload FROM outbound_messages WHERE idempotency_key='proof'")
    assert content.decode() not in str(stored["payload"])
    assert outbox.read_attachment(str(tmp_path), ref) == content


def _document_item(db, tmp_path, recipient):
    content = b"file bytes"
    ref = outbox.attach_file(str(tmp_path), content, "file.txt")
    outbox.enqueue(db, recipient=recipient, message_type="document",
                   payload={"file_ref": ref, "filename": "file.txt"},
                   idempotency_key=f"doc:{recipient}")
    item = outbox.claim_next(db, phone_scope="phone-id")
    outbox.mark_send_started(db, item)
    return item


def _worker_settings(owner_phone, proof_dir="payment_proofs"):
    from core.config import Settings
    from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN
    return Settings(
        database_url=TEST_DSN, channel_provider="fake", owner_phone=owner_phone,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="1",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="gemini-2.5-flash", ai_api_key=None, ai_base_url=None,
        payment_proof_dir=str(proof_dir), send_enabled=True)


def test_a_guest_document_failure_is_reported_interactively(db, channel, tmp_path):
    """The fallback must not be the one bare text in a guest conversation."""
    from core import outbound_worker

    settings = _worker_settings("+919999000000", tmp_path)
    channel.fail_document = True
    item = _document_item(db, tmp_path, "+919000000123")
    outbound_worker._send(channel, item, settings)

    assert channel.attempts.count("text") == 0, "no bare text to a guest"
    assert len(channel.prompts) == 1 and channel.prompts[0][0] == "+919000000123"


def test_an_owner_document_failure_is_reported_in_text(db, channel, tmp_path):
    """The owner is not a guest: plain text is still fine for them."""
    from core import outbound_worker

    settings = _worker_settings("+919999000000", tmp_path)
    channel.fail_document = True
    item = _document_item(db, tmp_path, "+919999000000")
    outbound_worker._send(channel, item, settings)

    assert channel.prompts == []
    assert any("could not send the file" in text for _, text in channel.texts)
