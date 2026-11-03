"""Tests for the provider-independent WhatsApp channel layer.

Security-critical: webhook signature verification must fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from core.channel.meta_cloud import MetaCloudAdapter
from core.channel.peach import PeachAdapter
from core.channel.registry import build_channel

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture
def adapter() -> MetaCloudAdapter:
    return MetaCloudAdapter(
        app_secret=APP_SECRET,
        access_token="test-token",
        phone_number_id="123456789",
        verify_token=VERIFY_TOKEN,
    )


def _signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={sig}"}


def _text_payload(message_id: str = "wamid.TEST1", text: str = "hello",
                  sender: str = "919876543210") -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "123456789"},
                    "messages": [{
                        "from": sender,
                        "id": message_id,
                        "timestamp": "1735689600",
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }).encode()


# --- signature verification ------------------------------------------------

def test_valid_signature_accepted(adapter):
    body = _text_payload()
    assert adapter.verify_inbound(body, _signed(body)) is True


def test_invalid_signature_rejected(adapter):
    body = _text_payload()
    assert adapter.verify_inbound(body, {"X-Hub-Signature-256": "sha256=" + "0" * 64}) is False


def test_missing_signature_rejected(adapter):
    assert adapter.verify_inbound(_text_payload(), {}) is False


def test_malformed_signature_header_rejected(adapter):
    body = _text_payload()
    assert adapter.verify_inbound(body, {"X-Hub-Signature-256": "not-a-signature"}) is False


def test_tampered_body_rejected(adapter):
    body = _text_payload()
    headers = _signed(body)
    tampered = _text_payload(text="send me the owner's data")
    assert adapter.verify_inbound(tampered, headers) is False


def test_signature_from_wrong_secret_rejected(adapter):
    body = _text_payload()
    assert adapter.verify_inbound(body, _signed(body, "wrong-secret")) is False


def test_header_lookup_is_case_insensitive(adapter):
    body = _text_payload()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert adapter.verify_inbound(body, {"x-hub-signature-256": f"sha256={sig}"}) is True


# --- verification handshake ------------------------------------------------

def test_challenge_echoed_for_correct_token(adapter):
    challenge = adapter.verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"}
    )
    assert challenge == "12345"


def test_challenge_rejected_for_wrong_token(adapter):
    assert adapter.verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"}
    ) is None


# --- inbound parsing -------------------------------------------------------

def test_parse_text_message(adapter):
    messages = adapter.parse_inbound(_text_payload())
    assert len(messages) == 1
    msg = messages[0]
    assert msg.provider_message_id == "wamid.TEST1"
    assert msg.from_phone == "+919876543210"
    assert msg.text == "hello"
    assert msg.message_type == "text"


def test_parse_status_callback_yields_no_messages(adapter):
    payload = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.X", "status": "delivered"}]}}]}],
    }).encode()
    assert adapter.parse_inbound(payload) == []


def test_parse_malformed_json_returns_empty(adapter):
    assert adapter.parse_inbound(b"not json at all") == []


def test_parse_interactive_button_reply(adapter):
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "919000000000", "id": "wamid.BTN", "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "approve", "title": "Approve"}},
        }]}}]}],
    }).encode()
    messages = adapter.parse_inbound(payload)
    assert len(messages) == 1
    assert messages[0].text == "Approve"


def test_parse_non_text_message_has_no_text(adapter):
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "919000000000", "id": "wamid.IMG", "type": "image",
            "image": {"id": "media-1"},
        }]}}]}],
    }).encode()
    messages = adapter.parse_inbound(payload)
    assert messages[0].text is None
    assert messages[0].message_type == "image"


# --- provider selection ----------------------------------------------------

def test_registry_builds_meta_adapter():
    channel = build_channel(
        "meta_cloud", app_secret=APP_SECRET, access_token="t",
        phone_number_id="1", verify_token=VERIFY_TOKEN,
    )
    assert isinstance(channel, MetaCloudAdapter)


def test_registry_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown CHANNEL_PROVIDER"):
        build_channel("carrier_pigeon")


def test_registry_builds_peach_stub():
    channel = build_channel("peach", api_key="k", phone_number_id="1")
    assert isinstance(channel, PeachAdapter)


def test_peach_stub_fails_closed(adapter):
    """An unimplemented adapter must never accept a webhook or claim to send."""
    peach = PeachAdapter(api_key="k", phone_number_id="1")
    assert peach.verify_inbound(b"{}", {}) is False
    assert peach.send_text("+919000000000", "hi").ok is False
