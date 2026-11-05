"""Billing: extras on a stay, the booking advance, and invoices.

The tests here hold the three promises that matter:

  * an extra belongs to one stay and its price is frozen when added;
  * the advance counts toward the bill and is never refunded;
  * an invoice is a document whose lines never change after it is issued.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import billing, booking, payments
from core.errors import NotFound, PaymentError, ValidationError

IST = None  # set from the tz fixture


def _stay(db, seeded, tz, *, phone="+919876500001", terms="ADVANCE", guests=1):
    """One booking for `guests` people. A party needs one bed per guest, so the
    helper takes the first `guests` beds of the same room type."""
    start = datetime.now(tz) + timedelta(days=1)
    beds = db.query_all(
        "SELECT bed_id FROM beds WHERE room_id = '1NA' ORDER BY bed_id LIMIT %s",
        (guests,))
    bed_ids = [b["bed_id"] for b in beds]
    return booking.create_request(
        db, phone=phone, full_name="Test Guest", gender="MALE",
        bed_ids=bed_ids, check_in_at=start, check_out_at=start + timedelta(days=3),
        food_included=True, payment_terms=terms, guests_count=guests)


# --- the advance rule ------------------------------------------------------

def test_the_advance_defaults_to_one_hundred_per_guest(db, seeded, tz):
    assert billing.advance_per_guest(db) == 100
    assert billing.advance_due(db, 1) == 100
    assert billing.advance_due(db, 2) == 200


def test_the_owner_can_change_the_advance(db, seeded):
    billing.set_advance_per_guest(db, 150, actor="+919999999999")
    assert billing.advance_due(db, 3) == 450


def test_a_negative_advance_is_refused(db, seeded):
    with pytest.raises(ValidationError):
        billing.set_advance_per_guest(db, -50, actor="owner")


def test_the_notice_says_it_is_not_refundable(db, seeded):
    notice = billing.advance_notice(db, 2)
    assert "200" in notice
    assert "not refundable" in notice
    assert "full amount" in notice


def test_a_booking_records_its_advance(db, seeded, tz):
    request = _stay(db, seeded, tz, terms="ADVANCE", guests=2)
    assert request.advance_due == 200
    assert request.payment_terms == "ADVANCE"


def test_a_full_payment_booking_owes_no_advance(db, seeded, tz):
    request = _stay(db, seeded, tz, terms="FULL", guests=2)
    assert request.advance_due == 0


def test_bad_payment_terms_are_refused(db, seeded, tz):
    with pytest.raises(ValidationError):
        _stay(db, seeded, tz, terms="HALF")


# --- extras on a stay ------------------------------------------------------

def test_an_extra_is_added_and_priced_from_the_shop(db, seeded, tz):
    request = _stay(db, seeded, tz)
    result = billing.add_extra(db, booking_id=request.booking_id, name="Towel",
                               actor="+919999999999")
    assert result["amount"] == "50.00"
    assert billing.booking_extras_total(db, request.booking_id) == 50


def test_an_extra_price_is_frozen_when_added(db, seeded, tz):
    request = _stay(db, seeded, tz)
    billing.add_extra(db, booking_id=request.booking_id, name="Towel", actor="owner")
    # The shop raises its price afterwards.
    db.execute("UPDATE extras SET price = 999 WHERE name = 'Towel'")
    total = billing.booking_extras_total(db, request.booking_id)
    assert total == 50, "a later price change must not rewrite an old bill"


def test_an_unknown_extra_is_refused(db, seeded, tz):
    request = _stay(db, seeded, tz)
    with pytest.raises(NotFound):
        billing.add_extra(db, booking_id=request.booking_id, name="Chocolate",
                          actor="owner")


def test_a_zero_quantity_is_refused(db, seeded, tz):
    request = _stay(db, seeded, tz)
    with pytest.raises(ValidationError):
        billing.add_extra(db, booking_id=request.booking_id, name="Towel",
                          quantity=0, actor="owner")


def test_extras_multiply_by_quantity(db, seeded, tz):
    request = _stay(db, seeded, tz)
    billing.add_extra(db, booking_id=request.booking_id, name="Towel",
                      quantity=2, actor="owner")
    assert billing.booking_extras_total(db, request.booking_id) == 100


# --- invoices --------------------------------------------------------------

def test_an_invoice_has_a_number_and_lines(db, seeded, tz):
    request = _stay(db, seeded, tz)
    billing.add_extra(db, booking_id=request.booking_id, name="Towel", actor="owner")
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")

    assert invoice.number.startswith("INV-")
    kinds = [line["kind"] for line in invoice.lines]
    assert "ROOM" in kinds and "EXTRA" in kinds
    assert invoice.total == 800  # 250 x 3 nights + 50 towel


def test_an_invoice_shows_the_advance_as_non_refundable(db, seeded, tz):
    request = _stay(db, seeded, tz, terms="ADVANCE", guests=2)
    payments.record_payment(
        db, booking_id=request.booking_id, guest_id=None, amount=200,
        method="UPI", actor="owner", verified=True, refundable=False)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")

    assert invoice.non_refundable == 200
    assert invoice.paid == 200
    assert invoice.balance_due == invoice.total - 200


def test_reissuing_returns_the_same_invoice(db, seeded, tz):
    request = _stay(db, seeded, tz)
    first = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    second = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    assert first.number == second.number


def test_invoice_lines_never_change_after_issue(db, seeded, tz):
    request = _stay(db, seeded, tz)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    before = [(line["description"], line["amount"]) for line in invoice.lines]

    # A late extra must not alter the issued document.
    billing.add_extra(db, booking_id=request.booking_id, name="Towel", actor="owner")
    after = billing.get_invoice(db, invoice.number)
    assert [(line["description"], line["amount"]) for line in after.lines] == before


def test_a_discount_reduces_the_bill(db, seeded, tz):
    request = _stay(db, seeded, tz)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner",
                                    discount=50)
    assert invoice.total == 700
    assert any(line["kind"] == "DISCOUNT" for line in invoice.lines)


def test_a_discount_bigger_than_the_bill_is_refused(db, seeded, tz):
    request = _stay(db, seeded, tz)
    with pytest.raises(ValidationError):
        billing.issue_invoice(db, booking_id=request.booking_id, actor="owner",
                              discount=99999)


def test_an_invoice_cannot_be_settled_while_money_is_owed(db, seeded, tz):
    request = _stay(db, seeded, tz)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    with pytest.raises(PaymentError):
        billing.settle_invoice(db, invoice.invoice_id, actor="owner")


def test_an_invoice_settles_once_the_money_is_verified(db, seeded, tz):
    request = _stay(db, seeded, tz)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    payments.record_payment(
        db, booking_id=request.booking_id, guest_id=None, amount=invoice.total,
        method="CASH", actor="owner", verified=True)
    settled = billing.settle_invoice(db, invoice.invoice_id, actor="owner")
    assert settled.status == "PAID"
    assert settled.settled


def test_the_rendered_bill_is_plain_text(db, seeded, tz):
    request = _stay(db, seeded, tz)
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    text = billing.render_invoice(invoice)
    assert "INV-" in text
    assert "Total:" in text
    assert "Balance due:" in text


def test_a_guest_bill_never_leaks_another_guest(db, seeded, tz):
    """The rendered bill carries no other guest's name or phone."""
    request = _stay(db, seeded, tz, phone="+919876500001")
    invoice = billing.issue_invoice(db, booking_id=request.booking_id, actor="owner")
    text = billing.render_invoice(invoice)
    assert "+919876500001" not in text
    assert "Test Guest" not in text
