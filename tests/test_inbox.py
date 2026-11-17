"""Inbox tests: duplicate-message protection and enquiry tracking.

A duplicate WhatsApp delivery must never produce a second reply or a second
booking. This is enforced by a UNIQUE constraint on the provider message id.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

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


def test_stale_unprocessed_message_is_claimed_atomically(db):
    stored = inbox.store_inbound(db, _message())
    db.execute("UPDATE inbound_processing SET next_retry_at = now() - interval '20 minutes' "
               "WHERE message_id = %s", (stored.message_id,))

    claimed = inbox.claim_stale_unprocessed(db)
    assert [str(row["message_id"]) for row in claimed] == [stored.message_id]
    assert inbox.claim_stale_unprocessed(db) == []
    assert claimed[0]["claim_token"]


def test_duplicate_delivery_creates_one_inbound_retry_job(db):
    first = inbox.store_inbound(db, _message())
    second = inbox.store_inbound(db, _message())
    assert first is not None and second is None
    assert db.query_one("SELECT count(*) AS n FROM inbound_processing")["n"] == 1


def test_retry_uses_bounded_exponential_backoff(db, monkeypatch):
    stored = inbox.store_inbound(db, _message())
    claimed = inbox.claim_inbound(db, message_id=stored.message_id, force=True)[0]
    monkeypatch.setattr(inbox.random, "uniform", lambda low, high: high)

    state = inbox.fail_inbound_attempt(db, stored.message_id, claimed["claim_token"],
                                       TimeoutError("sensitive detail"))
    row = db.query_one("SELECT * FROM inbound_processing WHERE message_id = %s",
                       (stored.message_id,))
    assert state == "pending"
    assert row["attempt_count"] == 1
    assert row["last_error_code"] == "TimeoutError"
    assert row["last_error"] == "TimeoutError"
    assert row["next_retry_at"] >= row["updated_at"] + timedelta(seconds=35)
    assert inbox.claim_inbound(db, message_id=stored.message_id) == []


def test_expired_claim_is_recovered_and_old_token_is_fenced(db):
    stored = inbox.store_inbound(db, _message())
    old = inbox.claim_inbound(db, message_id=stored.message_id, force=True)[0]
    db.execute("UPDATE inbound_processing SET claimed_until = now() - interval '1 second' "
               "WHERE message_id = %s", (stored.message_id,))

    new = inbox.claim_inbound(db, message_id=stored.message_id)[0]
    assert new["claim_token"] != old["claim_token"]
    assert new["attempt_count"] == 2
    assert inbox.mark_processed(db, stored.message_id, old["claim_token"]) is False
    row = db.query_one("SELECT state, claim_token FROM inbound_processing WHERE message_id = %s",
                       (stored.message_id,))
    assert row["state"] == "processing"
    assert str(row["claim_token"]) == new["claim_token"]


def test_concurrent_workers_cannot_claim_the_same_inbound_job(db):
    stored = inbox.store_inbound(db, _message())
    db.execute("UPDATE inbound_processing SET next_retry_at = now() - interval '1 second' "
               "WHERE message_id = %s", (stored.message_id,))
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return inbox.claim_inbound(db, limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(len(batch) for batch in claims) == 1


def test_crash_after_business_side_effect_is_not_replayed(db):
    from datetime import datetime, timedelta

    from core import booking
    from tests.conftest import IST

    stored = inbox.store_inbound(db, _message())
    claim = inbox.claim_inbound(db, message_id=stored.message_id, force=True)[0]
    assert inbox.mark_dispatch_started(db, stored.message_id, claim["claim_token"])
    start = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    booking.create_request(db, phone="+919000000700", full_name="Retry Guest",
                           gender="MALE", bed_id="1NA-B1", check_in_at=start,
                           check_out_at=start + timedelta(days=1), food_included=True)

    db.execute("UPDATE inbound_processing SET claimed_until = now() - interval '1 second' "
               "WHERE message_id = %s", (stored.message_id,))
    assert inbox.claim_inbound(db, message_id=stored.message_id) == []
    state = db.query_one("SELECT state, last_error_code FROM inbound_processing "
                         "WHERE message_id = %s", (stored.message_id,))
    assert state["state"] == "failed"
    assert state["last_error_code"] == "worker_lease_expired_after_dispatch_started"
    assert db.query_one("SELECT count(*) AS n FROM bookings")["n"] == 1


def test_inbound_retry_exhaustion_is_terminal(db):
    stored = inbox.store_inbound(db, _message())
    for attempt in range(inbox.INBOUND_MAX_ATTEMPTS):
        claimed = inbox.claim_inbound(db, message_id=stored.message_id, force=True)
        assert len(claimed) == 1
        state = inbox.fail_inbound_attempt(db, stored.message_id,
                                           claimed[0]["claim_token"], TimeoutError())
        if attempt < inbox.INBOUND_MAX_ATTEMPTS - 1:
            db.execute("UPDATE inbound_processing SET next_retry_at = now() - interval '1 second' "
                       "WHERE message_id = %s", (stored.message_id,))
            assert state == "pending"
        else:
            assert state == "failed"
    assert inbox.claim_inbound(db, message_id=stored.message_id, force=True) == []
    row = db.query_one("SELECT state, attempt_count, failed_at FROM inbound_processing "
                       "WHERE message_id = %s", (stored.message_id,))
    assert row["state"] == "failed"
    assert row["attempt_count"] == inbox.INBOUND_MAX_ATTEMPTS
    assert row["failed_at"] is not None


def test_permanent_inbound_error_fails_without_retry(db):
    stored = inbox.store_inbound(db, _message())
    claimed = inbox.claim_inbound(db, message_id=stored.message_id, force=True)[0]
    state = inbox.fail_inbound_attempt(db, stored.message_id, claimed["claim_token"],
                                       ValueError("bad input"), retryable=False)
    row = db.query_one("SELECT state, next_retry_at, failed_at FROM inbound_processing "
                       "WHERE message_id = %s", (stored.message_id,))
    assert state == "failed"
    assert row["state"] == "failed"
    assert row["next_retry_at"] is None
    assert row["failed_at"] is not None


def test_stored_interactive_message_can_be_rebuilt_for_retry(db):
    message = InboundMessage(
        provider_message_id="wamid.tap", from_phone="+919000000700",
        text="AC", message_type="interactive", button_id="price:room:AC",
        timestamp=1735689600,
        raw={"id": "wamid.tap", "type": "interactive", "timestamp": "1735689600",
             "interactive": {"button_reply": {"id": "price:room:AC", "title": "AC"}}},
    )
    stored = inbox.store_inbound(db, message)
    row = db.query_one("SELECT message_id, wa_message_id, body, payload, received_at, "
                       "conversation_id FROM messages WHERE message_id = %s",
                       (stored.message_id,))

    rebuilt = inbox.inbound_from_row(db, row)
    assert rebuilt.provider_message_id == message.provider_message_id
    assert rebuilt.from_phone == message.from_phone
    assert rebuilt.button_id == message.button_id
    assert rebuilt.message_type == "interactive"


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
