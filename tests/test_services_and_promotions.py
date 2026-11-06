"""Walk-in services and the 10% early-booking promotion.

Two rules under test, and they are the ones the owner cares about:
  * a fresh-up and a rest are priced from the rate card and never discounted;
  * the 10% off applies only to a booking made within the next three days, and
    never to a monthly stay, a fresh-up or a rest.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from core import pricing, services
from core.db import Database
from core.errors import BedUnavailable, NotFound

pytestmark = pytest.mark.usefixtures("seeded")


# --- the promotion ---------------------------------------------------------

def _set_promo(db, *, scope="DAILY", starts=None, ends=None):
    today = date.today()
    return pricing.set_promotion(
        db, name="Early booking 10%", percent=10,
        starts_on=starts or today, ends_on=ends or today + timedelta(days=3),
        scope=scope)


def test_a_booking_within_three_days_gets_ten_percent_off(db, tomorrow):
    _set_promo(db)
    q = pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=2))
    # 250 a night, 2 nights = 500, less 10%.
    assert q.charges_total == pricing.money(450)


def test_the_promotion_is_wired_into_the_breakdown(db, tomorrow):
    _set_promo(db)
    q = pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=2))
    assert q.discount == pricing.money(50)
    assert q.breakdown["promotion"] == "Early booking 10%"
    assert q.breakdown["promotion_percent"] == "10.00"


def test_no_promotion_means_no_discount(db, tomorrow):
    q = pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=2))
    assert q.discount == pricing.money(0)
    assert q.charges_total == pricing.money(500)


def test_a_promotion_outside_the_window_does_not_apply(db, tomorrow):
    today = date.today()
    _set_promo(db, starts=today + timedelta(days=10),
               ends=today + timedelta(days=20))
    q = pricing.quote(db, room_type="NON_AC", food_included=True,
                      start=tomorrow, end=tomorrow + timedelta(days=2))
    assert q.discount == pricing.money(0)


def test_monthly_stays_are_never_discounted(db):
    # A DAILY-scope promotion is running, and the monthly quote must ignore it.
    _set_promo(db, scope="DAILY")
    q = pricing.monthly_quote(db, room_type="AC", food_included=True, months=1)
    assert q.breakdown.get("promotion") is None
    assert q.charges_total == pricing.money(8000)


def test_an_all_scope_promotion_still_skips_monthly(db):
    _set_promo(db, scope="ALL")
    q = pricing.monthly_quote(db, room_type="AC", food_included=True, months=1)
    assert q.breakdown.get("promotion") is None


# --- the services ----------------------------------------------------------

def test_fresh_up_and_rest_are_seeded_and_priced(db):
    rows = {s["service_code"]: s for s in pricing.list_services(db)}
    assert rows["FRESH_UP"]["price"] == pricing.money(50)
    assert rows["FRESH_UP"]["needs_bed"] is False
    assert rows["REST_3H"]["price"] == pricing.money(100)
    assert rows["REST_3H"]["needs_bed"] is True


def test_a_fresh_up_needs_no_bed_and_is_not_discounted(db, tomorrow):
    _set_promo(db)  # a daily promotion is live; a service must ignore it
    order = services.create_order(db, phone="+919000000700",
                                  service_code="FRESH_UP", full_name="Fresh Guest")
    assert order.bed_id is None
    assert order.amount == pricing.money(50)


def test_a_rest_holds_a_bed_for_three_hours(db, tomorrow):
    start = tomorrow.replace(hour=10, minute=0)
    order = services.create_order(db, phone="+919000000701",
                                  service_code="REST_3H", full_name="Rest Guest",
                                  bed_id="1NA-B1", starts_at=start)
    assert order.bed_id == "1NA-B1"
    assert order.ends_at - order.starts_at == timedelta(hours=3)
    assert order.amount == pricing.money(100)


def test_a_rest_refuses_a_bed_that_is_already_resting(db, tomorrow):
    start = tomorrow.replace(hour=10, minute=0)
    services.create_order(db, phone="+919000000702", service_code="REST_3H",
                          full_name="First", bed_id="1NA-B1", starts_at=start)
    services.confirm(db, services.live_order_for(db, "+919000000702")["reference"],
                     actor="owner")
    with pytest.raises(BedUnavailable):
        services.create_order(db, phone="+919000000703", service_code="REST_3H",
                              full_name="Second", bed_id="1NA-B1", starts_at=start)


def test_an_unknown_service_is_refused_not_priced(db):
    with pytest.raises(NotFound):
        pricing.get_service(db, "NO_SUCH_SERVICE")


def test_the_review_shows_the_discount_line(db, tomorrow):
    """The guest sees the saving, and it comes from the engine's own numbers."""
    from core import booking_flow
    _set_promo(db)
    state = {
        "check_in": tomorrow.date().isoformat(),
        "check_out": (tomorrow + timedelta(days=2)).date().isoformat(),
        "guests": 1, "room_type": "NON_AC", "food": True,
        "stay_mode": "NIGHT", "terms": "ADVANCE", "name": "Review Guest",
    }
    flow = booking_flow._review(db, "+919000000710", state)
    assert "Subtotal: Rs 500" in flow.reply
    assert "Rs 50" in flow.reply          # the discount
    assert "Total: Rs 450" in flow.reply
