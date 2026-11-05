"""Booking state machine tests.

The important ones are the races: two guests requesting the same bed at the same
time, and an approval that must not confirm a bed that was taken meanwhile.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from core import availability, booking
from core.errors import BedUnavailable, InvalidTransition, NotFound, ValidationError

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"


def _request(db, bed_id="1NA-B1", phone="+919000000100", **kwargs):
    start = kwargs.pop("start", None) or booking_start()
    end = kwargs.pop("end", None) or (start + timedelta(days=2))
    kwargs.setdefault("food_included", True)
    return booking.create_request(
        db, phone=phone, full_name="Dummy Guest", gender="MALE",
        bed_id=bed_id, check_in_at=start, check_out_at=end, **kwargs,
    )


def booking_start():
    from datetime import datetime

    from tests.conftest import IST
    return (datetime.now(IST) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)


# --- happy path ------------------------------------------------------------

def test_request_creates_requested_booking_and_hold(db):
    request = _request(db, food_included=True)
    assert request.status == "REQUESTED"
    assert request.reference.startswith("SPG-")
    assert request.hold_id is not None
    # Two nights in a non-AC room with food at the owner's 250 a night.
    assert request.charges_total == Decimal("500.00")
    hold = db.query_one("SELECT status FROM bed_holds WHERE hold_id = %s", (request.hold_id,))
    assert hold["status"] == "ACTIVE"


def test_request_without_food_uses_the_no_food_rate(db):
    """Every room type offers both; a no-food request is priced on its own rate."""
    request = _request(db, bed_id="DORM-M1", food_included=False)
    assert request.charges_total == Decimal("300.00")


def test_omitting_the_food_choice_is_refused(db):
    """Every room type offers both food options, so the choice cannot be guessed."""
    with pytest.raises(ValidationError, match="both food options"):
        _request(db, food_included=None)


def test_request_is_never_auto_confirmed(db):
    request = _request(db)
    row = db.query_one("SELECT status FROM bookings WHERE booking_id = %s",
                       (request.booking_id,))
    assert row["status"] == "REQUESTED"


def test_approved_request_becomes_confirmed(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    assert row["status"] == "CONFIRMED"
    assert row["approved_by"] == OWNER
    assert row["approved_at"] is not None


def test_approval_converts_the_hold(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    hold = db.query_one("SELECT status FROM bed_holds WHERE hold_id = %s", (request.hold_id,))
    assert hold["status"] == "CONVERTED"


def test_rejection_releases_the_bed(db):
    request = _request(db)
    booking.reject(db, request.booking_id, actor=OWNER, reason="no vacancy that week")
    row = booking.get_booking(db, request.booking_id)
    assert row["status"] == "REJECTED"
    hold = db.query_one("SELECT status FROM bed_holds WHERE hold_id = %s", (request.hold_id,))
    assert hold["status"] == "RELEASED"
    assert availability.bed_is_available(db, "1NA-B1", row["check_in_at"], row["check_out_at"])


def test_cancellation_releases_the_bed(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.cancel(db, request.booking_id, actor=OWNER, reason="guest changed plans")
    row = booking.get_booking(db, request.booking_id)
    assert row["status"] == "CANCELLED"
    assert availability.bed_is_available(db, "1NA-B1", row["check_in_at"], row["check_out_at"])


# --- validation ------------------------------------------------------------

def test_missing_phone_rejected(db):
    with pytest.raises(ValidationError):
        _request(db, phone="")


def test_unknown_bed_rejected(db):
    with pytest.raises(NotFound):
        _request(db, bed_id="GHOST-9")


def test_checkout_before_checkin_rejected(db):
    start = booking_start()
    with pytest.raises(ValidationError):
        booking.create_request(
            db, phone="+919000000101", full_name="Dummy", gender="MALE",
            bed_id="1NA-B1", check_in_at=start, check_out_at=start - timedelta(hours=1))


def test_zero_length_hold_rejected(db):
    with pytest.raises(ValidationError):
        _request(db, hold_minutes=0)


# --- conflicts -------------------------------------------------------------

def test_second_request_for_a_held_bed_is_refused(db):
    _request(db, phone="+919000000102")
    with pytest.raises(BedUnavailable, match="held"):
        _request(db, phone="+919000000103")


def test_approving_a_taken_bed_is_refused(db):
    """Guest A is approved, then guest B's request for the same window is approved."""
    first = _request(db, phone="+919000000104")
    booking.approve(db, first.booking_id, actor=OWNER)

    # A second request can only exist because the bed was not held for it; force
    # the situation by requesting a different bed, then moving it onto the taken
    # bed. `booking_beds` is the source of truth for which beds a stay holds.
    second = _request(db, bed_id="1NA-B2", phone="+919000000105")
    db.execute("UPDATE booking_beds SET bed_id = '1NA-B1' WHERE booking_id = %s",
               (second.booking_id,))
    with pytest.raises(BedUnavailable, match="taken"):
        booking.approve(db, second.booking_id, actor=OWNER)


