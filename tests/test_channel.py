"""Tests for the provider-independent WhatsApp channel layer.

Security-critical: webhook signature verification must fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from core.channel.adapter import Button, DeliveryStatus, Flow, Prompt, SendResult
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



@pytest.mark.parametrize("status", ["sent", "delivered", "read", "failed"])
def test_parse_delivery_statuses(adapter, status):
    payload = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "statuses": [{
                "id": "wamid.STATUS1",
                "status": status,
                "timestamp": "1735689600",
                "recipient_id": "919000001000",
                "errors": [{"code": 131047}] if status == "failed" else [],
            }]
        }}]}],
    }).encode()

    statuses = adapter.parse_statuses(payload)

    assert len(statuses) == 1
    assert isinstance(statuses[0], DeliveryStatus)
    assert statuses[0].provider_message_id == "wamid.STATUS1"
    assert statuses[0].status == status
    assert statuses[0].timestamp == 1735689600
    assert statuses[0].recipient_id == "919000001000"


def test_parse_unknown_delivery_status_is_ignored(adapter):
    payload = json.dumps({
        "entry": [{"changes": [{"value": {
            "statuses": [{"id": "wamid.UNKNOWN", "status": "unknown"}]
        }}]}],
    }).encode()

    assert adapter.parse_statuses(payload) == []


def test_parse_malformed_status_json_returns_empty(adapter):
    assert adapter.parse_statuses(b"not json") == []


@pytest.mark.parametrize("payload", [
    [],
    {"entry": ["bad-entry"]},
    {"entry": [{"changes": ["bad-change"]}]},
    {"entry": [{"changes": [{"value": {"statuses": ["bad-item"]}}]}]},
])
def test_parse_malformed_status_structures_are_ignored(adapter, payload):
    assert adapter.parse_statuses(json.dumps(payload).encode()) == []


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
    assert peach.send_interactive(
        "+919000000000", Prompt(body="hi", buttons=(Button("a", "A"),))).ok is False


# --- interactive messages --------------------------------------------------

def _sent_body(adapter, monkeypatch, prompt):
    """Call send_interactive and return the JSON body it would have posted."""
    captured = {}

    def fake_post(self, body):
        captured.update(body)
        return SendResult(ok=True, provider_message_id="wamid.SENT")

    monkeypatch.setattr(MetaCloudAdapter, "_post", fake_post)
    result = adapter.send_interactive("+919876543210", prompt)
    assert result.ok
    return captured


def test_send_interactive_sends_reply_buttons(adapter, monkeypatch):
    prompt = Prompt(body="Which room type?",
                    buttons=(Button("price:room:AC", "AC"),
                             Button("price:room:NON_AC", "Non-AC")))
    body = _sent_body(adapter, monkeypatch, prompt)

    assert body["type"] == "interactive"
    assert body["to"] == "919876543210"
    interactive = body["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"]["text"] == "Which room type?"
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "price:room:AC", "title": "AC"}},
        {"type": "reply", "reply": {"id": "price:room:NON_AC", "title": "Non-AC"}},
    ]


def test_send_interactive_sends_a_list_when_more_than_three(adapter, monkeypatch):
    rows = tuple(Button(f"price:room:R{i}", f"Room {i}") for i in range(4))
    prompt = Prompt(body="Which room?", rows=rows, button_text="Choose a room")
    body = _sent_body(adapter, monkeypatch, prompt)

    interactive = body["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"]["button"] == "Choose a room"
    assert len(interactive["action"]["sections"][0]["rows"]) == 4


def test_send_interactive_truncates_over_long_titles(adapter, monkeypatch):
    """Meta rejects over-long labels, so they are clipped rather than failing."""
    prompt = Prompt(body="?", buttons=(Button("a", "x" * 40),))
    body = _sent_body(adapter, monkeypatch, prompt)
    assert len(body["interactive"]["action"]["buttons"][0]["reply"]["title"]) == 20


def test_parse_interactive_reply_keeps_the_button_id(adapter):
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "919000000000", "id": "wamid.BTN2", "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "price:room:AC", "title": "AC"}},
        }]}}]}],
    }).encode()
    message = adapter.parse_inbound(payload)[0]
    assert message.button_id == "price:room:AC"
    assert message.text == "AC"


def test_parse_interactive_list_reply_keeps_the_row_id(adapter):
    payload = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "919000000000", "id": "wamid.LST", "type": "interactive",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": "price:room:DORM", "title": "Dormitory"}},
        }]}}]}],
    }).encode()
    message = adapter.parse_inbound(payload)[0]
    assert message.button_id == "price:room:DORM"
    assert message.text == "Dormitory"


def test_prompt_validity_matches_the_meta_limits():
    assert Prompt(body="?", buttons=(Button("a", "A"),)).is_valid()
    assert Prompt(body="?", buttons=tuple(Button(f"b{i}", "B") for i in range(4))).is_valid() is False
    assert Prompt(body="?", rows=tuple(Button(f"r{i}", "R") for i in range(10))).is_valid()
    assert Prompt(body="?", rows=tuple(Button(f"r{i}", "R") for i in range(11))).is_valid() is False
    assert Prompt(body="?").is_valid() is False


# --- flows -----------------------------------------------------------------

def _sent_flow_body(adapter, monkeypatch, flow):
    captured = {}

    def fake_post(self, body):
        captured.update(body)
        return SendResult(ok=True, provider_message_id="wamid.SENT")

    monkeypatch.setattr(MetaCloudAdapter, "_post", fake_post)
    result = adapter.send_flow("+919876543210", flow)
    assert result.ok
    return captured


def test_send_flow_navigates_to_the_named_screen(adapter, monkeypatch):
    flow = Flow(flow_id="123", screen="BOOKING_DATES", cta="Pick dates",
                body="Choose your dates", data={"check_in": "2026-12-01"},
                flow_token="bk-token")
    body = _sent_flow_body(adapter, monkeypatch, flow)

    assert body["type"] == "interactive"
    interactive = body["interactive"]
    assert interactive["type"] == "flow"
    params = interactive["action"]["parameters"]
    assert params["flow_message_version"] == "3"
    assert params["flow_id"] == "123"
    assert params["flow_action"] == "navigate"
    assert params["flow_action_payload"]["screen"] == "BOOKING_DATES"
    assert params["flow_action_payload"]["data"] == {"check_in": "2026-12-01"}
    assert params["flow_token"] == "bk-token"
    assert interactive["body"]["text"] == "Choose your dates"


def test_send_flow_honours_data_exchange_mode(adapter, monkeypatch):
    flow = Flow(flow_id="123", screen="S", cta="Open", mode="data_exchange")
    body = _sent_flow_body(adapter, monkeypatch, flow)
    params = body["interactive"]["action"]["parameters"]
    assert params["flow_action"] == "data_exchange"
    assert "flow_action_payload" not in params


def test_an_invalid_flow_is_never_sent(adapter, monkeypatch):
    flow = Flow(flow_id="", screen="", cta="Open")
    result = adapter.send_flow("+919876543210", flow)
    assert result.ok is False


# --- documents -------------------------------------------------------------

def test_send_document_uploads_then_sends(adapter, monkeypatch):
    """Meta needs two calls: upload the bytes, then send the media id."""
    uploaded = {}

    def fake_upload(self, filename, content, media_type):
        uploaded.update(filename=filename, content=content, media_type=media_type)
        return "MEDIA_ID_1"

    def fake_post(self, body):
        uploaded["body"] = body
        return SendResult(ok=True, provider_message_id="wamid.DOC")

    monkeypatch.setattr(MetaCloudAdapter, "_upload", fake_upload)
    monkeypatch.setattr(MetaCloudAdapter, "_post", fake_post)
    result = adapter.send_document("+919876543210", "customers-1.csv",
                                   b"name\nRamesh\n", caption="Here you go")
    assert result.ok
    assert uploaded["filename"] == "customers-1.csv"
    assert uploaded["content"] == b"name\nRamesh\n"
    body = uploaded["body"]
    assert body["type"] == "document"
    assert body["to"] == "919876543210"
    assert body["document"] == {"id": "MEDIA_ID_1", "filename": "customers-1.csv",
                                "caption": "Here you go"}


def test_a_failed_upload_does_not_send_an_empty_message(adapter, monkeypatch):
    posted = []
    monkeypatch.setattr(MetaCloudAdapter, "_upload",
                        lambda self, f, c, m: None)
    monkeypatch.setattr(MetaCloudAdapter, "_post",
                        lambda self, body: posted.append(body))
    result = adapter.send_document("+919876543210", "x.csv", b"a\n")
    assert result.ok is False
    assert posted == []


def test_a_channel_that_cannot_send_files_fails_closed():
    from core.channel.peach import PeachAdapter

    peach = PeachAdapter(api_key="k", phone_number_id="1")
    result = peach.send_document("+919876543210", "x.csv", b"a\n")
    assert result.ok is False


# --- a throttled send must not be retried -----------------------------------

def _http_error(code, body):
    import io
    import urllib.error
    return urllib.error.HTTPError("https://graph.facebook.com", code,
                                  "err", {}, io.BytesIO(body.encode()))


def _failing_urlopen(code, body):
    def opener(req, timeout=None):
        raise _http_error(code, body)
    return opener


def test_a_rate_limited_send_is_marked_not_retryable(adapter, monkeypatch):
    """Meta's 131056 means this pair is throttled; a retry deepens the block."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen(
        400, '{"error":{"code":131056,"message":"pair rate limit hit"}}'))

    result = adapter.send_text("+919876543210", "hello")

    assert result.ok is False
    assert result.retryable is False


