"""Pricing engine tests. Every amount must be computed, never guessed."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core import pricing
from core.errors import NotFound, ValidationError

pytestmark = pytest.mark.usefixtures("seeded")


def test_daily_rate_for_three_nights(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(days=3))
    assert quote.units == 3
    assert quote.unit_rate == Decimal("250.00")
    assert quote.charges_total == Decimal("750.00")


def test_partial_day_rounds_up(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(days=1, hours=2))
    assert quote.units == 2
    assert quote.charges_total == Decimal("500.00")


def test_less_than_a_day_is_charged_one_day(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(hours=3))
    assert quote.units == 1
    assert quote.charges_total == Decimal("250.00")


def test_ac_rate_differs_from_non_ac(db, tomorrow):
    ac = pricing.quote(db, room_type="AC", food_included=True,
                       start=tomorrow, end=tomorrow + timedelta(days=1))
    non_ac = pricing.quote(db, room_type="NON_AC", food_included=True,
                           start=tomorrow, end=tomorrow + timedelta(days=1))
    assert ac.unit_rate == Decimal("350.00")
    assert non_ac.unit_rate == Decimal("250.00")
    assert ac.charges_total > non_ac.charges_total


def test_a_day_rate_costs_more_than_a_night_rate(db, tomorrow):
    """The owner quotes both. A daytime stay is priced higher than an overnight."""
    night = pricing.quote(db, room_type="NON_AC", food_included=True, stay_mode="NIGHT",
                          start=tomorrow, end=tomorrow + timedelta(days=1))
    day = pricing.quote(db, room_type="NON_AC", food_included=True, stay_mode="DAY",
                        start=tomorrow, end=tomorrow + timedelta(days=1))
    assert night.unit_rate == Decimal("250.00")
    assert day.unit_rate == Decimal("350.00")


def test_day_rate_for_ac(db, tomorrow):
    quote = pricing.quote(db, room_type="AC", food_included=True, stay_mode="DAY",
                          start=tomorrow, end=tomorrow + timedelta(days=1))
    assert quote.unit_rate == Decimal("450.00")


def test_night_rate_is_the_default(db, tomorrow):
    """A guest asking for 'a room' means overnight unless they say otherwise."""
    default = pricing.quote(db, room_type="AC", food_included=True,
                            start=tomorrow, end=tomorrow + timedelta(days=1))
    assert default.unit_rate == Decimal("350.00")
    assert default.breakdown["stay_mode"] == "NIGHT"


def test_dormitory_mat_rate(db, tomorrow):
    quote = pricing.quote(db, room_type="DORM", food_included=False,
                          start=tomorrow, end=tomorrow + timedelta(days=2))
    assert quote.charges_total == Decimal("300.00")


def test_dormitory_day_rate(db, tomorrow):
    quote = pricing.quote(db, room_type="DORM", food_included=False, stay_mode="DAY",
                          start=tomorrow, end=tomorrow + timedelta(days=1))
    assert quote.unit_rate == Decimal("200.00")


def test_hourly_billing(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True, mode="hourly",
                          start=tomorrow, end=tomorrow + timedelta(hours=3))
    assert quote.mode == "hourly"
    assert quote.units == 3
    assert quote.unit_rate == Decimal("20.00")
    assert quote.charges_total == Decimal("60.00")


def test_hourly_minimum_applies(db, tomorrow):
    plan = pricing._load_plan(db, "NON_AC", True)
    plan = pricing.RatePlan(**{**plan.__dict__, "min_hours": 4})
    quote = pricing.compute_charge(plan, tomorrow, tomorrow + timedelta(hours=1), mode="hourly")
    assert quote.units == 4


def test_extras_added(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(days=1),
                          extras=100)
    assert quote.charges_total == Decimal("350.00")
    assert quote.extras == Decimal("100.00")


def test_discount_applied(db, tomorrow):
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(days=1),
                          discount=50)
    assert quote.charges_total == Decimal("200.00")
    assert quote.discount == Decimal("50.00")


def test_discount_larger_than_total_rejected(db, tomorrow):
    with pytest.raises(ValidationError):
        pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=1),
                      discount=10000)


def test_negative_discount_rejected(db, tomorrow):
    with pytest.raises(ValidationError):
        pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=1),
                      discount=-10)


def test_no_rate_plan_refuses_to_quote(db, tomorrow):
    db.execute("UPDATE rate_plans SET active = false WHERE room_type = 'AC'")
    with pytest.raises(NotFound, match="no active rate plan"):
        pricing.quote(db, room_type="AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=1))


def test_checkout_before_checkin_rejected(db, tomorrow):
    with pytest.raises(ValidationError):
        pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow - timedelta(days=1))


def test_set_rate_replaces_the_previous_plan(db, tomorrow):
    pricing.set_rate(db, name="Non-AC with food v2", room_type="NON_AC",
                     food_included=True, daily_rate=400)
    active = db.query_all(
        "SELECT name, daily_rate FROM rate_plans WHERE room_type='NON_AC' "
        "AND food_included AND stay_mode='NIGHT' AND active"
    )
    assert len(active) == 1
    assert active[0]["name"] == "Non-AC with food v2"
    quote = pricing.quote(db, room_type="NON_AC", food_included=True,
                          start=tomorrow, end=tomorrow + timedelta(days=1))
    assert quote.charges_total == Decimal("400.00")


def test_setting_a_night_rate_leaves_the_day_rate_alone(db, tomorrow):
    """The two stay modes are independent; changing one must not disturb the other."""
    pricing.set_rate(db, name="Non-AC night v2", room_type="NON_AC",
                     food_included=True, daily_rate=225, stay_mode="NIGHT")
    night = pricing.quote(db, room_type="NON_AC", food_included=True, stay_mode="NIGHT",
                          start=tomorrow, end=tomorrow + timedelta(days=1))
    day = pricing.quote(db, room_type="NON_AC", food_included=True, stay_mode="DAY",
                        start=tomorrow, end=tomorrow + timedelta(days=1))
    assert night.unit_rate == Decimal("225.00")
    assert day.unit_rate == Decimal("350.00")


def test_no_day_rate_refuses_to_quote_that_mode(db, tomorrow):
    db.execute("UPDATE rate_plans SET active = false "
               "WHERE room_type = 'AC' AND stay_mode = 'DAY'")
    with pytest.raises(NotFound, match="per day"):
        pricing.quote(db, room_type="AC", food_included=True, stay_mode="DAY",
                      start=tomorrow, end=tomorrow + timedelta(days=1))


def test_set_rate_requires_at_least_one_rate(db):
    with pytest.raises(ValidationError):
        pricing.set_rate(db, name="empty", room_type="AC", food_included=True)


def test_money_rounding_half_up():
    assert pricing.money("10.005") == Decimal("10.01")
    assert pricing.money("10.004") == Decimal("10.00")
