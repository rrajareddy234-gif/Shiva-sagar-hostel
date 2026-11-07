"""AI-lockout alert tests.

The owner must be told when the AI stops answering, and told only once, and
never at the cost of the guest's own reply.
"""

from __future__ import annotations

from ai.adapter import AIResponse, AIUnavailable
from core import alerts
from core.channel.adapter import InboundMessage
from core.config import Settings
from core.dispatcher import dispatch
from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN

OWNER = "+919999999999"
GUEST = "+919000000901"

# Wording the router does not answer itself, so it reaches the AI layer.
GOES_TO_AI = "Can I bring a pet cat along with me?"


class BrokenAI:
    provider = "fake"
    model = "fake-1"

    def complete(self, **kwargs):
        raise AIUnavailable("Gemini returned HTTP 429")


class WorkingAI:
    provider = "fake"
    model = "fake-1"

    def complete(self, **kwargs):
        return AIResponse(text="Yes, well behaved pets are welcome.",
                          input_tokens=10, output_tokens=8,
                          model="fake-1", provider="fake")


def _settings():
    return Settings(
        database_url=TEST_DSN, channel_provider="fake", owner_phone=OWNER,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="1",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="fake-1", ai_api_key="k", ai_base_url=None)


def _dispatch(db, channel, text, ai, sender=GUEST):
    return dispatch(db, channel, _settings(), InboundMessage(
        provider_message_id="wamid.TEST", from_phone=sender,
        message_type="text", text=text), ai=ai)


def _owner_messages(channel):
    return [text for phone, text in channel.sent if phone == OWNER]


# --- the outage alert ------------------------------------------------------

def test_the_owner_is_told_when_the_ai_goes_down(db, channel):
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    messages = _owner_messages(channel)
    assert len(messages) == 1
    assert "stopped answering" in messages[0]


def test_the_guest_still_gets_a_reply_when_the_ai_is_down(db, channel):
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    guest_messages = [text for phone, text in channel.sent if phone == GUEST]
    assert guest_messages, "the guest was left without any reply"
    assert "check with the owner" in guest_messages[0]


def test_the_owner_is_told_once_per_outage_not_once_per_message(db, channel):
    for _ in range(3):
        _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    assert len(_owner_messages(channel)) == 1


def test_recovery_is_announced_once(db, channel):
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    _dispatch(db, channel, GOES_TO_AI, WorkingAI())
    _dispatch(db, channel, GOES_TO_AI, WorkingAI())
    messages = _owner_messages(channel)
    assert len(messages) == 2
    assert "answering again" in messages[1]


def test_a_healthy_ai_alone_never_alerts(db, channel):
    _dispatch(db, channel, GOES_TO_AI, WorkingAI())
    assert _owner_messages(channel) == []


def test_a_second_outage_is_reported_after_a_recovery(db, channel):
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    _dispatch(db, channel, GOES_TO_AI, WorkingAI())
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    messages = _owner_messages(channel)
    assert len(messages) == 3
    assert "stopped answering" in messages[2]


def test_state_is_remembered_in_the_database(db, channel):
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    assert alerts.state(db) == alerts.DOWN
    _dispatch(db, channel, GOES_TO_AI, WorkingAI())
    assert alerts.state(db) == alerts.UP


# --- it must never make things worse ---------------------------------------

def test_a_failed_alert_does_not_stop_the_guest_reply(db, channel):
    channel.fail = True
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())
    # Nothing was delivered, but the state is still recorded so we do not
    # retry on every message.
    assert alerts.state(db) == alerts.DOWN


def test_a_broken_channel_does_not_raise(db, channel):
    channel.fail = True
    _dispatch(db, channel, GOES_TO_AI, BrokenAI())  # must not raise


def test_no_owner_number_means_no_send_but_no_crash(db, channel):
    settings = _settings()
    object.__setattr__(settings, "owner_phone", None)
    dispatch(db, channel, settings, InboundMessage(
        provider_message_id="wamid.TEST", from_phone=GUEST,
        message_type="text", text=GOES_TO_AI), ai=BrokenAI())
    assert alerts.state(db) == alerts.DOWN


def test_alert_does_not_fire_for_a_normal_guest_question(db, channel):
    # A price question is answered by the router, never reaching the AI.
    _dispatch(db, channel, "prices", WorkingAI())
    assert _owner_messages(channel) == []
