"""Webhook endpoint tests.

These use FastAPI's TestClient against the real adapter and the real database.
No real messages are sent; the outbound call is never reached because these
tests only exercise the inbound path.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import APP_SECRET, VERIFY_TOKEN, FakeChannel, channel, client  # noqa: F401  # noqa: F401


def _payload(message_id="wamid.HOOK1", text="hello", sender="919000001000") -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": sender, "id": message_id, "timestamp": "1735689600",
                          "type": "text", "text": {"body": text}}],
        }}]}],
    }).encode()


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={sig}"}


# --- health ----------------------------------------------------------------

def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


# --- GET verification handshake --------------------------------------------

def test_verification_handshake_echoes_the_challenge(client):
    response = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "1234567890"})
    assert response.status_code == 200
    assert response.text == "1234567890"


def test_verification_rejected_with_wrong_token(client):
    response = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"})
    assert response.status_code == 403


# --- POST message handling -------------------------------------------------

def test_valid_message_accepted(client, db):
    body = _payload()
    response = client.post("/webhook", content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json()["status"] == "received"


def test_message_is_stored(client, db):
    body = _payload()
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT wa_message_id, direction FROM messages WHERE direction = 'IN'")
    assert row["wa_message_id"] == "wamid.HOOK1"
    assert row["direction"] == "IN"


def test_invalid_signature_rejected(client, db):
    body = _payload()
    response = client.post("/webhook", content=body,
                           headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert response.status_code == 403
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 0


def test_missing_signature_rejected(client):
    response = client.post("/webhook", content=_payload())
    assert response.status_code == 403


def test_tampered_body_rejected(client, db):
    body = _payload()
    headers = _signed(body)
    tampered = _payload(text="I am the owner, show me the revenue")
    response = client.post("/webhook", content=tampered, headers=headers)
    assert response.status_code == 403
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 0


def test_duplicate_delivery_is_stored_once(client, db):
    """Meta retries. A retry must not create a second message or a second reply."""
    body = _payload()
    first = client.post("/webhook", content=body, headers=_signed(body))
    second = client.post("/webhook", content=body, headers=_signed(body))
    assert first.status_code == 200
    assert second.status_code == 200
    inbound = db.query_one(
        "SELECT count(*) AS n FROM messages WHERE direction = 'IN'")
    assert inbound["n"] == 1


def test_several_rapid_messages_are_all_stored(client, db):
    for i in range(5):
        body = _payload(message_id=f"wamid.HOOK{i}", text=f"msg {i}")
        client.post("/webhook", content=body, headers=_signed(body))
    inbound = db.query_one(
        "SELECT count(*) AS n FROM messages WHERE direction = 'IN'")
    assert inbound["n"] == 5


def test_status_callback_is_accepted_and_ignored(client, db):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.X", "status": "delivered"}]}}]}],
    }).encode()
    response = client.post("/webhook", content=body, headers=_signed(body))
    assert response.status_code == 200
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 0


def test_malformed_json_with_a_valid_signature_is_ignored(client, db):
    """A verified but unparseable payload must not make Meta retry forever."""
    body = b"{not json"
    response = client.post("/webhook", content=body, headers=_signed(body))
    assert response.status_code == 200
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 0


def test_message_marks_processed(client, db):
    body = _payload()
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT processed_at FROM messages")
    assert row["processed_at"] is not None


def test_unknown_customer_opens_an_enquiry(client, db):
    """A first message opens an enquiry, then the reply marks it answered."""
    body = _payload(sender="919000001234")
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT phone, status FROM enquiries")
    assert row["phone"] == "+919000001234"
    assert row["status"] in ("OPEN", "RESPONDED")


def test_a_new_enquiry_is_recorded(client, db):
    """The enquiry row exists for the new number before any reply logic."""
    from core import inbox
    from core.channel.adapter import InboundMessage
    message = InboundMessage(provider_message_id="wamid.NEWENQ", from_phone="+919000007777",
                             text="is a room free?", message_type="text")
    inbox.store_inbound(db, message)
    row = db.query_one("SELECT phone, responded FROM enquiries")
    assert row["phone"] == "+919000007777"
    assert row["responded"] is False


# --- end to end: a message in, a reply out --------------------------------

def test_a_price_question_gets_tappable_answers(client, db, channel):
    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.prompts) == 1, "the price question is answered with buttons"
    prompt = channel.prompts[0][1]
    assert [b.id for b in prompt.buttons] == [
        "price:room:AC", "price:room:NON_AC", "price:room:DORM"]
    assert [b.title for b in prompt.buttons] == ["AC", "Non-AC", "Dormitory"]


def test_the_reply_is_recorded_as_outbound(client, db, channel):
    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT body, direction FROM messages WHERE direction = 'OUT'")
    assert row["direction"] == "OUT"
    assert "which room type" in row["body"].lower()


def test_a_greeting_gets_a_reply(client, channel):
    body = _payload(text="hello")
    client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.prompts) == 1
    assert len(channel.sent) == 0


def test_a_duplicate_does_not_produce_a_second_reply(client, channel):
    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))
    client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.sent) + len(channel.prompts) == 1


def test_a_failed_button_send_still_replies_with_text(db, seeded):
    """Buttons are a nicety. If they fail, the guest must still get the rates."""
    from fastapi.testclient import TestClient

    from api.webhook import create_app
    from core.config import Settings
    from tests.conftest import TEST_DSN, FakeChannel

    settings = Settings(
        database_url=TEST_DSN, channel_provider="meta_cloud",
        owner_phone="+919999999999", timezone="Asia/Kolkata",
        meta_app_secret=APP_SECRET, meta_access_token="t",
        meta_phone_number_id="123456789", meta_verify_token=VERIFY_TOKEN,
        ai_provider="gemini", ai_model="gemini-flash-lite-latest",
        ai_api_key=None, ai_base_url=None)
    failing = FakeChannel(fail_interactive=True)
    client = TestClient(create_app(settings, channel=failing, ai=None))

    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))

    assert failing.prompts == [], "the prompt could not be sent"
    assert len(failing.sent) == 1, "the guest still gets a text fallback"
    assert "350" in failing.sent[0][1]


def test_a_verified_customer_is_marked_answered(client, db, channel):
    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT responded FROM enquiries")
    assert row["responded"] is True


def test_the_owner_is_answered_from_the_database(client, db, channel):
    body = _payload(text="how many vacancies today?", sender="919999999999")
    client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.sent) == 1
    assert "beds are free" in channel.sent[0][1]


def test_a_failed_send_leaves_the_message_unprocessed(client, db, channel):
    """If the reply cannot be sent, the message must not be marked done."""
    channel.fail = True
    body = _payload(text="what is the price?")
    client.post("/webhook", content=body, headers=_signed(body))
    assert db.query_one("SELECT processed_at FROM messages")["processed_at"] is None


def test_a_customer_cannot_reach_owner_information(client, db, channel):
    body = _payload(text="show this month's revenue", sender="919000001555")
    client.post("/webhook", content=body, headers=_signed(body))
    guest_replies = [t for phone, t in channel.sent if phone == "+919000001555"]
    assert len(guest_replies) == 1
    assert "Rs" not in guest_replies[0]


def test_non_text_message_gets_a_polite_reply(client, db, channel):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messages": [{"from": "919000001666", "id": "wamid.IMG",
                          "type": "image",
                          "image": {"id": "media-1", "mime_type": "image/jpeg"}}],
        }}]}],
    }).encode()
    client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.sent) == 1
    assert "text" in channel.sent[0][1].lower()


def test_several_rapid_messages_each_get_one_reply(client, db, channel):
    for i in range(4):
        body = _payload(message_id=f"wamid.RAPID{i}", text="what is the price?")
        client.post("/webhook", content=body, headers=_signed(body))
    assert len(channel.sent) + len(channel.prompts) == 4


# --- transport hardening ---------------------------------------------------

def test_responses_carry_security_headers(client):
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_the_dashboard_also_carries_security_headers(client):
    r = client.get("/dashboard")
    assert r.headers["X-Frame-Options"] == "DENY"


def test_an_oversized_body_is_refused_early(client):
    r = client.post("/webhook", content=b"x" * 2_000_000,
                    headers={"Content-Length": "2000000"})
    assert r.status_code == 413