def test_owner_override_places_guest_on_a_bed_under_maintenance(db):
    """The override is for a not-ready bed, and it is recorded in the audit log."""
    request = _request(db, phone="+919000000107")
    db.execute("UPDATE beds SET status = 'MAINTENANCE' WHERE bed_id = '1NA-B1'")

    with pytest.raises(BedUnavailable, match="MAINTENANCE"):
        booking.approve(db, request.booking_id, actor=OWNER)

    result = booking.approve(db, request.booking_id, actor=OWNER, override=True,
                             reason="guest arrived late, bed is usable")
    assert result["status"] == "CONFIRMED"
    entry = db.query_one(
        "SELECT action, after FROM audit_log WHERE entity_id = %s "
        "AND action = 'booking_approved_override'", (request.booking_id,))
    assert entry is not None
    assert "usable" in entry["after"]["reason"]


def test_override_cannot_bypass_a_confirmed_double_booking(db):
    """The no-overlap guarantee is absolute. An override must not defeat it."""
    first = _request(db, phone="+919000000108")
    booking.approve(db, first.booking_id, actor=OWNER)
    second = _request(db, bed_id="1NA-B2", phone="+919000000109")
    db.execute("UPDATE bookings SET bed_id = '1NA-B1' WHERE booking_id = %s",
               (second.booking_id,))
    with pytest.raises(BedUnavailable):
        booking.approve(db, second.booking_id, actor=OWNER, override=True, reason="trying")


def test_two_guests_requesting_the_same_bed_at_once(db):
    """Concurrent requests: at most one may hold the bed."""
    start = booking_start()
    end = start + timedelta(days=2)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(phone: str):
        try:
            request = booking.create_request(
                db, phone=phone, full_name="Dummy", gender="MALE",
                bed_id="1NA-B1", check_in_at=start, check_out_at=end,
                food_included=True)
            outcome = f"ok:{request.booking_id}"
        except BedUnavailable:
            outcome = "refused"
        except Exception as exc:  # surface unexpected errors
            outcome = f"error:{type(exc).__name__}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(f"+9190000002{i:02d}",))
               for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    accepted = [r for r in results if r.startswith("ok:")]
    assert len(accepted) == 1, f"expected exactly one hold, got {results}"
    active_holds = db.query_all(
        "SELECT hold_id FROM bed_holds WHERE bed_id = '1NA-B1' AND status = 'ACTIVE'")
    assert len(active_holds) == 1


def test_database_blocks_two_confirmed_bookings_on_one_bed(db):
    """Even bypassing the application layer, the database refuses the overlap."""
    start = booking_start()
    end = start + timedelta(days=3)
    guest = booking.get_or_create_guest(db, phone="+919000000300")
    db.execute(
        "INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, status, charges_total) "
        "VALUES (%s,'1NA-B1',%s,%s,'CONFIRMED',0)", (guest, start, end))
    with pytest.raises(BedUnavailable):
        db.execute(
            "INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, status, charges_total) "
            "VALUES (%s,'1NA-B1',%s,%s,'CONFIRMED',0)",
            (guest, start + timedelta(days=1), end + timedelta(days=1)))


