"""The interactive-only contract, end to end over the webhook.

One incoming guest message must produce exactly one logical interactive reply:
never a text answer followed by a separate prompt, and never a bare text message
with no choices. This file drives real inbound messages through the webhook and
asserts that contract for the outcomes that used to fall back to plain text --
an informational answer, an acknowledgement, an error -- as well as the ones
that already carried a question.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from tests.conftest import APP_SECRET, channel, client, db, flush_outbox  # noqa: F401

_counter = [0]


def _send(client, text, *, sender="+919000002000"):
    _counter[0] += 1
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": sender, "id": f"wamid.IO{_counter[0]}",
                          "timestamp": "1735689600", "type": "text",
                          "text": {"body": text}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhook", content=body,
                           headers={"X-Hub-Signature-256": f"sha256={sig}",
                                    "Content-Type": "application/json"})
    assert response.status_code == 200
    return response


def _assert_one_interactive(channel, guest):
    """The guest got exactly one interactive message and no bare text."""
    prompts = [p for phone, p in channel.prompts if phone == guest]
    assert len(prompts) == 1, f"expected one interactive reply, got {len(prompts)}"
    prompt = prompts[0]
    assert prompt.is_valid(), "the interactive must satisfy Meta's limits"
    from core.channel.adapter import MAX_INTERACTIVE_BODY
    assert len(prompt.body) <= MAX_INTERACTIVE_BODY, \
        "the one interactive body must respect Meta's character cap"
    guest_texts = [t for phone, t in channel.texts if phone == guest]
    assert guest_texts == [], f"the guest got a bare text message: {guest_texts}"
    assert channel.attempts.count("text") == 0, "no text send was even attempted"
    assert channel.attempts.count("interactive") == 1, "exactly one interactive send"


def test_a_greeting_is_one_interactive(client, channel):
    _send(client, "hi", sender="+919000002001")
    _assert_one_interactive(channel, "+919000002001")


def test_an_informational_answer_is_one_interactive(client, channel):
    # A location answer has no question of its own; it gets terminal actions.
    _send(client, "where are you located?", sender="+919000002002")
    _assert_one_interactive(channel, "+919000002002")


def test_an_acknowledgement_is_one_interactive(client, channel):
    _send(client, "thanks!", sender="+919000002003")
    _assert_one_interactive(channel, "+919000002003")


def test_a_media_message_is_acknowledged_interactively(client, channel):
    # An unsupported type used to get a bare "please send it as text" reply.
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": "+919000002004", "id": "wamid.IO-media",
                          "timestamp": "1735689600", "type": "audio",
                          "audio": {"id": "media-1"}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    client.post("/webhook", content=body,
                headers={"X-Hub-Signature-256": f"sha256={sig}",
                         "Content-Type": "application/json"})
    _assert_one_interactive(channel, "+919000002004")


def test_the_answer_and_the_choices_share_one_body(client, channel):
    _send(client, "what are your prices?", sender="+919000002005")
    flush_outbox(client)
    prompts = [p for phone, p in channel.prompts if phone == "+919000002005"]
    assert len(prompts) == 1
    prompt = prompts[0]
    # The rate card answer and the question travel in the one interactive body.
    assert "which room type" in prompt.body.lower()


# --- terminal actions have real handlers -----------------------------------

_TERMINAL_TAP_IDS = ("menu:book", "menu:availability", "menu:price",
                     "menu:location", "menu:talk")


def _tap(client, button_id, *, sender):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": sender, "id": f"wamid.TAP-{button_id}",
                          "timestamp": "1735689600", "type": "interactive",
                          "interactive": {"type": "button_reply",
                                          "button_reply": {"id": button_id,
                                                           "title": "tapped"}}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    client.post("/webhook", content=body,
                headers={"X-Hub-Signature-256": f"sha256={sig}",
                         "Content-Type": "application/json"})


def test_every_terminal_action_tap_is_answered_interactively(client, channel):
    """Each action offered under an answer must resolve to a real handler.

    A tap that fell through to the AI, or to nothing, would break the
    interactive-only contract: the guest would get no reply, or a bare text one.
    """
    for i, button_id in enumerate(_TERMINAL_TAP_IDS):
        guest = f"+9190000021{i:02d}"
        interactive_before = channel.attempts.count("interactive")
        text_before = channel.attempts.count("text")
        _tap(client, button_id, sender=guest)
        flush_outbox(client)

        prompts = [p for phone, p in channel.prompts if phone == guest]
        assert len(prompts) == 1, f"{button_id}: expected one interactive reply"
        assert prompts[0].is_valid(), button_id
        from core.channel.adapter import MAX_INTERACTIVE_BODY
        assert len(prompts[0].body) <= MAX_INTERACTIVE_BODY, button_id
        assert [t for phone, t in channel.texts if phone == guest] == [], button_id
        assert channel.attempts.count("interactive") == interactive_before + 1, button_id
        assert channel.attempts.count("text") == text_before, button_id


def test_a_repeated_tap_is_answered_once(client, channel):
    """Meta retries deliveries; a duplicated tap must not produce two replies."""
    guest = "+919000002199"
    _tap(client, "menu:location", sender=guest)
    _tap(client, "menu:location", sender=guest)   # same wamid -> a retry
    flush_outbox(client)
    _assert_one_interactive(channel, guest)


# --- the payment screenshot is an inbound message too ----------------------

def _accepted_booking(db, guest):
    from datetime import datetime, timedelta

    from core import booking as booking_mod
    from tests.conftest import IST

    start = datetime(2027, 2, 1, 12, tzinfo=IST)
    request = booking_mod.create_request(
        db, phone=guest, full_name="Proof Guest", gender="MALE",
        bed_id="1NA-B1", check_in_at=start,
        check_out_at=start + timedelta(days=2), food_included=False)
    booking_mod.accept(db, request.booking_id, actor="owner")
    return request


def test_a_payment_screenshot_gets_one_interactive_reply(client, db, channel):
    """The screenshot is one inbound guest message, so it gets one interactive.

    The acknowledgement used to be a bare text message, which broke the
    interactive-only contract for a guest who was mid-booking.
    """
    guest = "+919000002301"
    _accepted_booking(db, guest)
    flush_outbox(client)  # deliver the acceptance notice first

    prompts_before = len([p for ph, p in channel.prompts if ph == guest])
    texts_before = len([t for ph, t in channel.texts if ph == guest])
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": guest.lstrip("+"), "id": "wamid.PROOF-IO",
                          "timestamp": "1735689600", "type": "image",
                          "image": {"id": "media-1", "mime_type": "image/jpeg"}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    client.post("/webhook", content=body,
                headers={"X-Hub-Signature-256": f"sha256={sig}",
                         "Content-Type": "application/json"})
    flush_outbox(client)

    prompts = [p for ph, p in channel.prompts if ph == guest]
    texts = [t for ph, t in channel.texts if ph == guest]
    assert len(prompts) == prompts_before + 1, \
        "the screenshot gets exactly one interactive reply"
    assert len(texts) == texts_before, "no bare text was sent to the guest"
    assert "screenshot" in prompts[-1].body.lower()
    assert prompts[-1].is_valid()


def test_the_acceptance_notice_is_one_interactive(client, db, channel):
    """A booking notice is a guest message too: it is one interactive, not text."""
    guest = "+919000002302"
    _accepted_booking(db, guest)
    flush_outbox(client)

    prompts = [p for ph, p in channel.prompts if ph == guest]
    assert len(prompts) == 1, "the acceptance notice is one interactive message"
    assert "accepted" in prompts[0].body.lower()
    assert prompts[0].is_valid()
    from core.channel.adapter import MAX_INTERACTIVE_BODY
    assert len(prompts[0].body) <= MAX_INTERACTIVE_BODY
    assert [t for ph, t in channel.texts if ph == guest] == [], \
        "the guest must not also get a bare text notice"
