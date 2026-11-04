"""Deterministic router tests.

The router must answer common questions from the database, with no AI call, and
hand genuinely free text to the AI layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core import booking, payments, router
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"


def _now():
    return datetime.now(IST)


def _confirmed(db, bed_id="1NA-B1", phone="+919000000800", food=True):
    start = _now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    request = booking.create_request(
        db, phone=phone, full_name="Dummy Guest", gender="MALE", bed_id=bed_id,
        check_in_at=start, check_out_at=start + timedelta(days=2), food_included=food)
    booking.approve(db, request.booking_id, actor=OWNER)
    return request


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("text,intent", [
    ("hi", "greeting"),
    ("Hello", "greeting"),
    ("what is the price?", "price"),
    ("how much for one night", "price"),
    ("do you have any rooms available?", "availability"),
    ("any beds free?", "availability"),
    ("is the hostel full?", "availability"),
    ("do you provide food?", "facilities"),
    ("is wifi available", "facilities"),
    ("thanks", "thanks"),
    ("", "empty"),
    ("my dog's name is Bruno", "unknown"),
])
def test_classify(text, intent):
    assert router.classify(text) == intent


# --- customer answers ------------------------------------------------------

def test_greeting_returns_help(db):
    reply = router.route(db, "hi")
    assert reply.handled is True
    assert "availability" in reply.text.lower() or "Sagar PG" in reply.text


# --- the greeting menu, answered by tapping --------------------------------

def test_greeting_offers_a_menu_of_taps(db):
    """A bare "hi" should offer choices, not a paragraph to read."""
    reply = router.route(db, "hi")
    assert reply.prompt is not None
    assert [b.title for b in reply.prompt.buttons] == [
        "Prices", "Availability", "Facilities"]
    assert [b.id for b in reply.prompt.buttons] == [
        "menu:price", "menu:availability", "menu:facilities"]
    # The help text is still the fallback body for a channel without buttons.
    assert "Sagar PG" in reply.text


def test_menu_word_asks_for_the_menu(db):
    for word in ("menu", "help", "options", "start", "MENU!"):
        reply = router.route(db, word)
        assert reply.prompt is not None, word


def test_help_inside_a_sentence_is_not_the_menu(db):
    """"help me book a bed" is a real request, not a call for the menu."""
    reply = router.route(db, "help me book a bed for tomorrow")
    assert reply.intent != "greeting"


def test_tapping_prices_starts_the_price_flow(db):
    reply = router.route(db, "Prices", button_id="menu:price")
    assert reply.prompt is not None
    assert [b.id for b in reply.prompt.buttons][0] == "price:room:AC"


def test_tapping_availability_answers_without_the_ai(db):
    reply = router.route(db, "Availability", button_id="menu:availability")
    assert reply.handled is True
    assert reply.needs_ai is False
    assert "free" in reply.text


def test_tapping_facilities_answers_without_the_ai(db):
    reply = router.route(db, "Facilities", button_id="menu:facilities")
    assert reply.handled is True
    assert reply.needs_ai is False
    assert "Sagar PG" in reply.text


def test_a_menu_button_never_carries_a_price(db):
    reply = router.route(db, "hi")
    for button in reply.prompt.buttons:
        assert "Rs" not in button.title
        assert not any(ch.isdigit() for ch in button.title)


def test_an_unknown_menu_id_is_ignored(db):
    """A stale or foreign menu id must fall through, not crash."""
    reply = router.route(db, "hi", button_id="menu:nonsense")
    assert reply.handled is True
    assert reply.intent == "greeting"


def test_price_quotes_the_owners_rates(db):
    reply = router.route(db, "what is the price?")
    assert reply.handled is True
    assert "200" in reply.text          # non-AC with food, per night
    assert "250" in reply.text          # non-AC with food, per day
    assert "300" in reply.text          # AC with food, per night
    assert "350" in reply.text          # AC with food, per day
    assert "100" in reply.text          # dormitory, per night
    assert "150" in reply.text          # dormitory, per day


def test_price_reply_lists_the_extras(db):
    reply = router.route(db, "what are your rates?")
    assert "New bedsheet" in reply.text
    assert "New towel" in reply.text
    assert "New pillow" in reply.text
    assert "Locker" in reply.text


def test_price_reply_names_the_night_and_day_rates(db):
    """A guest must be able to tell which figure is which."""
    reply = router.route(db, "how much do you charge?")
    assert "a night" in reply.text
    assert "a day" in reply.text


# --- the price flow, answered by tapping -----------------------------------

def test_price_question_offers_room_buttons(db):
    """The guest is asked which room, with taps, instead of reading every rate."""
    reply = router.route(db, "what are the prices?")
    assert reply.prompt is not None
    assert [b.id for b in reply.prompt.buttons] == [
        "price:room:AC", "price:room:NON_AC", "price:room:DORM"]
    assert [b.title for b in reply.prompt.buttons] == ["AC", "Non-AC", "Dormitory"]
    # The full text is still the fallback body for a channel without buttons.
    assert "350" in reply.text


def test_tapping_a_room_asks_the_food_question(db):
    reply = router.route(db, "what are the prices?", button_id="price:room:DORM")
    assert reply.prompt is not None
    assert [b.title for b in reply.prompt.buttons] == ["With food", "Without food"]


def test_tapping_food_quotes_exactly_one_rate_from_the_rate_card(db):
    """Two taps give one room's rate, computed from the database, not guessed."""
    reply = router.route(db, "x", button_id="price:food:AC:1")
    assert "AC with food" in reply.text
    assert "Rs 350 a night" in reply.text
    assert "Rs 450 a day" in reply.text
    # Other rooms must not be listed: that was the whole point of the flow.
    assert "Non-AC" not in reply.text
    assert "Dormitory" not in reply.text