def test_phone_number_rate_limit_is_marked_not_retryable(adapter, monkeypatch):
    """Meta's 130429 means the phone number throughput limit was hit."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen(
        400, '{"error":{"code":130429,"message":"phone number rate limit hit"}}'))

    result = adapter.send_text("+919876543210", "hello")

    assert result.ok is False
    assert result.retryable is False


def test_an_ordinary_failure_stays_retryable(adapter, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        _failing_urlopen(500, '{"error":{"code":2}}'))

    result = adapter.send_text("+919876543210", "hello")

    assert result.ok is False
    assert result.retryable is True


def test_a_throttled_reply_is_attempted_once(db, channel):
    """One throttled send must not be followed by another.

    Meta counts each message to the pair against the same limit, so a fallback
    after a refusal turns one refusal into two messages while the guest is
    already blocked. A throttled reply makes exactly one attempt and stops.
    """
    from core.channel.adapter import Button, Prompt
    from core.dispatcher import _reply

    channel.throttle = True
    prompt = Prompt(body="Would you like to book a room?",
                    buttons=(Button("book", "Book"),))

    queued = _reply(db, channel, "+919000009999",
                  "We have 4 beds free for 1 night.", prompt=prompt)
    _drain(db, channel)
    assert queued is True, "the message is durably queued for the worker"
    assert channel.attempts == ["interactive"], (
        "the answer and choices must be one interactive send, never a text "
        "message followed by a second prompt")


def test_a_failed_interactive_response_never_falls_back_to_plain_text(db, channel):
    """Interactive-only conversation must not silently send a text fallback."""
    from core.channel.adapter import Button, Prompt
    from core.dispatcher import _reply

    channel.fail_interactive = True
    body = "AC room. With food or without?"
    prompt = Prompt(body=body, buttons=(Button("food:1", "With food"),
                                        Button("food:0", "Without food")))

    queued = _reply(db, channel, "+919000009999", body, prompt=prompt)
    _drain(db, channel)
    _make_due(db)
    _drain(db, channel)

    assert queued is True
    assert channel.attempts == ["interactive"], \
        "a failed interactive response must not create a second text reply"
    assert channel.sent == []


def test_answer_and_choices_are_queued_as_one_interactive_message(db, channel):
    """One inbound answer must produce one interactive payload containing both."""
    from core.channel.adapter import Button, Prompt
    from core.dispatcher import _reply

    prompt = Prompt(body="Would you like to book a room?",
                    buttons=(Button("book", "Book"),))

    queued = _reply(db, channel, "+919000009999",
                  "We have 4 beds free for 1 night.", prompt=prompt)
    _drain(db, channel)

    assert queued is True
    assert channel.attempts == ["interactive"]
    assert len(channel.sent) == 1
    assert channel.sent[0][1] == (
        "We have 4 beds free for 1 night.\n\nWould you like to book a room?")


def test_a_guest_reply_without_a_prompt_still_goes_out_interactive(db, channel):
    """Interactive-only: a guest reply with no question gets terminal actions.

    The answer has no choices of its own, so the dispatcher attaches the
    terminal actions. One inbound message still produces one interactive
    message, never a bare text message.
    """
    from core.dispatcher import _reply

    channel.throttle = True
    queued = _reply(db, channel, "+919000009999", "hello")
    _drain(db, channel)

    assert queued is True
    assert channel.attempts == ["interactive"], (
        "a guest reply is interactive even when the answer has no question")


def test_the_owner_is_answered_in_plain_text(db, channel):
    """The interactive-only rule is about guests; the owner gets plain text."""
    from types import SimpleNamespace

    from core.dispatcher import _reply

    settings = SimpleNamespace(owner_phone="+919999999999", staff_phones=(),
                               meta_phone_number_id="test-phone-id",
                               payment_proof_dir="/tmp/sagar-test-proofs")
    _reply(db, channel, "+919999999999", "3 beds free today", settings=settings)
    _drain(db, channel)

    assert channel.attempts == ["text"]
    assert channel.prompts == []


def _worker_settings():
    from types import SimpleNamespace
    return SimpleNamespace(meta_phone_number_id="test-phone-id", owner_phone=None,
                           payment_proof_dir="/tmp/sagar-test-proofs")


def _drain(db, channel):
    from core.outbound_worker import run_batch
    run_batch(db, channel, _worker_settings())


def _make_due(db):
    db.execute("UPDATE outbound_messages SET next_attempt_at=now() WHERE state='pending'")
