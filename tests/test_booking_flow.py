"""The tap-driven booking flow.

The promise these tests defend: a guest who taps through a booking always makes
a request, and the owner is always told about it -- with no AI involved at all.
Every figure must come from the pricing engine, never from the flow.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core import booking_flow, pricing, router
from core.channel.adapter import InboundMessage
from core.config import Settings
from core.dispatcher import dispatch
from core.handler import handle_message
from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN

OWNER = "+919999999999"
GUEST = "+919000000901"

pytestmark = pytest.mark.usefixtures("seeded")


def _settings():
    return Settings(
        database_url=TEST_DSN, channel_provider="fake", owner_phone=OWNER,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="1",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="fake-1", ai_api_key="k", ai_base_url=None)


def _step(db, *, bid=None, text="", phone=GUEST):
    return handle_message(db, phone=phone, text=text, owner_phone=OWNER,
                          ai=None, button_id=bid)


def _tap_through(db, *, room="AC", food="1", terms="ADVANCE", name="Dummy Guest",
                 phone=GUEST):
    """Walk the whole flow by tapping, as a guest would."""
    _step(db, bid="menu:book", phone=phone)
    _step(db, bid="bk:date:tomorrow", phone=phone)
    _step(db, bid="bk:cout:2", phone=phone)
    _step(db, bid="bk:stay:NIGHT", phone=phone)
    _step(db, bid="bk:times:skip", phone=phone)
    _step(db, bid="bk:guests:1", phone=phone)
    _step(db, bid=f"bk:room:{room}", phone=phone)
    _step(db, bid=f"bk:food:{food}", phone=phone)
    _step(db, bid=f"bk:terms:{terms}", phone=phone)
    _step(db, text=name, phone=phone)
    return _step(db, bid="bk:submit", phone=phone)


# --- the core promise: a request is made and the owner is told -------------

def test_tapping_through_creates_a_request_with_no_ai(db):
    """The whole point: the flow makes the request without the AI."""
    outcome = _tap_through(db)
    assert [e["type"] for e in outcome.events] == ["booking_requested"]
    assert outcome.events[0]["reference"].startswith("SPG-")


def test_the_owner_gets_approve_reject_with_no_ai(db, channel):
    """A full tap-through ends with an Approve/Reject prompt for the owner."""
    for i, bid in enumerate(["menu:book", "bk:date:tomorrow", "bk:cout:1",
                             "bk:stay:NIGHT", "bk:times:skip", "bk:guests:1",
                             "bk:room:AC", "bk:food:1", "bk:terms:ADVANCE"]):
        dispatch(db, channel, _settings(), InboundMessage(
            provider_message_id=f"wamid.{i}", from_phone=GUEST,
            message_type="interactive", text="", button_id=bid), ai=None)
    dispatch(db, channel, _settings(), InboundMessage(
        provider_message_id="wamid.name", from_phone=GUEST,
        message_type="text", text="Dummy Guest"), ai=None)
    dispatch(db, channel, _settings(), InboundMessage(
        provider_message_id="wamid.submit", from_phone=GUEST,
        message_type="interactive", text="", button_id="bk:submit"), ai=None)

    owner_prompts = [p for phone, p in channel.prompts if phone == OWNER]
    assert owner_prompts, "the owner was never told about the request"
    titles = [b.title for b in owner_prompts[-1].buttons]
    assert titles == ["Approve", "Reject"]


def test_the_flow_works_even_when_the_ai_is_down(db):
    """ai=None is the AI-down case; the request and the event still happen."""
    outcome = _tap_through(db, phone="+919000000902")
    assert outcome.events and outcome.events[0]["type"] == "booking_requested"


# --- figures come from the engine, never the flow --------------------------

def test_the_amount_shown_matches_the_pricing_engine(db):
    outcome = _tap_through(db, room="AC", food="1", terms="FULL")
    event = outcome.events[0]
    start = booking_flow._window({
        "check_in": event["check_in"], "check_out": event["check_out"],
        "check_in_time": "21:00", "check_out_time": "09:00"})[0]
    end = start.replace(hour=0) + __import__("datetime").timedelta(days=2)
    q = pricing.quote(db, room_type="AC", food_included=True, start=start,
                      end=end, mode="daily", stay_mode="NIGHT", guests=1)
    assert Decimal(event["charges_total"]) == q.charges_total


def test_the_review_shows_the_amount_before_submitting(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:2")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    _step(db, bid="bk:guests:1")
    _step(db, bid="bk:room:AC")
    _step(db, bid="bk:food:1")
    _step(db, bid="bk:terms:ADVANCE")
    review = _step(db, text="Dummy Guest")
    assert "Total: Rs" in review.reply
    assert review.prompt is not None
    assert [b.title for b in review.prompt.buttons] == ["Submit", "Start over", "Cancel"]


def test_the_submit_message_carries_the_upi_id(db):
    outcome = _tap_through(db, terms="ADVANCE")
    assert booking_flow.DEFAULT_UPI in outcome.reply


# --- entry points ----------------------------------------------------------

def test_availability_answer_offers_a_book_button(db):
    reply = router.route(db, "Availability", button_id="menu:availability")
    assert reply.prompt is not None
    assert [b.id for b in reply.prompt.buttons] == ["menu:book"]


def test_typed_book_a_room_starts_the_flow(db):
    reply = router.route(db, "I want to book a room", phone=GUEST)
    assert reply.handled is True
    assert reply.intent == "book"
    assert reply.prompt is not None


def test_my_bookings_is_honest_when_empty(db):
    reply = router.route(db, "my bookings", phone=GUEST)
    assert reply.handled is True
    assert "don't have any bookings" in reply.text


def test_my_bookings_shows_only_this_guests_own(db):
    _tap_through(db, phone=GUEST)
    mine = router.route(db, "my bookings", phone=GUEST)
    assert "SPG-" in mine.text
    someone_else = router.route(db, "my bookings", phone="+919000000909")
    assert "SPG-" not in someone_else.text


# --- the address never goes past code --------------------------------------

@pytest.mark.parametrize("text", [
    "where exactly is sagar hostel",
    "where",
    "address please",
    "what is the full address",
])
def test_address_questions_are_answered_by_code(db, text):
    assert router.classify(text) == "location"


def test_the_location_reply_includes_the_owner_map_link(db):
    """The owner's own share link is repeated, so the pin is always right."""
    from core import hostel_facts
    hostel_facts.set_fact(db, key="address.full", value="Main Road, Macherla",
                          actor="owner", category="location")
    hostel_facts.set_fact(db, key="address.map_link",
                          value="https://maps.app.goo.gl/S7voZ2PEDLVyHfdZ7",
                          actor="owner", category="location")
    reply = router.route(db, "where is the hostel")
    assert "https://maps.app.goo.gl/S7voZ2PEDLVyHfdZ7" in reply.text


