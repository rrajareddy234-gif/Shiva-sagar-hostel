"""The temporary auto-reply pause.

The owner sometimes needs the system to go quiet -- a holiday, a problem, or a
run of junk messages -- without taking the webhook down. Meta must keep getting
its 200, every message must still be recorded, and bookings, availability and
payments must keep working. Only the outbound WhatsApp reply is held back.

These tests defend exactly that boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.webhook import create_app
from core import availability, booking, inbox
from core.channel.adapter import InboundMessage
from core.config import Settings
from core.dispatcher import dispatch
from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN, FakeChannel

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"


def _payload(message_id="wamid.PAUSE1", text="hello",
             sender="919000001000") -> bytes:
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


def _settings(*, auto_reply: bool) -> Settings:
    return Settings(
        database_url=TEST_DSN, channel_provider="meta_cloud", owner_phone=OWNER,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="123456789",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="fake-1", ai_api_key=None, ai_base_url=None,
        auto_reply_enabled=auto_reply)


def _client(settings, channel) -> TestClient:
    return TestClient(create_app(settings, channel=channel, ai=None))


# --- the setting itself -----------------------------------------------------

def test_auto_reply_defaults_to_enabled(monkeypatch):
    """With nothing set, replies happen as before. A pause is opt-in."""
    monkeypatch.delenv("AUTO_REPLY_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().auto_reply_enabled is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
])
def test_auto_reply_reads_common_truthy_and_falsy_words(monkeypatch, raw, expected):
    monkeypatch.setenv("AUTO_REPLY_ENABLED", raw)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().auto_reply_enabled is expected


# --- paused: nothing goes out, but everything is recorded -------------------

def test_when_paused_no_reply_is_sent(db):
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.P1", from_phone="+919000001111",
                             text="hello", message_type="text")
    stored = inbox.store_inbound(db, message)

    dispatch(db, channel, _settings(auto_reply=False), message, ai=None,
             message_id=stored.message_id)

    assert channel.sent == [], "no text should be sent while paused"
    assert channel.prompts == [], "no buttons should be sent while paused"
    assert channel.documents == []


def test_when_paused_the_message_is_still_logged(db):
    """Receiving and logging continue; only the reply is held back."""
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.P2", from_phone="+919000001112",
                             text="do you have a room?", message_type="text")
    stored = inbox.store_inbound(db, message)

    dispatch(db, channel, _settings(auto_reply=False), message, ai=None,
             message_id=stored.message_id)

    row = db.query_one("SELECT body, direction FROM messages WHERE direction = 'IN'")
    assert row is not None, "the inbound message must still be stored"
    assert row["body"] == "do you have a room?"
    assert db.query_one(
        "SELECT processed_at FROM messages WHERE message_id = %s",
        (stored.message_id,))["processed_at"] is not None, \
        "a paused message is handled, not left to look stuck"


def test_when_paused_no_outbound_row_is_written(db):
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.P3", from_phone="+919000001113",
                             text="hello", message_type="text")
    stored = inbox.store_inbound(db, message)

    dispatch(db, channel, _settings(auto_reply=False), message, ai=None,
             message_id=stored.message_id)

    assert db.query_one(
        "SELECT count(*) n FROM messages WHERE direction = 'OUT'")["n"] == 0


def _tap(db, channel, settings, *, bid=None, text="", phone="+919000001200", n=1):
    message = InboundMessage(provider_message_id=f"wamid.PAUSE-{n}", from_phone=phone,
                             text=text, message_type="text", button_id=bid)
    stored = inbox.store_inbound(db, message)
    dispatch(db, channel, settings, message, ai=None, message_id=stored.message_id)


def test_when_paused_a_booking_still_reaches_the_owner(db):
    """A pause must not swallow a booking request. The owner still gets it.

    The whole flow is driven through dispatch while paused, which is the real
    path a tap takes: the booking is created, the owner is alerted, and the
    guest gets nothing back.
    """
    channel = FakeChannel()
    settings = _settings(auto_reply=False)
    guest = "+919000001200"

    steps = ["menu:book", "bk:date:tomorrow", "bk:nights:1", "bk:guests:1",
             "bk:room:AC", "bk:food:0", "bk:terms:FULL"]
    for i, bid in enumerate(steps, start=1):
        _tap(db, channel, settings, bid=bid, phone=guest, n=i)
    _tap(db, channel, settings, text="Pause Guest", phone=guest, n=20)
    _tap(db, channel, settings, bid="bk:submit", phone=guest, n=21)

    assert booking.pending_requests(db), "the request is waiting for the owner"

    # The owner alert is sent as an Approve/Reject prompt, so it lands in
    # prompts rather than plain sends. Either counts as "the owner was told".
    owner_told = ([t for phone, t in channel.sent if phone == OWNER]
                  + [p.body for phone, p in channel.prompts if phone == OWNER])
    assert owner_told, "the owner must still be told about a booking while paused"

    guest_reached = ([t for phone, t in channel.sent if phone == guest]
                     + [p.body for phone, p in channel.prompts if phone == guest])
    assert guest_reached == [], "the guest is not replied to while paused"


# --- paused: the webhook keeps answering ------------------------------------

def test_when_paused_the_webhook_still_returns_200(db):
    channel = FakeChannel()
    client = _client(_settings(auto_reply=False), channel)
    body = _payload()
    response = client.post("/webhook", content=body, headers=_signed(body))
    assert response.status_code == 200, "Meta must keep getting its 200"
    assert channel.sent == []


def test_when_paused_the_verification_handshake_still_works(db):
    """Meta's GET handshake is unaffected by a pause."""
    client = _client(_settings(auto_reply=False), FakeChannel())
    response = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "challenge-123"})
    assert response.status_code == 200
    assert response.text == "challenge-123"


