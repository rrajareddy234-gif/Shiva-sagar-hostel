"""The owner's dashboard: the numbers, the search, and the lock on the door.

The line these tests defend: the page shows only what the database says, it is
never reachable without the password, and it changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import booking, dashboard, export
from tests.conftest import DASHBOARD_PASSWORD, IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
GUEST = "+919000000901"


def _now():
    return datetime.now(IST)


def _guest(db, *, name="Ramesh Kumar", phone=GUEST, bed="1NA-B1"):
    from core import booking

    start = _now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    request = booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=3),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    return request


# --- the lock on the door --------------------------------------------------

def test_the_password_is_owner_set_and_never_a_known_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "sagar-secret")
    assert dashboard.password_is_ok("sagar-secret")
    assert not dashboard.password_is_ok("sagar")
    assert not dashboard.password_is_ok(None)


def test_an_unset_password_is_random_not_a_guessable_default(monkeypatch):
    # A fixed default would be readable in a public repository and would open
    # every guest's details to anyone who tried it.
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    dashboard._generated_password.cache_clear()
    chosen = dashboard.dashboard_password()
    assert chosen and chosen != "sagar"
    assert dashboard.password_is_ok(chosen)
    assert not dashboard.password_is_ok("sagar")
    # And it is stable within the run, so the owner can log in with it.
    assert dashboard.dashboard_password() == chosen


def test_the_dashboard_asks_for_a_password_first(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Password" in response.text
    # The guest table must not leak before the password is given.
    assert "Ramesh" not in response.text


def test_a_wrong_password_is_refused(client):
    response = client.post("/dashboard/login", data={"password": "nope"})
    assert response.status_code == 401
    assert "not right" in response.text


def test_the_right_password_opens_the_dashboard(client, db):
    _guest(db)
    response = client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD},
                           follow_redirects=True)
    assert response.status_code == 200
    assert "Ramesh Kumar" in response.text
    assert "Sagar PG Hostel" in response.text


def test_logging_out_locks_the_page_again(client, db):
    _guest(db)
    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    assert "Ramesh" in client.get("/dashboard").text
    client.get("/dashboard/logout", follow_redirects=True)
    assert "Ramesh" not in client.get("/dashboard").text


# --- what the page shows ---------------------------------------------------

def test_the_page_shows_the_numbers_the_owner_cares_about(db):
    _guest(db)
    numbers = dashboard.overview(db, _now())
    assert numbers["beds_total"] == 10
    # The stay starts tomorrow, so nothing is occupied yet.
    assert numbers["beds_free"] == 10
    assert numbers["in_house"] == 0
    assert numbers["pending"] == 0


def test_a_guest_staying_now_counts_as_occupied(db):
    from core import booking

    start = _now() - timedelta(days=1)
    request = booking.create_request(
        db, phone=GUEST, full_name="Ramesh Kumar", gender="MALE", bed_id="1NA-B1",
        check_in_at=start, check_out_at=start + timedelta(days=3), food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    numbers = dashboard.overview(db, _now())
    assert numbers["in_house"] == 1
    assert numbers["beds_free"] == 9


def test_a_waiting_booking_is_counted_and_listed(db):
    from core import booking

    start = _now() + timedelta(days=2)
    booking.create_request(
        db, phone=GUEST, full_name="Ramesh Kumar", gender="MALE", bed_id="1NA-B1",
        check_in_at=start, check_out_at=start + timedelta(days=2), food_included=True)
    assert dashboard.overview(db, _now())["pending"] == 1
    waiting = dashboard.pending_requests(db)
    assert waiting[0]["name"] == "Ramesh Kumar"
    assert waiting[0]["bed"] == "1NA-B1"
    # Non-AC with food is 250 a night for two nights.
    assert waiting[0]["amount"] == "500"


def test_the_guest_table_carries_the_bill_and_the_balance(db):
    from core import payments

    request = _guest(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=200, method="UPI", actor=OWNER, verified=True)
    rows = dashboard.guests(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "Ramesh Kumar"
    assert row.room == "1NA"
    assert row.bill == "750"
    assert row.paid == "200"
    assert row.due == "550"


def test_the_search_finds_a_guest_by_name_or_phone(db):
    _guest(db)
    assert len(dashboard.guests(db, search="ramesh")) == 1
    assert len(dashboard.guests(db, search="9000000901")) == 1
    assert dashboard.guests(db, search="nobody") == []


def test_an_empty_search_returns_everyone(db):
    _guest(db, name="Ramesh Kumar", phone=GUEST, bed="1NA-B1")
    _guest(db, name="Sita Devi", phone="+919000000902", bed="2AC-B1")
    assert len(dashboard.guests(db)) == 2


def test_the_owner_and_staff_numbers_are_not_guests(db):
    db.execute("INSERT INTO guests (full_name, phone, is_owner) VALUES ('Me', %s, true)",
               (OWNER,))
    db.execute("INSERT INTO guests (full_name, phone, is_staff) VALUES ('Desk', %s, true)",
               ("+919888888888",))
    assert dashboard.guests(db) == []


# --- the page changes nothing ----------------------------------------------

def test_opening_the_dashboard_does_not_change_any_data(db):
    _guest(db)
    before = export.customer_file(db, _now()).content
    dashboard.overview(db, _now())
    dashboard.guests(db)
    assert export.customer_file(db, _now()).content == before


# --- approving and rejecting from the page ---------------------------------

def _waiting(db, *, name="Vikram Rao", phone="+919000000905", bed="6NA-B2"):
    from core import booking

    start = _now() + timedelta(days=3)
    return booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=2), food_included=True)


def test_the_owner_can_approve_a_booking_from_the_page(db):
    request = _waiting(db)
    result = dashboard.approve_booking(db, request.reference, actor="owner-dashboard")
    # The first approval accepts the booking and asks the guest to pay; the
    # booking is still waiting on the owner, for the payment check.
    assert result["status"] == "ACCEPTED"
    assert dashboard.guests(db)[0].status == "ACCEPTED"


def test_the_owner_confirms_from_the_page_after_the_payment(db):
    request = _waiting(db)
    dashboard.approve_booking(db, request.reference, actor="owner-dashboard")
    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    result = dashboard.approve_booking(db, request.reference, actor="owner-dashboard")
    assert result["status"] == "CONFIRMED"
    assert dashboard.overview(db, _now())["pending"] == 0
    assert dashboard.guests(db)[0].status == "CONFIRMED"


def test_the_owner_can_reject_a_booking_from_the_page(db):
    request = _waiting(db)
    result = dashboard.reject_booking(db, request.reference, actor="owner-dashboard")
    assert result["status"] == "REJECTED"
    assert dashboard.overview(db, _now())["pending"] == 0


def test_a_decision_is_recorded_in_the_audit_log(db):
    request = _waiting(db)
    dashboard.approve_booking(db, request.reference, actor="owner-dashboard")
    row = db.query_one(
        "SELECT actor, action FROM audit_log WHERE action = 'booking_accepted' "
        "ORDER BY at DESC LIMIT 1")
    assert row["actor"] == "owner-dashboard"


def test_an_unknown_reference_is_refused_clearly(db):
    from core.errors import NotFound

    with pytest.raises(NotFound):
        dashboard.approve_booking(db, "SPG-2026-999999", actor="owner-dashboard")


def test_a_bed_taken_while_waiting_is_not_silently_double_booked(db):
    """The exclusion constraint and the accept check both hold on this path."""
    from core.errors import BedUnavailable

    first = _waiting(db, name="Vikram Rao", phone="+919000000905", bed="6NA-B2")
    dashboard.approve_booking(db, first.reference, actor="owner-dashboard")
    booking.mark_proof_sent(db, first.booking_id, proof_path="/tmp/p.jpg")
    dashboard.approve_booking(db, first.reference, actor="owner-dashboard")

    second = _waiting(db, name="Anil Kumar", phone="+919000000906", bed="6NA-B2")
    with pytest.raises(BedUnavailable):
        dashboard.approve_booking(db, second.reference, actor="owner-dashboard")


def test_the_buttons_only_appear_for_the_owner(client, db):
    _waiting(db)
    assert "Reject" not in client.get("/dashboard").text
    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    page = client.get("/dashboard").text
    assert "Accept" in page and "Reject" in page


def test_the_page_says_which_step_each_waiting_booking_is_at(client, db):
    """A fresh request and a payment check must not look the same to the owner."""
    fresh = _waiting(db, name="Fresh Guest", phone="+919000000911", bed="6NA-B1")
    paid = _waiting(db, name="Paid Guest", phone="+919000000912", bed="6NA-B2")
    dashboard.approve_booking(db, paid.reference, actor="owner-dashboard")
    booking.mark_proof_sent(db, paid.booking_id, proof_path="/tmp/p.jpg")

    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    page = client.get("/dashboard").text
    assert "New request - ask to pay" in page
    assert "Payment sent - check it" in page
    # The two rows offer the two different decisions, by name.
    assert "Accept" in page and "Confirm" in page
    assert fresh.reference in page and paid.reference in page


def test_a_tap_without_the_password_changes_nothing(client, db):
    request = _waiting(db)
    response = client.post("/dashboard/decide",
                           data={"reference": request.reference,
                                 "decision": "approve"},
                           follow_redirects=False)
    # Sent back to the login page, and the booking is untouched.
    assert response.status_code == 303
    assert dashboard.overview(db, _now())["pending"] == 1


def test_a_tap_from_the_owner_accepts_and_says_so(client, db):
    request = _waiting(db)
    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    response = client.post("/dashboard/decide",
                           data={"reference": request.reference,
                                 "decision": "approve"}, follow_redirects=True)
    assert "is accepted" in response.text
    # Nothing is waiting on the owner now: the guest owes the payment. The
    # booking comes back to the owner's list when the screenshot arrives.
    assert dashboard.overview(db, _now())["pending"] == 0


def test_a_tap_on_a_bed_that_was_taken_shows_a_clear_message(client, db):
    first = _waiting(db, name="Vikram Rao", phone="+919000000905", bed="6NA-B2")
    dashboard.approve_booking(db, first.reference, actor="owner-dashboard")
    booking.mark_proof_sent(db, first.booking_id, proof_path="/tmp/p.jpg")
    dashboard.approve_booking(db, first.reference, actor="owner-dashboard")
    second = _waiting(db, name="Anil Kumar", phone="+919000000906", bed="6NA-B2")

    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    response = client.post("/dashboard/decide",
                           data={"reference": second.reference,
                                 "decision": "approve"}, follow_redirects=True)
    assert "taken" in response.text.lower()
    # The second booking is still waiting, not wrongly confirmed.
    assert dashboard.overview(db, _now())["pending"] == 1