def test_a_removed_map_link_is_never_invented(db):
    """With no link recorded, the reply must not invent one."""
    from core import hostel_facts
    hostel_facts.set_fact(db, key="address.full", value="Main Road, Macherla",
                          actor="owner", category="location")
    reply = router.route(db, "where is the hostel")
    assert "maps" not in reply.text.lower()
    assert hostel_facts.map_link(db) is None


# --- the escape hatch ------------------------------------------------------

def test_a_greeting_abandons_the_flow_and_shows_the_menu(db):
    _step(db, bid="menu:book")
    outcome = _step(db, text="hi")
    assert outcome.intent == "greeting"
    assert not booking_flow.is_active(db, GUEST)


def test_cancel_abandons_the_flow(db):
    _step(db, bid="menu:book")
    outcome = _step(db, text="cancel")
    assert not booking_flow.is_active(db, GUEST)
    assert "cancel" in outcome.reply.lower()


# --- out-of-order taps -----------------------------------------------------

def test_a_tap_that_skips_a_question_restarts_instead_of_crashing(db):
    """A button from an older message must not walk past an unanswered step.

    Without the ordering guard the flow reached review with no room type and
    raised KeyError, so the guest got the generic error fallback and no booking.
    """
    _step(db, bid="menu:book")
    outcome = _step(db, bid="bk:food:1")   # food tapped before date/nights/etc.
    assert "start again" in outcome.reply.lower()
    assert booking_flow.is_active(db, GUEST)


def test_submit_with_an_incomplete_flow_restarts_instead_of_crashing(db):
    _step(db, bid="menu:book")
    outcome = _step(db, bid="bk:submit")
    assert "start again" in outcome.reply.lower()
    assert not _has_booking(db)


