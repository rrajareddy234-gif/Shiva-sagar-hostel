"""Payment ledger tests.

The governing rule: a screenshot is not proof. Unverified money must never count
towards revenue or a settled balance.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core import booking, payments
from core.errors import NotFound, PaymentError, ValidationError

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"


def _confirmed_booking(db, phone="+919000000500", charges=None, bed_id="1NA-B1"):
    from tests.conftest import IST
    start = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    request = booking.create_request(
        db, phone=phone, full_name="Dummy Guest", gender="MALE",
        bed_id=bed_id, check_in_at=start, check_out_at=start + timedelta(days=2),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    if charges is not None:
        db.execute("UPDATE bookings SET charges_total = %s WHERE booking_id = %s",
                   (charges, request.booking_id))
    return request.booking_id


# --- recording -------------------------------------------------------------

def test_record_unverified_payment(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(
        db, booking_id=booking_id, guest_id=None, amount=200, method="UPI",
        actor="+919000000500", txn_ref="UPI-TEST-1")
    row = db.query_one("SELECT * FROM payments WHERE payment_id = %s", (payment_id,))
    assert row["verified"] is False
    assert row["verified_by"] is None


def test_unverified_payment_does_not_count_towards_balance(db):
    """A screenshot-backed claim leaves the balance due untouched."""
    booking_id = _confirmed_booking(db)          # 700.00
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=700,
                            method="UPI", actor="+919000000500", txn_ref="UPI-TEST-2")
    balance = payments.get_balance(db, booking_id)
    assert balance.verified_paid == Decimal("0.00")
    assert balance.unverified_pending == Decimal("700.00")
    assert balance.balance_due == Decimal("700.00")
    assert balance.settled is False


def test_verified_full_payment_settles_the_balance(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(
        db, booking_id=booking_id, guest_id=None, amount=700, method="UPI",
        actor="+919000000500", txn_ref="UPI-TEST-3")
    payments.verify_payment(db, payment_id, actor=OWNER)
    balance = payments.get_balance(db, booking_id)
    assert balance.verified_paid == Decimal("700.00")
    assert balance.balance_due == Decimal("0.00")
    assert balance.settled is True


def test_partial_payment_leaves_outstanding_balance(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(
        db, booking_id=booking_id, guest_id=None, amount=300, method="CASH",
        actor=OWNER)
    payments.verify_payment(db, payment_id, actor=OWNER)
    balance = payments.get_balance(db, booking_id)
    assert balance.verified_paid == Decimal("300.00")
    assert balance.balance_due == Decimal("400.00")


def test_advance_then_final_payment(db):
    booking_id = _confirmed_booking(db)
    advance = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                      amount=200, method="UPI", actor=OWNER,
                                      txn_ref="UPI-ADV")
    payments.verify_payment(db, advance, actor=OWNER)
    final = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                    amount=500, method="CASH", actor=OWNER)
    payments.verify_payment(db, final, actor=OWNER)
    assert payments.get_balance(db, booking_id).settled is True


def test_payment_can_be_recorded_already_verified_by_a_gateway(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(
        db, booking_id=booking_id, guest_id=None, amount=700, method="GATEWAY",
        actor="gateway", txn_ref="GATEWAY-1", verified=True)
    assert db.query_one("SELECT verified FROM payments WHERE payment_id = %s",
                        (payment_id,))["verified"] is True


# --- validation ------------------------------------------------------------

def test_zero_amount_rejected(db):
    booking_id = _confirmed_booking(db)
    with pytest.raises(ValidationError, match="cannot be zero"):
        payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=0,
                                method="CASH", actor=OWNER)


def test_negative_payment_rejected(db):
    booking_id = _confirmed_booking(db)
    with pytest.raises(ValidationError, match="positive amount"):
        payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=-100,
                                method="CASH", actor=OWNER)


def test_positive_refund_rejected(db):
    booking_id = _confirmed_booking(db)
    with pytest.raises(ValidationError, match="negative amount"):
        payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                                method="CASH", actor=OWNER, kind="REFUND")


def test_unknown_method_rejected(db):
    booking_id = _confirmed_booking(db)
    with pytest.raises(ValidationError, match="unknown payment method"):
        payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                                method="BITCOIN", actor=OWNER)


def test_payment_for_unknown_booking_rejected(db):
    with pytest.raises(NotFound):
        payments.record_payment(db, booking_id="00000000-0000-0000-0000-000000000000",
                                guest_id=None, amount=100, method="CASH", actor=OWNER)


# --- duplicates and verification -------------------------------------------

def test_duplicate_transaction_reference_rejected(db):
    booking_id = _confirmed_booking(db)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                            method="UPI", actor=OWNER, txn_ref="UPI-DUP-1")
    with pytest.raises(PaymentError, match="already been recorded"):
        payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                                method="UPI", actor=OWNER, txn_ref="UPI-DUP-1")


def test_multiple_cash_payments_without_a_reference_are_allowed(db):
    booking_id = _confirmed_booking(db)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                            method="CASH", actor=OWNER)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=100,
                            method="CASH", actor=OWNER)
    assert len(db.query_all("SELECT 1 FROM payments WHERE booking_id = %s", (booking_id,))) == 2


def test_verifying_twice_rejected(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                         amount=100, method="CASH", actor=OWNER)
    payments.verify_payment(db, payment_id, actor=OWNER)
    with pytest.raises(PaymentError, match="already verified"):
        payments.verify_payment(db, payment_id, actor=OWNER)


def test_verify_unknown_payment_raises(db):
    with pytest.raises(NotFound):
        payments.verify_payment(db, "00000000-0000-0000-0000-000000000000", actor=OWNER)


# --- refunds and revenue ---------------------------------------------------

def test_refund_reduces_the_verified_total(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                         amount=700, method="UPI", actor=OWNER,
                                         txn_ref="UPI-REF")
    payments.verify_payment(db, payment_id, actor=OWNER)
    refund_id = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                        amount=-200, method="UPI", actor=OWNER,
                                        kind="REFUND", txn_ref="UPI-REF-REV")
    payments.verify_payment(db, refund_id, actor=OWNER)
    balance = payments.get_balance(db, booking_id)
    assert balance.verified_paid == Decimal("500.00")
    assert balance.balance_due == Decimal("200.00")


def test_unverified_refund_is_ignored_in_the_balance(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                         amount=700, method="UPI", actor=OWNER,
                                         txn_ref="UPI-R2")
    payments.verify_payment(db, payment_id, actor=OWNER)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=-100,
                            method="UPI", actor=OWNER, kind="REFUND", txn_ref="UPI-R2-REV")
    assert payments.get_balance(db, booking_id).balance_due == Decimal("0.00")


def test_revenue_counts_only_verified_payments(db):
    booking_id = _confirmed_booking(db)
    verified = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                       amount=500, method="CASH", actor=OWNER)
    payments.verify_payment(db, verified, actor=OWNER)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=200,
                            method="UPI", actor=OWNER, txn_ref="UPI-UNVERIFIED")

    now = datetime.now()
    revenue = payments.revenue_between(db, now - timedelta(hours=1), now + timedelta(hours=1))
    assert revenue == Decimal("500.00")


# --- queries for the owner -------------------------------------------------

def test_outstanding_bookings_excludes_settled_ones(db):
    settled_id = _confirmed_booking(db, phone="+919000000600", bed_id="1NA-B1")
    owing_id = _confirmed_booking(db, phone="+919000000601", bed_id="2AC-B1")

    payment = payments.record_payment(db, booking_id=settled_id, guest_id=None,
                                      amount=700, method="CASH", actor=OWNER)
    payments.verify_payment(db, payment, actor=OWNER)

    outstanding = payments.outstanding_bookings(db)
    ids = {str(row["booking_id"]) for row in outstanding}
    assert owing_id in ids
    assert settled_id not in ids


def test_unverified_payments_listed_for_the_owner(db):
    booking_id = _confirmed_booking(db)
    payments.record_payment(db, booking_id=booking_id, guest_id=None, amount=250,
                            method="UPI", actor="+919000000500", txn_ref="UPI-PENDING")
    pending = payments.unverified_payments(db)
    assert len(pending) == 1
    assert pending[0]["amount"] == Decimal("250.00")


def test_audit_trail_records_payment_and_verification(db):
    booking_id = _confirmed_booking(db)
    payment_id = payments.record_payment(db, booking_id=booking_id, guest_id=None,
                                         amount=100, method="CASH", actor=OWNER)
    payments.verify_payment(db, payment_id, actor=OWNER)
    actions = [row["action"] for row in db.query_all(
        "SELECT action FROM audit_log WHERE entity = 'payment' AND entity_id = %s "
        "ORDER BY id", (payment_id,))]
    assert actions == ["payment_recorded", "payment_verified"]
