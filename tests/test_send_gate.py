"""The outbound send gate: two keys before a guest is messaged.

The failure these tests defend against is one-way and expensive: a development
box or a fresh clone holding real Meta credentials messaging real guests the
moment a webhook arrives. The gate must default closed, must be enforced in the
delivery layer rather than only in the dispatcher, and must never mark a blocked
message as sent.

The owner half is tested too: a booking request must still reach the owner while
guest replies are paused, but only while the master switch is on.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from api.webhook import create_app
from core import outbox, send_policy
from core.auto_reply import set_paused
from core.channel.adapter import InboundMessage, SendResult
from core.config import Settings
from core.db import Database
from core.dispatcher import dispatch
from core.outbound_worker import run_batch
from tests.conftest import (
    APP_SECRET,
    TEST_DSN,
    VERIFY_TOKEN,
    FakeChannel,
)

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
STAFF = "+919888888888"
GUEST = "+919000001111"


def _settings(*, send: bool, owner_notify: bool = True, auto_reply: bool = True,
              owner_phone: str | None = OWNER,
              staff: tuple[str, ...] = ()) -> Settings:
    return Settings(
        database_url=TEST_DSN, channel_provider="fake", owner_phone=owner_phone,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="1",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="fake-1", ai_api_key=None, ai_base_url=None,
        staff_phones=staff,
        send_enabled=send, owner_notify_enabled=owner_notify,
        auto_reply_enabled=auto_reply)


def _guest_text(db: Database, *, key: str = "gate:guest", phone: str = GUEST) -> str:
    return outbox.enqueue(db, recipient=phone, message_type="text",
                          payload={"text": "hello from the guest"},
                          idempotency_key=key)


def _owner_text(db: Database, *, key: str = "gate:owner", phone: str = OWNER) -> str:
    return outbox.enqueue(db, recipient=phone, message_type="text",
                          payload={"text": "a booking request"},
                          idempotency_key=key)


def _row(db: Database, outbound_id: str) -> dict:
    return db.query_one(
        "SELECT state, attempt_count FROM outbound_messages WHERE outbound_id = %s",
        (outbound_id,))


# --- the setting itself -----------------------------------------------------

def test_send_enabled_defaults_to_false(monkeypatch):
    """A fresh clone must not be able to message anyone."""
    monkeypatch.delenv("SEND_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().send_enabled is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ("", False), ("maybe", False), ("2", False), ("enabled", False),
])
def test_send_enabled_reads_words_and_fails_closed(monkeypatch, raw, expected):
    """A typo leaves the safer default in place, never silently arms sending."""
    monkeypatch.setenv("SEND_ENABLED", raw)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().send_enabled is expected


def test_owner_notify_defaults_to_true(monkeypatch):
    """The owner must hear about their business unless told otherwise."""
    monkeypatch.delenv("OWNER_NOTIFY_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert Settings.from_env().owner_notify_enabled is True


# --- the decision function --------------------------------------------------

def test_may_send_needs_both_keys_for_a_guest(db):
    settings = _settings(send=True)
    assert send_policy.may_send(db, settings, GUEST) is True
    set_paused(db, True, actor="test")
    assert send_policy.may_send(db, settings, GUEST) is False


def test_the_master_switch_alone_blocks_a_guest(db):
    """Resuming the pause is not enough; SEND_ENABLED is the second key."""
    set_paused(db, False, actor="test")
    assert send_policy.may_send(db, _settings(send=False), GUEST) is False


def test_resuming_the_pause_never_raises_the_master_switch(db):
    """The dashboard switch is the owner's; it cannot open the deployment gate."""
    set_paused(db, False, actor="test")
    settings = _settings(send=False)
    assert send_policy.env_allows(settings) is False
    assert send_policy.may_send(db, settings, GUEST) is False


def test_owner_is_governed_by_its_own_switch(db):
    assert send_policy.may_send(db, _settings(send=True), OWNER) is True
    assert send_policy.may_send(
        db, _settings(send=True, owner_notify=False), OWNER) is False
    # A paused guest conversation does not silence the owner.
    set_paused(db, True, actor="test")
    assert send_policy.may_send(db, _settings(send=True), OWNER) is True


def test_staff_are_treated_like_the_owner(db):
    settings = _settings(send=True, staff=(STAFF,))
    assert send_policy.audience_for(STAFF, settings) == send_policy.OWNER


# --- enforced in the delivery layer -----------------------------------------

def test_a_guest_message_is_held_when_the_master_switch_is_off(db, channel):
    outbound_id = _guest_text(db)
    result = run_batch(db, channel, _settings(send=False))
    assert result["claimed"] == 0, "a blocked row must not even be claimed"
    assert channel.attempts == [], "nothing reached the channel"
    assert _row(db, outbound_id)["state"] == "pending"


def test_a_guest_message_is_held_when_the_pause_is_on(db, channel):
    set_paused(db, True, actor="test")
    outbound_id = _guest_text(db)
    result = run_batch(db, channel, _settings(send=True))
    assert result["claimed"] == 0
    assert channel.attempts == []
    assert _row(db, outbound_id)["state"] == "pending"


def test_a_guest_message_goes_out_when_both_keys_are_on(db, channel):
    outbound_id = _guest_text(db)
    result = run_batch(db, channel, _settings(send=True))
    assert result["claimed"] == 1 and result["accepted"] == 1
    assert channel.texts == [(GUEST, "hello from the guest")]
    assert _row(db, outbound_id)["state"] == "accepted"


