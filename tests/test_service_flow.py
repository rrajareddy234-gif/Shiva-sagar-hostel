"""The tap-driven service flow, end to end through the handler.

The same promise the booking flow keeps: a guest who taps through a fresh-up or
a rest always makes a request, the owner is always told, and no AI is involved.
Every figure comes from the rate card.
"""

from __future__ import annotations

import pytest

from core import pricing, services
from core.config import Settings
from core.handler import handle_message
from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN

OWNER = "+919999999999"
GUEST = "+919000000888"

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


def test_the_menu_offers_the_services_from_the_rate_card(db):
    outcome = _step(db, bid="menu:service")
    assert "Fresh-up" in outcome.reply
    assert "Rs 50" in outcome.reply
    assert "Rs 100" in outcome.reply


def test_a_fresh_up_is_requested_by_tapping(db):
    _step(db, bid="menu:service")
    outcome = _step(db, bid="svc:FRESH_UP")
    assert "name" in outcome.reply.lower() or "full name" in outcome.reply.lower()
    _step(db, text="Suresh")
    outcome = _step(db, bid="sv:submit")
    assert [e["type"] for e in outcome.events] == ["service_requested"]
    assert outcome.events[0]["service_code"] == "FRESH_UP"
    assert outcome.events[0]["amount"] == str(pricing.money(50))
    assert outcome.events[0]["bed_id"] is None


def test_a_rest_picks_a_free_bed_then_is_requested(db):
    _step(db, bid="menu:service")
    _step(db, bid="svc:REST_3H")
    _step(db, bid="sv:hours:3")
    _step(db, bid="sv:bed:1NA-B1")
    outcome = _step(db, text="Ravi")
    assert "1NA-B1" in outcome.reply
    outcome = _step(db, bid="sv:submit")
    assert [e["type"] for e in outcome.events] == ["service_requested"]
    assert outcome.events[0]["bed_id"] == "1NA-B1"
    assert outcome.events[0]["amount"] == str(pricing.money(100))


def test_the_service_price_is_never_discounted(db, tomorrow):
    """A daily promotion is live; a fresh-up must still be Rs 50."""
    pricing.set_promotion(db, name="Early 10%", percent=10,
                          starts_on=tomorrow.date(),
                          ends_on=tomorrow.date())
    _step(db, bid="menu:service")
    _step(db, bid="svc:FRESH_UP")
    _step(db, text="Suresh")
    outcome = _step(db, bid="sv:submit")
    assert outcome.events[0]["amount"] == str(pricing.money(50))
    order = services.get_order(db, outcome.events[0]["reference"])
    assert order["amount"] == pricing.money(50)