def test_tapping_food_without_quotes_the_without_food_rate(db):
    reply = router.route(db, "x", button_id="price:food:AC:0")
    assert "AC without food" in reply.text
    assert "Rs 300 a night" in reply.text
    assert "Rs 350 a day" in reply.text


def test_a_room_that_offers_only_one_food_option_is_quoted_immediately(db):
    """When a room has no food choice to make, the question is skipped."""
    db.execute("UPDATE rate_plans SET active = false "
               "WHERE room_type = 'NON_AC' AND food_included = false")
    reply = router.route(db, "x", button_id="price:room:NON_AC")
    assert reply.prompt is None, "no question is needed when there is no choice"
    assert "Non-AC with food" in reply.text
    assert "Rs 250 a night" in reply.text


def test_a_tap_is_answered_without_the_ai(db):
    reply = router.route(db, "x", button_id="price:room:AC")
    assert reply.handled is True
    assert reply.needs_ai is False


def test_an_unknown_button_id_falls_through_to_normal_routing(db):
    """A foreign or stale button id must not be mistaken for a price tap."""
    reply = router.route(db, "what is the price?", button_id="something:else")
    assert reply.handled is True
    assert reply.prompt is not None
    assert [b.id for b in reply.prompt.buttons][0] == "price:room:AC"


def test_the_guest_never_sees_a_price_on_a_button(db):
    """Buttons carry the choice only; every figure is computed from the rate card."""
    reply = router.route(db, "what are the prices?")
    for button in reply.prompt.buttons:
        assert "Rs" not in button.title
        assert not any(ch.isdigit() for ch in button.title)


def test_price_does_not_invent_a_figure_when_no_plan_exists(db):
    db.execute("UPDATE rate_plans SET active = false")
    reply = router.route(db, "what is the price?")
    assert reply.handled is True
    assert "ask the owner" in reply.text.lower()
    assert "Rs" not in reply.text


def test_availability_reports_real_counts(db):
    reply = router.route(db, "do you have rooms available?")
    assert reply.handled is True
    assert "10 bed" in reply.text or "10 beds" in reply.text


def test_availability_after_a_booking_shows_one_fewer(db):
    _confirmed(db)
    reply = router.route(db, "any beds free for 2 nights?", now=_now())
    assert "9" in reply.text


