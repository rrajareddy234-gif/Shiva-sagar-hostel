"""Regressions in the guest journey, each one seen in a real conversation.

What these defend:

  * a guest waiting on the owner is not walled off from ordinary questions --
    "hi", "where are you", "what are the prices" are still answered;
  * a second booking request is still refused while one is live;
  * a typed service ("fresh up", "rest for 3 hours") opens the service flow
    instead of falling to the AI;
  * a booking opened with the details already in the message does not ask for
    them again;
  * a stray tap re-asks the current question instead of killing the flow.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import booking_flow, router
from core.handler import handle_message
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
GUEST = "+919000000777"


def _send(db, text="", *, bid=None, phone=GUEST):
    return handle_message(db, phone=phone, text=text, owner_phone=OWNER,
                          ai=None, button_id=bid)


def _make_request(db, *, phone=GUEST):
    """Walk a booking through to submit, so the guest is waiting on the owner."""
    for bid in ("menu:book", "bk:date:tomorrow", "bk:cout:2", "bk:stay:NIGHT",
                "bk:times:skip", "bk:guests:1", "bk:room:AC", "bk:food:1",
                "bk:terms:ADVANCE"):
        _send(db, bid=bid, phone=phone)
    _send(db, "Waiting Guest", phone=phone)
    return _send(db, bid="bk:submit", phone=phone)


# --- a waiting guest is not blocked from ordinary questions -----------------

@pytest.mark.parametrize("question", [
    "hi",
    "what are your prices",
    "where are you located",
    "do you have wifi",
])
def test_a_waiting_guest_is_still_answered(db, question):
    _make_request(db)
    reply = _send(db, question)
    # The old bug: every one of these returned the "wait for the owner" wall.
    assert reply.intent != "booking_status"
    assert "wait for the owner" not in reply.reply.lower()


def test_a_waiting_guest_can_ask_for_their_booking(db):
    _make_request(db)
    reply = _send(db, "my bookings")
    assert reply.intent == "my_bookings"
    assert "SPG-" in reply.reply


def test_a_second_booking_request_is_still_refused(db):
    _make_request(db)
    for attempt in ("Book a Room", "book a room for 2 nights"):
        reply = _send(db, attempt)
        assert "with the owner" in reply.reply.lower()


def test_an_unrelated_message_still_holds_the_guest(db):
    _make_request(db)
    reply = _send(db, "can I bring a friend")
    assert reply.intent == "booking_status"


# --- typed services open the service flow ----------------------------------

@pytest.mark.parametrize("phrase", [
    "fresh up", "I need a fresh up", "rest for 3 hours", "freshen up please",
])
def test_a_typed_service_opens_the_service_flow(db, phrase):
    reply = _send(db, phrase)
    assert reply.intent == "service"
    assert reply.prompt is not None
    titles = [b.title for b in (reply.prompt.buttons or reply.prompt.rows)]
    assert "Fresh-up" in titles


def test_typed_rest_is_not_read_as_a_room_booking(db):
    # "rest for 3 hours" contains no booking word, but must not reach the AI.
    assert router.classify("rest for 3 hours") == "service"


# --- the opening message is used, not ignored ------------------------------

def test_a_dated_booking_request_does_not_ask_for_the_date_again(db):
    reply = _send(db, "I want to book from 1 December for 2 nights")
    # The date and the nights were both in the message, so the next question is
    # the one thing still missing: the check-in time.
    assert "time would you like to check in" in reply.reply.lower()


def test_a_month_name_is_read_as_a_date():
    parsed = booking_flow._parse_date("book from 1 December for 2 nights")
    assert parsed is not None
    assert (parsed.month, parsed.day) == (12, 1)


def test_a_numeric_date_inside_a_sentence_is_read():
    parsed = booking_flow._parse_date("book 3 days from 2026-12-01")
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-12-01"


def test_a_stay_length_in_the_message_sets_the_checkout(db):
    reply = _send(db, "I want to book from 1 December for 2 nights")
    state = booking_flow._load(db, GUEST)
    assert state.get("check_in") == "2026-12-01"
    assert state.get("check_out") == "2026-12-03"
    assert state.get("stay_mode") == "NIGHT"
    assert reply.prompt is not None


def test_a_message_with_no_date_still_asks_for_it(db):
    reply = _send(db, "book a room")
    assert "check in" in reply.reply.lower()


# --- a stray tap does not kill the flow ------------------------------------

def test_an_unrecognised_tap_reasks_the_current_question(db):
    _send(db, bid="menu:book")
    _send(db, bid="bk:date:tomorrow")
    # A stale or unknown id arrives while the guest is on the check-out step.
    reply = _send(db, bid="bk:bogus:xyz")
    assert "check out" in reply.reply.lower()
    assert "start again" not in reply.reply.lower()


def test_a_stray_tap_does_not_lose_the_answers_so_far(db):
    _send(db, bid="menu:book")
    _send(db, bid="bk:date:tomorrow")
    _send(db, bid="bk:bogus:xyz")
    state = booking_flow._load(db, GUEST)
    assert state.get("check_in") is not None


# --- an unreadable reply must not throw the booking away -------------------

def test_a_stray_reply_at_a_confirm_keeps_the_answer(db):
    """The owner saw the flow jump back to the first question mid-booking.

    The guest had given the date and was asked "Is that correct?". They typed
    something we could not read as a date, and the flow erased the date and
    asked for the check-in date again. The answer must survive.
    """
    _send(db, bid="menu:book")
    _send(db, "2026-12-01")
    stray = _send(db, "2 nights")  # not a date, and not a yes/no
    assert "Is that correct?" in stray.reply
    # The held date is still the one we asked about, not erased.
    state = booking_flow._load(db, GUEST)
    assert state.get("step") == "confirm"
    assert (state.get("pending") or {}).get("value") == "2026-12-01"

    # And confirming still moves on to the next question, not back to the start.
    after = _send(db, bid="bk:cf:yes")
    assert "check out" in after.reply.lower()
    assert "check in" not in after.reply.lower()


def test_a_number_meant_for_another_question_is_not_a_time(db):
    """At the check-in-time question, "1 guest" must not be read as 01:00."""
    for bid in ("menu:book", "bk:date:today", "bk:cout:2", "bk:stay:NIGHT"):
        _send(db, bid=bid)
    reply = _send(db, "1 guest")
    # Still on the time question, not silently confirmed as 1:00 am.
    assert "time" in reply.reply.lower()
    state = booking_flow._load(db, GUEST)
    assert state.get("step") == "times"
    assert not state.get("check_in_time")


def test_a_real_time_is_still_read(db):
    """The guard above must not stop a genuine time from being understood."""
    for bid in ("menu:book", "bk:date:today", "bk:cout:2", "bk:stay:NIGHT"):
        _send(db, bid=bid)
    reply = _send(db, "9 pm")
    assert "Is that correct?" in reply.reply
    assert (booking_flow._load(db, GUEST).get("pending") or {}).get("value", {}) \
        .get("check_in_time") == "21:00"


# --- the greeting shows the offer the owner set ---------------------------

def test_the_greeting_shows_the_active_offer(db):
    """A booking made in the offer window is discounted, so say so up front."""
    from core import pricing
    today = date.today()
    pricing.set_promotion(db, name="Early booking 10%", percent=10,
                          starts_on=today, ends_on=today + timedelta(days=2),
                          scope="DAILY")
    reply = _send(db, "hi")
    assert reply.intent == "greeting"
    assert "10%" in reply.reply
    assert "next 3 days" in reply.reply.lower()
    assert "monthly" in reply.reply.lower()


def test_no_offer_means_the_greeting_does_not_invent_one(db):
    """With no promotion set, the greeting must not promise a discount."""
    reply = _send(db, "hi")
    assert reply.intent == "greeting"
    assert "%" not in reply.reply