def test_when_paused_an_inbound_webhook_message_is_still_stored(db):
    client = _client(_settings(auto_reply=False), FakeChannel())
    body = _payload(message_id="wamid.PAUSE-STORE", text="room free?")
    client.post("/webhook", content=body, headers=_signed(body))
    row = db.query_one("SELECT body FROM messages WHERE direction = 'IN'")
    assert row is not None and row["body"] == "room free?"


# --- paused: the rest of the system is untouched ----------------------------

def test_when_paused_availability_still_reads(db):
    now = datetime.now(UTC)
    beds = availability.available_beds(db, now, now + timedelta(days=1))
    assert beds, "availability must keep working while paused"


def test_when_paused_bookings_can_still_be_created_and_approved(db):
    """The core business does not depend on outbound replies."""
    start = datetime.now(UTC)
    request = booking.create_request(
        db, phone="+919000001300", full_name="Paused Guest", gender=None,
        bed_id="1NA-B1", check_in_at=start,
        check_out_at=start + timedelta(days=1),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    row = db.query_one("SELECT status FROM bookings WHERE booking_id = %s",
                       (request.booking_id,))
    assert row["status"] == "CONFIRMED"


# --- resumed: replies come back ---------------------------------------------

def test_when_resumed_replies_are_sent_again(db):
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.R1", from_phone="+919000001400",
                             text="hello", message_type="text")
    stored = inbox.store_inbound(db, message)

    dispatch(db, channel, _settings(auto_reply=True), message, ai=None,
             message_id=stored.message_id)

    assert channel.prompts or channel.sent, "replies resume when enabled again"


# --- the health endpoint says which mode it is in ---------------------------

def test_health_reports_auto_reply_state(db):
    on = _client(_settings(auto_reply=True), FakeChannel()).get("/health").json()
    off = _client(_settings(auto_reply=False), FakeChannel()).get("/health").json()
    assert on["auto_reply"] == "enabled"
    assert off["auto_reply"] == "paused"
