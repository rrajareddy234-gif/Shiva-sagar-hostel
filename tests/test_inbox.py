"""Inbox tests: duplicate-message protection and enquiry tracking.

A duplicate WhatsApp delivery must never produce a second reply or a second
booking. This is enforced by a UNIQUE constraint on the provider message id.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import inbox
from core.channel.adapter import InboundMessage

pytestmark = pytest.mark.usefixtures("seeded")


def _message(message_id="wamid.1", phone="+919000000700", text="hello"):
    return InboundMessage(
        provider_message_id=message_id, from_phone=phone, text=text,
        message_type="text", timestamp=1735689600, raw={"id": message_id},
    )


def test_first_message_is_stored(db):
    stored = inbox.store_inbound(db, _message())
    assert stored is not None
    assert stored.is_new is True
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_duplicate_delivery_is_ignored(db):
    """The same provider message id arriving twice is stored only once."""
    first = inbox.store_inbound(db, _message())
    second = inbox.store_inbound(db, _message())
    assert first is not None
    assert second is None
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_message_without_a_provider_id_is_dropped(db):
    assert inbox.store_inbound(db, _message(message_id="")) is None
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 0


def test_distinct_messages_are_both_stored(db):
    inbox.store_inbound(db, _message(message_id="wamid.A"))
    inbox.store_inbound(db, _message(message_id="wamid.B"))
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 2


def test_rapid_messages_from_one_customer_share_a_conversation(db):
    for i in range(5):
        inbox.store_inbound(db, _message(message_id=f"wamid.{i}", text=f"msg {i}"))
    conversations = db.query_all("SELECT phone FROM conversations")
    assert len(conversations) == 1
    assert db.query_one("SELECT count(*) AS n FROM messages")["n"] == 5


def test_processing_can_be_marked_done(db):
    stored = inbox.store_inbound(db, _message())
    inbox.mark_processed(db, stored.message_id)
    row = db.query_one("SELECT processed_at FROM messages WHERE message_id = %s",
                       (stored.message_id,))
    assert row["processed_at"] is not None


# --- enquiry tracking ------------------------------------------------------

def test_first_message_opens_an_enquiry(db):
    inbox.store_inbound(db, _message())
    enquiries = inbox.open_enquiries(db)
    assert len(enquiries) == 1
    assert enquiries[0]["status"] == "OPEN"
    assert enquiries[0]["responded"] is False


def test_a_reply_marks_the_enquiry_answered(db):
    inbox.store_inbound(db, _message())
    inbox.record_outbound(db, "+919000000700", "Hello, how can we help?")
    enquiries = inbox.open_enquiries(db)
    assert enquiries[0]["responded"] is True
    assert enquiries[0]["status"] == "RESPONDED"


def test_unanswered_enquiry_is_detected(db):
    inbox.store_inbound(db, _message())
    later = datetime.now().astimezone() + timedelta(hours=2)
    unanswered = inbox.unanswered_enquiries(db, now=later, threshold=timedelta(minutes=30))
    assert len(unanswered) == 1
    assert unanswered[0]["phone"] == "+919000000700"


def test_answered_enquiry_is_not_flagged(db):
    inbox.store_inbound(db, _message())
    inbox.record_outbound(db, "+919000000700", "replied")
    later = datetime.now().astimezone() + timedelta(hours=2)
    assert inbox.unanswered_enquiries(db, now=later, threshold=timedelta(minutes=30)) == []


def test_recent_enquiry_is_not_yet_flagged(db):
    inbox.store_inbound(db, _message())
    assert inbox.unanswered_enquiries(db, threshold=timedelta(minutes=30)) == []


def test_second_message_does_not_open_a_second_enquiry(db):
    inbox.store_inbound(db, _message(message_id="wamid.A"))
    inbox.store_inbound(db, _message(message_id="wamid.B"))
    assert len(inbox.open_enquiries(db)) == 1


def test_enquiries_in_a_period(db):
    inbox.store_inbound(db, _message())
    now = datetime.now().astimezone()
    assert len(inbox.enquiries_between(db, now - timedelta(minutes=5),
                                       now + timedelta(minutes=5))) == 1
    assert inbox.enquiries_between(db, now + timedelta(days=1),
                                   now + timedelta(days=2)) == []


def test_conversation_state_round_trips(db):
    inbox.set_conversation_state(db, "+919000000700", {"stage": "collecting_dates", "step": 2})
    state = inbox.get_conversation_state(db, "+919000000700")
    assert state["stage"] == "collecting_dates"
    assert state["step"] == 2


def test_outbound_reply_is_recorded(db):
    inbox.record_outbound(db, "+919000000700", "our AC rooms are 450 a day")
    row = db.query_one("SELECT direction, body FROM messages ORDER BY received_at DESC LIMIT 1")
    assert row["direction"] == "OUT"
    assert "450" in row["body"]


# --- input bounds ----------------------------------------------------------

def test_a_normal_message_is_stored_whole(db):
    body = "please book a non-ac bed for two nights"
    inbox.store_inbound(db, _message(text=body))
    row = db.query_one("SELECT body FROM messages")
    assert row["body"] == body


def test_an_oversized_message_is_clipped_before_storage(db):
    """A payload far larger than WhatsApp allows must not reach the database whole."""
    inbox.store_inbound(db, _message(text="x" * 100_000))
    row = db.query_one("SELECT length(body) AS n FROM messages")
    assert row["n"] == inbox.MAX_TEXT_LENGTH


def test_bounded_text_passes_none_and_short_text_through(db):
    assert inbox.bounded_text(None) is None
    assert inbox.bounded_text("hi") == "hi"
    assert len(inbox.bounded_text("y" * 5000)) == inbox.MAX_TEXT_LENGTH
