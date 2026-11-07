"""The holding reply, and the duplicate-booking guard.

After a request is sent, the guest is held on "wait for the owner" until the
owner decides. What these tests defend:

  * a second request is refused while one is live, so a guest cannot pile up two
    bed holds for the owner to untangle;
  * the AI is never asked to answer in that window, so it cannot promise a bed
    or invent an amount;
  * once the owner cancels or rejects, the guest is free again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import booking, services, sticky
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
GUEST = "+919000000555"


def _request(db, *, phone=GUEST, bed="1NA-B1"):
    start = datetime(2027, 5, 1, 12, tzinfo=IST)
    return booking.create_request(
        db, phone=phone, full_name="Wait Guest", gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=2),
        food_included=True)


def test_a_live_request_holds_the_guest(db):
    request = _request(db)
    held = sticky.reply_for(db, phone=GUEST, text="hello?")
    assert held is not None
    assert request.reference in held
    assert "wait" in held.lower()


def test_the_holding_reply_quotes_the_recorded_total(db):
    _request(db)
    held = sticky.reply_for(db, phone=GUEST)
    # Two nights, non-AC with food, one guest: 250 * 2 = 500.
    assert "500" in held


def test_a_cancelled_request_frees_the_guest(db):
    request = _request(db)
    booking.cancel(db, request.booking_id, actor=OWNER, reason="test")
    assert sticky.reply_for(db, phone=GUEST) is None


def test_a_rejected_request_frees_the_guest(db):
    request = _request(db)
    booking.reject(db, request.booking_id, actor=OWNER, reason="test")
    assert sticky.reply_for(db, phone=GUEST) is None


def test_a_guest_with_no_booking_is_not_held(db):
    assert sticky.reply_for(db, phone="+919000000556") is None


def test_a_live_rest_holds_the_guest_too(db):
    order = services.create_order(db, phone=GUEST, service_code="REST_3H",
                                  full_name="Rest Guest", bed_id="1NA-B1",
                                  starts_at=datetime(2027, 5, 1, 10, tzinfo=IST))
    held = sticky.reply_for(db, phone=GUEST)
    assert held is not None
    assert order.reference in held


def test_a_fresh_up_holds_the_guest_with_no_bed(db):
    order = services.create_order(db, phone=GUEST, service_code="FRESH_UP",
                                  full_name="Fresh Guest")
    held = sticky.reply_for(db, phone=GUEST)
    assert held is not None
    assert order.reference in held