# --- state machine ---------------------------------------------------------

def test_cannot_confirm_a_rejected_booking(db):
    request = _request(db)
    booking.reject(db, request.booking_id, actor=OWNER)
    with pytest.raises(InvalidTransition):
        booking.approve(db, request.booking_id, actor=OWNER)


def test_cannot_check_in_before_approval(db):
    request = _request(db)
    with pytest.raises(InvalidTransition):
        booking.check_in(db, request.booking_id, actor=OWNER)


def test_cannot_check_out_before_check_in(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    with pytest.raises(InvalidTransition):
        booking.check_out(db, request.booking_id, actor=OWNER)


def test_check_in_then_check_out_flow(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    assert booking.get_booking(db, request.booking_id)["status"] == "CHECKED_IN"
    assert db.query_one("SELECT status FROM beds WHERE bed_id='1NA-B1'")["status"] == "OCCUPIED"

    booking.check_out(db, request.booking_id, actor=OWNER)
    assert booking.get_booking(db, request.booking_id)["status"] == "CHECKED_OUT"
    assert db.query_one("SELECT status FROM beds WHERE bed_id='1NA-B1'")["status"] == "CLEANING"


def test_bed_becomes_available_after_cleaning(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    booking.check_out(db, request.booking_id, actor=OWNER)
    booking.mark_bed_ready(db, "1NA-B1", actor=OWNER)
    assert db.query_one("SELECT status FROM beds WHERE bed_id='1NA-B1'")["status"] == "AVAILABLE"


def test_unknown_booking_raises(db):
    with pytest.raises(NotFound):
        booking.get_booking(db, "00000000-0000-0000-0000-000000000000")


# --- expiry ----------------------------------------------------------------

def test_lapsed_hold_expires_the_request(db):
    request = _request(db)
    db.execute(
        "UPDATE bed_holds SET expires_at = now() - interval '1 minute', "
        "created_at = now() - interval '1 hour' WHERE hold_id = %s", (request.hold_id,))
    assert booking.expire_lapsed_requests(db) == 1
    assert booking.get_booking(db, request.booking_id)["status"] == "EXPIRED"


def test_live_hold_does_not_expire_the_request(db):
    _request(db)
    assert booking.expire_lapsed_requests(db) == 0


def test_expired_request_can_still_be_confirmed_by_the_owner(db):
    request = _request(db)
    db.execute(
        "UPDATE bed_holds SET expires_at = now() - interval '1 minute', "
        "created_at = now() - interval '1 hour' WHERE hold_id = %s", (request.hold_id,))
    booking.expire_lapsed_requests(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    assert booking.get_booking(db, request.booking_id)["status"] == "CONFIRMED"


# --- queries used by the owner assistant -----------------------------------

def test_pending_requests_listed(db):
    _request(db, phone="+919000000400")
    pending = booking.pending_requests(db)
    assert len(pending) == 1
    assert pending[0]["status"] == "REQUESTED"


def test_in_house_and_overdue(db):
    start = booking_start()
    request = booking.create_request(
        db, phone="+919000000401", full_name="Dummy", gender="MALE",
        bed_id="1NA-B1", check_in_at=start, check_out_at=start + timedelta(days=1),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    booking.check_in(db, request.booking_id, actor=OWNER)
    assert len(booking.in_house(db)) == 1
    assert len(booking.overdue_checkouts(db, now=start + timedelta(days=5))) == 1


def test_audit_trail_records_every_step(db):
    request = _request(db)
    booking.approve(db, request.booking_id, actor=OWNER)
    actions = [row["action"] for row in db.query_all(
        "SELECT action FROM audit_log WHERE entity = 'booking' AND entity_id = %s "
        "ORDER BY id", (request.booking_id,))]
    assert actions == ["booking_requested", "booking_approved"]
