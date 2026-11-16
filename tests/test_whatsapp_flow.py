"""WhatsApp Flow (native calendar) wiring, end to end.

The Flow replaces only the check-in / check-out question: a published Flow is
sent as its own single message, the guest's submission arrives back on the
webhook as an ``nfm_reply``, and the booking carries on from there with one
interactive reply per inbound message.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from core import booking_flow
from core.channel.adapter import Flow, flow_date_range, flow_response_data
from core.config import Settings
from tests.conftest import (
    APP_SECRET,
    TEST_DSN,
    VERIFY_TOKEN,
    FakeChannel,
    channel,
    client,
    flush_outbox,  # noqa: F401
)

GUEST = "+919000001000"
FLOW_ID = "1234567890"
SCREEN = "BOOKING_DATES"


# --- parsing helpers -------------------------------------------------------

def test_flow_date_range_reads_a_pair_of_iso_dates():
    assert flow_date_range({"check_in": "2026-12-01", "check_out": "2026-12-03"}) \
        == ("2026-12-01", "2026-12-03")


def test_flow_date_range_accepts_alternate_field_names():
    assert flow_date_range({"start_date": "2026-12-01", "end_date": "2026-12-03"}) \
        == ("2026-12-01", "2026-12-03")
    assert flow_date_range({"from": "2026-12-01", "to": "2026-12-03"}) \
        == ("2026-12-01", "2026-12-03")


def test_flow_date_range_accepts_a_single_range_string():
    assert flow_date_range({"date_range": "2026-12-01/2026-12-03"}) \
        == ("2026-12-01", "2026-12-03")


def test_flow_date_range_ignores_an_unusable_value():
    assert flow_date_range({"check_in": "not a date", "check_out": "2026-12-03"}) \
        == (None, "2026-12-03")
    assert flow_date_range({}) == (None, None)


def test_flow_response_data_parses_the_json_string_meta_sends():
    raw = {"interactive": {"type": "nfm_reply", "nfm_reply": {
        "response_json": json.dumps({"check_in": "2026-12-01",
                                     "check_out": "2026-12-03"})}}}
    assert flow_response_data(raw) == {"check_in": "2026-12-01",
                                       "check_out": "2026-12-03"}


def test_flow_response_data_is_empty_for_a_malformed_payload():
    assert flow_response_data({}) == {}
    assert flow_response_data({"interactive": {"nfm_reply": {
        "response_json": "{not json"}}}) == {}


# --- building the Flow -----------------------------------------------------

def test_a_published_flow_replaces_the_date_buttons(db):
    result = booking_flow.start(db, phone=GUEST, flow_id=FLOW_ID, flow_screen=SCREEN)
    assert isinstance(result.flow, Flow)
    assert result.flow.flow_id == FLOW_ID
    assert result.flow.screen == SCREEN
    # The Flow is the whole message: no separate prompt travels with it.
    assert result.prompt is None
    assert result.reply == result.flow.body


def test_without_a_published_flow_the_date_buttons_remain(db):
    result = booking_flow.start(db, phone=GUEST)
    assert result.flow is None
    assert result.prompt is not None
    assert [b.id for b in result.prompt.rows] == [
        "bk:date:today", "bk:date:tomorrow", "bk:date:dayafter"]


def test_a_typed_date_does_not_ask_with_the_flow(db):
    """The guest already gave the check-in, so the Flow is not offered again."""
    result = booking_flow.start(db, phone=GUEST, text="book from 2026-12-01",
                                flow_id=FLOW_ID, flow_screen=SCREEN)
    assert result.flow is None


# --- folding the submission back into the booking --------------------------

def test_apply_flow_dates_sets_the_range_and_asks_the_next_question(db):
    booking_flow.start(db, phone=GUEST, flow_id=FLOW_ID, flow_screen=SCREEN)
    result = booking_flow.apply_flow_dates(db, phone=GUEST,
                                           check_in="2026-12-01",
                                           check_out="2026-12-03")
    state = booking_flow._load(db, GUEST)
    assert state["check_in"] == "2026-12-01"
    assert state["check_out"] == "2026-12-03"
    assert result.prompt is not None


def test_apply_flow_dates_refuses_a_checkout_not_after_checkin(db):
    booking_flow.start(db, phone=GUEST, flow_id=FLOW_ID, flow_screen=SCREEN)
    result = booking_flow.apply_flow_dates(db, phone=GUEST,
                                           check_in="2026-12-03",
                                           check_out="2026-12-01")
    assert "check-out must be after" in result.reply
    assert result.asks_dates is True
    assert booking_flow._load(db, GUEST).get("check_in") is None


# --- webhook: a Flow submission in, one reply out --------------------------

def _flow_payload(message_id: str, response: dict, sender: str = "919000001000") -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{
                "from": sender, "id": message_id, "timestamp": "1735689600",
                "type": "interactive",
                "interactive": {"type": "nfm_reply", "nfm_reply": {
                    "name": "flow", "body": "Sent",
                    "response_json": json.dumps(response)}},
            }],
        }}]}],
    }).encode()


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={sig}"}


def test_a_flow_submission_is_stored_and_answered_once(client, db, channel):
    body = _flow_payload("wamid.FLOW1", {"check_in": "2026-12-01",
                                         "check_out": "2026-12-03"})
    client.post("/webhook", content=body, headers=_signed(body))
    flush_outbox(client)

    assert db.query_one("SELECT count(*) AS n FROM messages WHERE direction = 'IN'")["n"] == 1
    # The submission is one inbound message, so it gets exactly one reply.
    assert len(channel.sent) == 1
    assert len(channel.prompts) == 1
    assert db.query_one("SELECT count(*) AS n FROM messages WHERE direction = 'OUT'")["n"] == 1
    state = booking_flow._load(db, GUEST)
    assert state["check_in"] == "2026-12-01"
    assert state["check_out"] == "2026-12-03"


def test_a_flow_submission_is_never_answered_with_plain_text(client, db, channel):
    body = _flow_payload("wamid.FLOW2", {"check_in": "2026-12-01",
                                         "check_out": "2026-12-03"})
    client.post("/webhook", content=body, headers=_signed(body))
    flush_outbox(client)
    # Interactive-only: no bare text message is sent alongside the interactive.
    assert channel.attempts.count("text") == 0


def test_a_cancelled_flow_gets_one_interactive_reply(client, db, channel):
    """A Flow the guest dismissed carries no range; the dates are asked again."""
    body = _flow_payload("wamid.FLOWCANCEL", {})
    client.post("/webhook", content=body, headers=_signed(body))
    flush_outbox(client)
    guest_replies = [p for phone, p in channel.prompts if phone == GUEST]
    assert len(guest_replies) == 1, "a cancelled Flow still gets one reply"
    assert channel.attempts.count("text") == 0


def test_a_malformed_flow_submission_gets_one_interactive_reply(client, db, channel):
    """A response_json that is not usable JSON is refused, not answered twice."""
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{
                "from": "919000001000", "id": "wamid.FLOWBAD",
                "timestamp": "1735689600", "type": "interactive",
                "interactive": {"type": "nfm_reply", "nfm_reply": {
                    "name": "flow", "body": "Sent",
                    "response_json": "{not json"}},
            }],
        }}]}],
    }).encode()
    client.post("/webhook", content=body, headers=_signed(body))
    flush_outbox(client)
    assert len([p for phone, p in channel.prompts if phone == GUEST]) == 1
    assert channel.attempts.count("text") == 0


def test_a_duplicate_flow_submission_is_answered_once(client, db, channel):
    """Meta retries a submission; the guest must not get two replies."""
    body = _flow_payload("wamid.FLOWDUPE", {"check_in": "2026-12-01",
                                            "check_out": "2026-12-03"})
    client.post("/webhook", content=body, headers=_signed(body))
    client.post("/webhook", content=body, headers=_signed(body))
    flush_outbox(client)
    assert len([p for phone, p in channel.prompts if phone == GUEST]) == 1
    assert channel.attempts.count("text") == 0


# --- webhook: a published Flow is sent as its own single message -----------

def _flow_client(channel, tmp_path):
    from api.webhook import create_app

    settings = Settings(
        database_url=TEST_DSN, channel_provider="meta_cloud",
        owner_phone="+919999999999", timezone="Asia/Kolkata",
        meta_app_secret=APP_SECRET, meta_access_token="t",
        meta_phone_number_id="123456789", meta_verify_token=VERIFY_TOKEN,
        ai_provider="gemini", ai_model="gemini-flash-lite-latest",
        ai_api_key=None, ai_base_url=None,
        payment_proof_dir=str(tmp_path / "proofs"),
        whatsapp_flow_id=FLOW_ID, whatsapp_flow_calendar_screen=SCREEN)
    return TestClient(create_app(settings, channel=channel, ai=None))


def test_a_booking_request_sends_the_flow_as_one_message(db, channel, tmp_path):
    app_client = _flow_client(channel, tmp_path)
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": "919000001000", "id": "wamid.BOOKFLOW",
                          "timestamp": "1735689600", "type": "text",
                          "text": {"body": "I want to book a room"}}],
        }}]}],
    }).encode()
    app_client.post("/webhook", content=body, headers=_signed(body))

    from core.outbound_worker import run_batch
    run_batch(app_client.app.state.db, channel, app_client.app.state.settings)

    assert len(channel.flows) == 1, "the calendar is sent as its own message"
    assert channel.flows[0][1].flow_id == FLOW_ID
    # No text and no interactive prompt accompany the Flow.
    assert channel.prompts == []
    assert channel.attempts.count("text") == 0
