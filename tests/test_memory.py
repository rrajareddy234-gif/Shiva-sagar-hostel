"""What the AI is told about the caller.

The memory block must be facts from the database. These tests pin the two
promises that matter: it carries no money, and it can never carry another
guest's data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import billing, booking, memory, payments, security
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")


def _stay(db, *, phone="+919000000901", name="Ramesh Kumar", days=3,
          bed="1NA-B1"):
    start = datetime.now(IST) + timedelta(days=1)
    return booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=days),
        food_included=True)


def test_a_known_guest_is_remembered(db):
    _stay(db)
    block = memory.memory_for(db, phone="+919000000901", role=security.CUSTOMER)
    assert "Ramesh Kumar" in block
    assert "SPG-" in block


def test_an_unknown_guest_is_stated_plainly(db):
    block = memory.memory_for(db, phone="+919000000999", role=security.CUSTOMER)
    assert "not spoken to this number before" in block


def test_memory_never_carries_an_amount(db):
    request = _stay(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=500, method="UPI", actor="owner", verified=True)
    block = memory.memory_for(db, phone="+919000000901", role=security.CUSTOMER)
    assert "500" not in block, "a balance must be computed live, never remembered"
    assert "Amounts are never quoted from memory" in block


def test_memory_never_carries_another_guest(db):
    _stay(db, phone="+919000000901", name="Ramesh Kumar")
    _stay(db, phone="+919000000902", name="Suresh Rao", days=2, bed="1NA-B2")
    block = memory.memory_for(db, phone="+919000000901", role=security.CUSTOMER)
    assert "Suresh Rao" not in block
    assert "+919000000902" not in block


def test_the_owner_block_holds_no_guest_names(db):
    _stay(db, phone="+919000000901", name="Ramesh Kumar")
    block = memory.memory_for(db, phone="+919999999999", role=security.OWNER)
    assert "Ramesh Kumar" not in block
    assert "Beds free now" in block


def test_a_staff_block_does_not_claim_owner_powers(db):
    block = memory.memory_for(db, phone="+919888888888", role=security.STAFF)
    assert "staff member" in block
    assert "only the owner verifies money" in block


def test_an_unconfirmed_request_is_flagged(db):
    _stay(db)
    block = memory.memory_for(db, phone="+919000000901", role=security.CUSTOMER)
    assert "REQUESTED" in block
    assert "not confirmed this request yet" in block