def test_availability_when_full(db):
    start = _now().replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    for i, bed in enumerate(["1NA-B1", "1NA-B2", "2AC-B1", "2AC-B2",
                             "6NA-B1", "6NA-B2", "6NA-B3",
                             "DORM-M1", "DORM-M2", "DORM-M3"]):
        guest = booking.get_or_create_guest(db, phone=f"+9190000090{i:02d}")
        db.execute(
            "INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, "
            "status, charges_total) VALUES (%s,%s,%s,%s,'CONFIRMED',0)",
            (guest, bed, start, end))
    reply = router.route(db, "any beds free?", now=_now())
    assert "fully booked" in reply.text.lower()


def test_facilities_lists_only_existing_room_types(db):
    db.execute("DELETE FROM beds WHERE room_id = 'DORM'")
    db.execute("UPDATE rooms SET active = false WHERE room_id = 'DORM'")
    reply = router.route(db, "do you have food?")
    assert "AC" in reply.text
    assert "Non-AC" in reply.text
    assert "dormitory" not in reply.text.lower()


def test_free_text_goes_to_the_ai(db):
    reply = router.route(db, "my dog is called Bruno and he likes biscuits")
    assert reply.handled is False
    assert reply.needs_ai is True


def test_empty_message_goes_to_the_ai(db):
    assert router.route(db, "").needs_ai is True


def test_router_makes_no_ai_call_for_simple_questions(db):
    """There is no AI client in this test at all, and the answer still works."""
    for text in ("hi", "what is the price?", "any beds free?", "do you have food?"):
        reply = router.route(db, text)
        assert reply.handled is True
        assert reply.needs_ai is False


# --- owner answers ---------------------------------------------------------

def test_owner_vacancy_count(db):
    reply = router.owner_answer(db, "how many vacancies today?")
    assert reply.handled is True
    assert "10 of 10 beds are free" in reply.text


def test_owner_vacancy_reflects_a_booking(db):
    _confirmed(db)
    reply = router.owner_answer(db, "how many beds are vacant?")
    assert "9 of 10" in reply.text


def test_owner_outstanding_dues(db):
    request = _confirmed(db)
    reply = router.owner_answer(db, "who has not paid?")
    assert reply.handled is True
    assert "Dummy Guest" in reply.text
    assert request.reference in reply.text


def test_owner_no_dues(db):
    reply = router.owner_answer(db, "who has outstanding payments?")
    assert "No outstanding balances" in reply.text


def test_owner_pending_requests(db):
    start = _now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    booking.create_request(
        db, phone="+919000000801", full_name="Waiting Guest", gender="MALE",
        bed_id="1NA-B1", check_in_at=start, check_out_at=start + timedelta(days=1),
        food_included=True)
    reply = router.owner_answer(db, "show pending booking requests")
    assert "Waiting Guest" in reply.text
    assert "awaiting your approval" in reply.text


def test_owner_no_pending_requests(db):
    assert "No booking requests" in router.owner_answer(db, "pending requests?").text


def test_owner_revenue_this_month(db):
    request = _confirmed(db)
    payment_id = payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                                         amount=700, method="CASH", actor=OWNER)
    payments.verify_payment(db, payment_id, actor=OWNER)
    reply = router.owner_answer(db, "how much did we collect this month?")
    assert "700" in reply.text


def test_owner_revenue_excludes_unverified_money(db):
    request = _confirmed(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=700, method="UPI", actor="+919000000800",
                            txn_ref="UPI-ROUTER-1")
    reply = router.owner_answer(db, "show this month's revenue")
    assert "Rs 0" in reply.text


def test_owner_unverified_payments(db):
    request = _confirmed(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=250, method="UPI", actor="+919000000800",
                            txn_ref="UPI-ROUTER-2")
    reply = router.owner_answer(db, "show payments awaiting verification")
    assert "250" in reply.text
    assert "UPI" in reply.text


