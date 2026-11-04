"""Staff, owner lookup and the guest's own bill.

The line these tests defend: staff can record money but never verify it, and a
guest can only ever see their own bill.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import billing, booking, payments, router, security, tools
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
STAFF = "+919888888888"


def _now():
    return datetime.now(IST)


def _request(db, *, phone="+919000000901", name="Ramesh Kumar", bed="1NA-B1",
             days=3, terms="ADVANCE", guests=1):
    start = _now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=days),
        food_included=True, payment_terms=terms, guests_count=guests)


def _confirmed(db, **kw):
    request = _request(db, **kw)
    booking.approve(db, request.booking_id, actor=OWNER)
    return request


# --- roles -----------------------------------------------------------------

def test_a_staff_number_is_not_the_owner():
    assert security.role_for(STAFF, OWNER, {STAFF}) == security.STAFF
    assert security.role_for(STAFF, OWNER) == security.CUSTOMER


def test_a_staff_member_cannot_see_owner_tools():
    names = [t.name for t in tools.TOOLS if t.allows(security.STAFF)]
    assert "who_is" not in names
    assert "hostel_summary" not in names


def test_a_staff_member_can_record_and_add_but_not_verify():
    names = [t.name for t in tools.TOOLS if t.allows(security.STAFF)]
    assert "record_payment" in names
    assert "add_extra" in names
    assert "issue_invoice" in names


def test_a_customer_cannot_reach_a_staff_tool():
    names = [t.name for t in tools.TOOLS if t.allows(security.CUSTOMER)]
    assert "record_payment" not in names
    assert "add_extra" not in names
    assert "issue_invoice" not in names
    assert "who_is" not in names


def test_the_owner_sees_every_tool():
    names = [t.name for t in tools.TOOLS if t.allows(security.OWNER)]
    assert {"who_is", "record_payment", "add_extra", "issue_invoice"} <= set(names)


# --- staff view ------------------------------------------------------------

def test_the_staff_menu_offers_taps(db):
    reply = router.staff_answer(db, "menu")
    assert reply.handled
    assert reply.prompt is not None
    assert reply.prompt.is_valid()


def test_staff_see_the_free_beds(db):
    reply = router.staff_answer(db, "how many beds are free?")
    assert reply.handled
    assert "free" in reply.text


def test_staff_see_todays_arrivals(db):
    _confirmed(db)
    reply = router.staff_answer(db, "who is arriving today?")
    assert reply.handled
    assert "arrival" in reply.text.lower()


def test_a_staff_answer_never_shows_revenue(db):
    _confirmed(db)
    reply = router.staff_answer(db, "what is the full status?")
    assert "revenue" not in reply.text.lower()


# --- recording a payment ---------------------------------------------------

def test_recording_a_payment_leaves_it_unverified(db):
    request = _confirmed(db)
    ctx = tools.ToolContext(db=db, phone=STAFF, role=security.STAFF)
    result = tools.execute(ctx, "record_payment",
                           {"reference": request.reference, "amount": "200",
                            "method": "UPI"})
    assert result["verified"] is False
    assert "NOT verified" in result["note"]

    balance = payments.get_balance(db, request.booking_id)
    assert balance.verified_paid == 0, "unverified money must not count as paid"
    assert balance.unverified_pending == 200


def test_an_advance_is_recorded_as_not_refundable(db):
    request = _confirmed(db, terms="ADVANCE", guests=2)
    ctx = tools.ToolContext(db=db, phone=STAFF, role=security.STAFF)
    tools.execute(ctx, "record_payment",
                  {"reference": request.reference, "amount": "200",
                   "method": "UPI", "refundable": False})
    row = db.query_one("SELECT refundable FROM payments WHERE booking_id = %s",
                       (request.booking_id,))
    assert row["refundable"] is False


def test_a_staff_tool_refuses_the_owner_lookup(db):
    ctx = tools.ToolContext(db=db, phone=STAFF, role=security.STAFF)
    result = tools.execute(ctx, "who_is", {"query": "Ramesh"})
    assert "error" in result


# --- the guest's own bill --------------------------------------------------

def test_my_bill_shows_the_guest_their_balance(db):
    request = _confirmed(db, days=3)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=200, method="UPI", actor=OWNER, verified=True)
    ctx = tools.ToolContext(db=db, phone="+919000000901", role=security.CUSTOMER)
    result = tools.execute(ctx, "my_bill", {})
    assert result["balance_due"] == "550.00"   # 750 bill, 200 paid
    assert result["verified_paid"] == "200.00"


def test_my_bill_includes_the_extras(db):
    request = _confirmed(db)
    billing.add_extra(db, booking_id=request.booking_id, name="Towel", actor=OWNER)
    ctx = tools.ToolContext(db=db, phone="+919000000901", role=security.CUSTOMER)
    result = tools.execute(ctx, "my_bill", {})
    assert any(e["name"] == "Towel" for e in result["extras"])


def test_my_bill_shows_no_other_guest(db):
    _confirmed(db, phone="+919000000901", name="Ramesh Kumar", bed="1NA-B1")
    _confirmed(db, phone="+919000000902", name="Suresh Rao", bed="1NA-B2")
    ctx = tools.ToolContext(db=db, phone="+919000000901", role=security.CUSTOMER)
    result = tools.execute(ctx, "my_bill", {})
    assert "Suresh" not in str(result)
    assert "+919000000902" not in str(result)


# --- owner lookup ----------------------------------------------------------

def test_the_owner_can_look_up_a_guest_by_name(db):
    _confirmed(db)
    reply = router.owner_answer(db, "who is Ramesh")
    assert reply.handled
    assert "Ramesh Kumar" in reply.text
    assert "SPG-" in reply.text


def test_the_owner_can_look_up_a_guest_by_number(db):
    _confirmed(db)
    reply = router.owner_answer(db, "who is 9000000901")
    assert reply.handled
    assert "Ramesh Kumar" in reply.text


def test_an_unknown_guest_lookup_says_so(db):
    reply = router.owner_answer(db, "who is Nobody At All")
    assert reply.handled
    assert "No guest matches" in reply.text


# --- the guest list --------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "guest details", "guest full details", "customer details",
    "customer full details", "list guests", "all guests", "show guests",
    "how many guests", "guests", "customers", "guest data", "guest summary",
])
def test_the_owner_can_ask_for_the_guest_list(db, phrase):
    """Every one of these must list the guests, not search for a person."""
    _confirmed(db, name="Ramesh Kumar")
    reply = router.owner_answer(db, phrase)
    assert reply.handled, phrase
    assert reply.intent == "owner_guests", phrase
    assert "Ramesh Kumar" in reply.text
    assert "No guest matches" not in reply.text


def test_the_guest_list_shows_the_bill_and_the_balance(db):
    request = _confirmed(db, days=3)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=200, method="UPI", actor=OWNER, verified=True)
    reply = router.owner_answer(db, "guest details")
    assert "Bill Rs 750" in reply.text
    assert "paid Rs 200" in reply.text
    assert "due Rs 550" in reply.text


def test_the_guest_list_puts_a_live_booking_first(db):
    # Suresh is created last, so he would lead on recency alone. Ramesh has a
    # live booking, so he must lead instead.
    _confirmed(db, phone="+919000000901", name="Ramesh Kumar", bed="1NA-B1")
    old = _request(db, phone="+919000000902", name="Suresh Rao", bed="1NA-B2")
    booking.reject(db, old.booking_id, actor=OWNER)
    reply = router.owner_answer(db, "guest details")
    assert reply.text.index("Ramesh") < reply.text.index("Suresh")


def test_the_guest_list_is_empty_and_plain_when_there_are_no_guests(db):
    reply = router.owner_answer(db, "guest details")
    assert reply.handled
    assert "No guests on the books yet" in reply.text


def test_a_name_after_guest_is_still_a_lookup_not_a_list(db):
    _confirmed(db, name="Ramesh Kumar")
    reply = router.owner_answer(db, "guest Ramesh")
    assert reply.intent == "owner_guest"
    assert "Ramesh Kumar" in reply.text


def test_the_owner_can_see_a_bill(db):
    request = _confirmed(db)
    reply = router.owner_answer(db, f"show bill {request.reference}")
    assert reply.handled
    assert "INV-" in reply.text
    assert "Balance due" in reply.text


def test_the_owner_lookup_tool_reports_the_balance(db):
    request = _confirmed(db, days=3)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=300, method="CASH", actor=OWNER, verified=True)
    ctx = tools.ToolContext(db=db, phone=OWNER, role=security.OWNER)
    result = tools.execute(ctx, "who_is", {"query": "Ramesh"})
    assert result["found"] == 1
    stay = result["guests"][0]["bookings"][0]
    assert stay["bill"] == "750.00"
    assert stay["balance_due"] == "450.00"