def test_a_stale_room_tap_for_the_wrong_step_does_not_skip_ahead(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    # A room tap arrives while the flow is still waiting for the night count.
    outcome = _step(db, bid="bk:room:AC")
    assert outcome is not None
    assert not _has_booking(db)
    # The flow is still usable: the guest can carry on answering.
    assert booking_flow.is_active(db, GUEST)


def _has_booking(db):
    return db.query_one("SELECT count(*) n FROM bookings")["n"] > 0


def test_a_lost_race_is_retried_on_another_free_bed(db, monkeypatch):
    """A guest who loses the race by a hair still gets a booking.

    The first create_request call is made to fail the way a losing race does.
    The flow must retry on whatever is now free, not send the guest back to the
    start with nothing.
    """
    from core import booking as booking_mod
    from core.errors import BedUnavailable

    real = booking_mod.create_request
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BedUnavailable("that bed is already held for those dates")
        return real(*args, **kwargs)

    monkeypatch.setattr(booking_mod, "create_request", flaky)

    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:1")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    _step(db, bid="bk:guests:1")
    _step(db, bid="bk:room:AC")
    _step(db, bid="bk:food:0")
    _step(db, bid="bk:terms:FULL")
    _step(db, text="Race Guest")
    outcome = _step(db, bid="bk:submit")

    assert calls["n"] == 2, "the flow should have retried once"
    assert "sent to the owner" in outcome.reply
    assert _has_booking(db)


# --- a typed value is read back before it is used --------------------------
#
# A value typed by hand is easy to get wrong, and a wrong date or guest count
# sends a wrong bill. So typed answers are confirmed first; tapped ones are not.

def test_typed_checkout_is_confirmed_before_use(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    outcome = _step(db, text="2026-12-25")
    assert "25 December 2026" in outcome.reply
    assert "correct" in outcome.reply.lower()
    from core import booking_flow
    assert booking_flow._load(db, GUEST).get("check_out") is None


def test_confirming_typed_checkout_moves_on(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, text="2026-12-25")
    outcome = _step(db, bid="bk:cf:yes")
    assert "night" in outcome.reply.lower() or "day" in outcome.reply.lower()
    from core import booking_flow
    assert booking_flow._load(db, GUEST)["check_out"] == "2026-12-25"


def test_saying_change_re_asks_the_same_question(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, text="2026-12-25")
    outcome = _step(db, bid="bk:cf:no")
    assert "check out" in outcome.reply.lower()
    from core import booking_flow
    assert booking_flow._load(db, GUEST).get("check_out") is None


def test_a_checkout_before_checkin_is_refused(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    outcome = _step(db, text="2020-01-01")
    assert "after the check-in" in outcome.reply


def test_replying_with_a_date_at_the_confirm_step_corrects_it(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, text="2026-12-25")
    outcome = _step(db, text="2026-12-26")
    assert "26 December 2026" in outcome.reply
    assert "correct" in outcome.reply.lower()


def test_typed_guests_are_confirmed_before_use(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:2")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    outcome = _step(db, text="2")
    assert "2 guests" in outcome.reply
    assert "correct" in outcome.reply.lower()


def test_typed_room_is_confirmed_before_use(db):
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:2")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    _step(db, bid="bk:guests:1")
    outcome = _step(db, text="ac")
    assert "AC" in outcome.reply
    assert "correct" in outcome.reply.lower()
    from core import booking_flow
    assert booking_flow._load(db, GUEST).get("room_type") is None


def test_tapped_answers_are_never_re_asked(db):
    """A tap is already an explicit choice, so no confirmation appears."""
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    outcome = _step(db, bid="bk:cout:2")
    assert "night" in outcome.reply.lower() or "day" in outcome.reply.lower()
    assert "correct" not in outcome.reply.lower()


def test_a_typed_date_is_confirmed(db):
    _step(db, bid="menu:book")
    outcome = _step(db, text="2026-12-20")
    assert "2026-12-20" not in outcome.reply  # shown readably, not raw ISO
    assert "20 December 2026" in outcome.reply
    assert "correct" in outcome.reply.lower()


# --- a full room type is shown, but its tap is refused ---------------------
#
# WhatsApp buttons cannot be truly disabled, so a full room type is labelled
# "(full)" and the tap is answered with a reason instead of a silent restart.

def _fill_room_type(db, room_type, *, phone="+919000000777"):
    from datetime import datetime, timedelta

    from core import availability
    start = datetime.now().astimezone().replace(
        hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    end = start + timedelta(days=2)
    beds = [b.bed_id for b in availability.list_bed_availability(db, start, end)
            if b.room_type == room_type]
    request = booking_flow.booking.create_request(
        db, phone=phone, full_name="Fill Guest", gender=None, bed_ids=beds,
        check_in_at=start, check_out_at=end, food_included=False,
        guests_count=len(beds), payment_terms="FULL")
    booking_flow.booking.approve(db, request.booking_id, actor="owner")
    return start, end


def test_a_full_room_type_is_labelled_full(db):
    _fill_room_type(db, "DORM")
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:2")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    outcome = _step(db, bid="bk:guests:1")
    titles = [b.title for b in (outcome.prompt.buttons or outcome.prompt.rows)]
    assert "Dormitory (full)" in titles


def test_tapping_a_full_room_type_explains_why(db):
    _fill_room_type(db, "DORM")
    _step(db, bid="menu:book")
    _step(db, bid="bk:date:tomorrow")
    _step(db, bid="bk:cout:2")
    _step(db, bid="bk:stay:NIGHT")
    _step(db, bid="bk:times:skip")
    _step(db, bid="bk:guests:1")
    outcome = _step(db, bid="bk:room:DORM")
    assert "full" in outcome.reply.lower()
    assert "full" in " ".join(
        b.title for b in (outcome.prompt.buttons or outcome.prompt.rows)).lower()
