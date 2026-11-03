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

from api.webhook import create_app
from core.config import Settings
from core.db import Database
from tests.conftest import TEST_DSN

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture
def client(db) -> TestClient:
    settings = Settings(
        database_url=TEST_DSN,
        channel_provider="meta_cloud",
        owner_phone="+919999999999",
        timezone="Asia/Kolkata",
        meta_app_secret=APP_SECRET,
        meta_access_token="test-token",
        meta_phone_number_id="123456789",
        meta_verify_token=VERIFY_TOKEN,
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
        ai_api_key=None,
        ai_base_url=None,
    )
    return TestClient(create_app(settings))


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
    row = db.query_one("SELECT wa_message_id, direction FROM messages")
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
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_several_rapid_messages_are_all_stored(client, db):
    for i in range(5):
        body = _payload(message_id=f"wamid.HOOK{i}", text=f"msg {i}")
        client.post("/webhook", content=body, headers=_signed(body))
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 5


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
    body = _payload(sender="919000001234")
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT phone, status FROM enquiries")
    assert row["phone"] == "+919000001234"
    assert row["status"] == "OPEN"