def test_owner_overdue_checkouts(db):
    start = _now() - timedelta(days=5)
    request = booking.create_request(
        db, phone="+919000000802", full_name="Overdue Guest", gender="MALE",
        bed_id="1NA-B1", check_in_at=start,
        check_out_at=start + timedelta(days=1), food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    reply = router.owner_answer(db, "any overdue checkouts?")
    assert "Overdue Guest" in reply.text


def test_owner_in_house_list(db):
    start = _now() - timedelta(hours=2)
    request = booking.create_request(
        db, phone="+919000000803", full_name="Staying Guest", gender="MALE",
        bed_id="1NA-B1", check_in_at=start, check_out_at=start + timedelta(days=3),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    reply = router.owner_answer(db, "show all occupied beds")
    assert "Staying Guest" in reply.text


def test_owner_unanswered_enquiries(db):
    from core.channel.adapter import InboundMessage
    from core import inbox
    inbox.store_inbound(db, InboundMessage(
        provider_message_id="wamid.ROUTER", from_phone="+919000000804",
        text="is a room free?", message_type="text"))
    db.execute("UPDATE enquiries SET last_message_at = now() - interval '2 hours'")
    reply = router.owner_answer(db, "which enquiries have not received a reply?")
    assert "+919000000804" in reply.text


def test_owner_free_text_goes_to_the_ai(db):
    reply = router.owner_answer(db, "what do you think of my new signboard?")
    assert reply.handled is False
    assert reply.needs_ai is True


def test_owner_answers_need_no_ai(db):
    for text in ("how many vacancies today?", "show pending booking requests",
                 "who has not paid?", "show this month's revenue"):
        reply = router.owner_answer(db, text)
        assert reply.handled is True, text


# --- AI cost reporting -----------------------------------------------------

def test_owner_can_ask_the_ai_cost(db):
    from core import router
    reply = router.owner_answer(db, "what is the ai cost this month?")
    assert reply.handled
    assert reply.intent == "owner_ai_cost"
    assert "$0" in reply.text


def test_ai_cost_reflects_recorded_calls(db):
    from ai.adapter import AIResponse
    from ai.usage import record_usage
    from core import router
    record_usage(db, AIResponse(text="hi", input_tokens=1_000_000, output_tokens=0,
                                provider="gemini", model="gemini-2.5-flash"),
                 purpose="reply")
    reply = router.owner_answer(db, "show me the ai spend")
    assert "$0.3" in reply.text


def test_a_customer_does_not_get_ai_cost_via_owner_answer(db):
    """The customer path must not expose the figure at all."""
    from core import router
    reply = router.route(db, "what is the ai cost this month?", role="customer")
    assert "$" not in reply.text


# --- full status -----------------------------------------------------------

def test_owner_full_status_is_answered_without_ai(db):
    reply = router.owner_answer(db, "show me the full hostel status")
    assert reply.handled is True
    assert reply.needs_ai is False
    assert reply.intent == "owner_status"


def test_owner_full_status_shows_the_live_numbers(db):
    request = _confirmed(db)
    reply = router.owner_answer(db, "show me the full hostel status")
    assert "9 free of 10" in reply.text
    assert "1 occupied" in reply.text
    assert "Booking requests awaiting you: 0" in reply.text
    assert "Bookings with a balance: 1" in reply.text
    assert "Verified revenue this month" in reply.text

    # A confirmed booking is not yet a guest in the building. Only a check-in
    # makes someone "in house".
    assert "Guests in house: 0" in reply.text
    booking.check_in(db, request.booking_id, actor=OWNER)
    reply = router.owner_answer(db, "show me the full hostel status")
    assert "Guests in house: 1" in reply.text


def test_owner_status_word_alone_works(db):
    for text in ("status", "report", "give me the overall summary",
                 "show the complete picture"):
        reply = router.owner_answer(db, text)
        assert reply.intent == "owner_status", text


def test_full_status_never_reaches_the_ai(db):
    """Every phrase in the brief's list must be answered by SQL, not a model."""
    from core import handler
    for text in ("how many vacancies today?", "show all occupied beds",
                 "who is checking in tomorrow?", "show pending booking requests",
                 "who has outstanding payments?", "how much did we collect today?",
                 "show this month's revenue", "which customers have not checked out?",
                 "how many enquiries did we receive today?",
                 "which enquiries have not received a reply?",
                 "show me the full hostel status"):
        outcome = handler.handle_message(db, phone=OWNER, text=text, owner_phone=OWNER,
                                         ai=None)
        assert outcome.handled_by in ("router", "owner_command"), \
            f"{text!r} fell through to {outcome.handled_by}"