def test_the_gate_is_central_not_only_in_the_dispatcher(db, channel):
    """A row enqueued directly into the outbox is still held.

    This is the point of enforcing in the delivery layer: the dispatcher could
    be bypassed by a job, a retry or a future caller, and the message must still
    not go out.
    """
    _guest_text(db)
    run_batch(db, channel, _settings(send=False))
    assert channel.texts == []


def test_a_blocked_message_does_not_burn_attempts_or_loop(db, channel):
    """No retry storm: the row waits, unclaimed, with no attempt counted."""
    outbound_id = _guest_text(db)
    for _ in range(5):
        result = run_batch(db, channel, _settings(send=False))
        assert result["claimed"] == 0
    row = _row(db, outbound_id)
    assert row["state"] == "pending"
    assert row["attempt_count"] == 0
    assert channel.attempts == []


def test_a_held_message_goes_out_once_the_switches_are_on(db, channel):
    """The message is delayed, never lost."""
    outbound_id = _guest_text(db)
    run_batch(db, channel, _settings(send=False))
    assert channel.texts == []
    run_batch(db, channel, _settings(send=True))
    assert channel.texts == [(GUEST, "hello from the guest")]
    assert _row(db, outbound_id)["state"] == "accepted"


def test_release_claim_returns_a_row_to_pending_without_counting_it(db):
    outbound_id = _guest_text(db)
    item = outbox.claim_next(db, phone_scope="phone-id")
    assert item["attempt_count"] == 1
    assert outbox.release_claim(db, item) is True
    row = _row(db, outbound_id)
    assert row["state"] == "pending"
    assert row["attempt_count"] == 0
    # The send slot is freed, so the next eligible message can go at once.
    assert outbox.claim_next(db, phone_scope="phone-id") is not None


# --- the owner still hears about a booking while guests are paused ----------

def test_an_owner_notice_reaches_the_owner_while_guests_are_paused(db, channel):
    set_paused(db, True, actor="test")
    _owner_text(db)
    _guest_text(db, key="gate:guest2")
    run_batch(db, channel, _settings(send=True))
    assert channel.texts == [(OWNER, "a booking request")]
    assert all(phone != GUEST for phone, _ in channel.texts)


def test_owner_notices_can_be_switched_off(db, channel):
    _owner_text(db)
    run_batch(db, channel, _settings(send=True, owner_notify=False))
    assert channel.texts == []


def test_nothing_sends_at_all_when_the_master_switch_is_off(db, channel):
    _owner_text(db)
    _guest_text(db, key="gate:guest3")
    run_batch(db, channel, _settings(send=False))
    assert channel.texts == []


# --- the dispatcher does not even build a guest reply when disabled ---------

def test_dispatch_leaves_no_guest_row_when_sending_is_disabled(db):
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.GATE1", from_phone=GUEST,
                             text="hello", message_type="text")
    from core import inbox
    stored = inbox.store_inbound(db, message)
    dispatch(db, channel, _settings(send=False), message, ai=None,
             message_id=stored.message_id)
    assert db.query_one(
        "SELECT count(*) n FROM outbound_messages")["n"] == 0, \
        "a disabled deployment must not even queue a guest reply"
    # The inbound is still handled and recorded, never left looking stuck.
    assert db.query_one("SELECT processed_at FROM messages WHERE message_id = %s",
                        (stored.message_id,))["processed_at"] is not None


def test_dispatch_still_queues_the_owner_while_sending_is_disabled(db):
    """The master switch is off, so the owner notice is queued but not sent."""
    channel = FakeChannel()
    message = InboundMessage(provider_message_id="wamid.GATE2", from_phone=OWNER,
                             text="how many vacancies today", message_type="text")
    from core import inbox
    stored = inbox.store_inbound(db, message)
    dispatch(db, channel, _settings(send=False), message, ai=None,
             message_id=stored.message_id)
    assert channel.attempts == [], "the owner is not messaged while the gate is off"


# --- the health line tells the truth ----------------------------------------

def test_health_reports_the_send_gate(db):
    body = _client(_settings(send=False)).get("/health").json()
    assert "SEND_ENABLED" in body["sending"]


def test_health_says_sending_is_on_when_armed(db):
    body = _client(_settings(send=True)).get("/health").json()
    assert body["sending"].startswith("sending on")


def test_health_says_guests_are_blocked_when_paused(db):
    set_paused(db, True, actor="test")
    body = _client(_settings(send=True)).get("/health").json()
    assert "paused" in body["sending"]


# --- the dashboard resume is honest about the master switch -----------------

def _client(settings) -> TestClient:
    return TestClient(create_app(settings, channel=FakeChannel(), ai=None))


def _login(client) -> None:
    client.post("/dashboard/login", data={"password": "test-dashboard-password"})


def test_dashboard_resume_does_not_promise_a_send_it_cannot_make(db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-dashboard-password")
    client = _client(_settings(send=False))
    _login(client)
    response = client.post("/dashboard/auto-reply", data={"action": "resume"},
                           follow_redirects=False)
    assert response.status_code == 303
    notice = response.headers["location"]
    assert "SEND_ENABLED" in notice, \
        "the owner must be told the deployment is not allowed to send"
