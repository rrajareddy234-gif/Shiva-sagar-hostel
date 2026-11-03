"""Availability engine tests.

Covers the brief's availability matrix: one bed free, one bed occupied, a full
room, a full hostel, different-day stays, same-day turnover, overlapping
reservations, temporary hold expiry, and cleaning/maintenance states.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import availability, booking
from core.errors import NotFound, ValidationError

pytestmark = pytest.mark.usefixtures("seeded")


def _book(db, bed_id, start, end, status="CONFIRMED", phone="+919000000010"):
    guest = booking.get_or_create_guest(db, phone=phone, full_name="Dummy Guest")
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, status, charges_total)
            VALUES (%s, %s, %s, %s, %s, 350) RETURNING booking_id, reference
            """,
            (guest, bed_id, start, end, status),
        )
        return cur.fetchone()


# --- basic availability ----------------------------------------------------

def test_all_beds_available_when_empty(db, tomorrow):
    beds = availability.list_bed_availability(db, tomorrow, tomorrow + timedelta(days=2))
    assert len(beds) == 10
    assert all(b.available for b in beds)


def test_one_bed_occupied_removes_only_that_bed(db, tomorrow):
    _book(db, "1NA-B1", tomorrow, tomorrow + timedelta(days=2))
    free = availability.available_beds(db, tomorrow, tomorrow + timedelta(days=2))
    assert "1NA-B1" not in free
    assert "1NA-B2" in free
    assert len(free) == 9


def test_specific_bed_availability_check(db, tomorrow):
    _book(db, "2AC-B1", tomorrow, tomorrow + timedelta(days=3))
    assert availability.bed_is_available(
        db, "2AC-B1", tomorrow, tomorrow + timedelta(days=1)) is False
    assert availability.bed_is_available(
        db, "2AC-B2", tomorrow, tomorrow + timedelta(days=1)) is True


def test_unknown_bed_raises(db, tomorrow):
    with pytest.raises(NotFound):
        availability.bed_is_available(db, "NOPE-1", tomorrow, tomorrow + timedelta(days=1))


# --- whole room and whole hostel -------------------------------------------

def test_all_beds_in_a_room_occupied(db, tomorrow):
    end = tomorrow + timedelta(days=2)
    _book(db, "1NA-B1", tomorrow, end, phone="+919000000011")
    _book(db, "1NA-B2", tomorrow, end, phone="+919000000012")
    summary = availability.availability_summary(db, tomorrow, end)
    assert "1NA" in summary["full_rooms"]


def test_all_rooms_full(db, tomorrow):
    end = tomorrow + timedelta(days=2)
    for i, bed in enumerate(["1NA-B1", "1NA-B2", "2AC-B1", "2AC-B2",
                             "6NA-B1", "6NA-B2", "6NA-B3",
                             "DORM-M1", "DORM-M2", "DORM-M3"]):
        _book(db, bed, tomorrow, end, phone=f"+9190000010{i:02d}")
    summary = availability.availability_summary(db, tomorrow, end)
    assert summary["available_beds"] == 0
    assert len(summary["full_rooms"]) == 4
    assert availability.find_bed(db, tomorrow, end) is None


# --- date and time handling ------------------------------------------------

def test_check_in_and_checkout_on_different_days(db, tomorrow):
    """A stay that ends before the next guest arrives leaves the bed free."""
    _book(db, "1NA-B1", tomorrow, tomorrow + timedelta(days=2))
    later = tomorrow + timedelta(days=3)
    assert availability.bed_is_available(db, "1NA-B1", later, later + timedelta(days=1)) is True


def test_same_day_checkout_and_arrival_allowed(db, tomorrow):
    """Half-open intervals: out at 10:00, in at 10:00, no conflict."""
    checkout = tomorrow + timedelta(days=2)
    _book(db, "1NA-B1", tomorrow, checkout)
    assert availability.bed_is_available(db, "1NA-B1", checkout, checkout + timedelta(days=2)) is True


def test_overlapping_reservation_blocks_bed(db, tomorrow):
    _book(db, "1NA-B1", tomorrow, tomorrow + timedelta(days=4))
    overlap_start = tomorrow + timedelta(days=2)
    assert availability.bed_is_available(
        db, "1NA-B1", overlap_start, overlap_start + timedelta(days=2)) is False


def test_partial_hour_overlap_blocks_bed(db, tomorrow):
    _book(db, "1NA-B1", tomorrow, tomorrow + timedelta(days=1))
    # Arriving one hour before the previous guest leaves.
    assert availability.bed_is_available(
        db, "1NA-B1", tomorrow + timedelta(hours=23), tomorrow + timedelta(days=2)) is False


# --- statuses --------------------------------------------------------------

def test_cleaning_bed_is_not_available(db, tomorrow):
    db.execute("UPDATE beds SET status = 'CLEANING' WHERE bed_id = '1NA-B1'")
    beds = {b.bed_id: b for b in availability.list_bed_availability(
        db, tomorrow, tomorrow + timedelta(days=1))}
    assert beds["1NA-B1"].available is False


def test_maintenance_bed_is_not_available(db, tomorrow):
    db.execute("UPDATE beds SET status = 'MAINTENANCE' WHERE bed_id = '2AC-B1'")
    beds = {b.bed_id: b for b in availability.list_bed_availability(
        db, tomorrow, tomorrow + timedelta(days=1))}
    assert beds["2AC-B1"].available is False


def test_unavailable_bed_is_not_available(db, tomorrow):
    db.execute("UPDATE beds SET status = 'UNAVAILABLE' WHERE bed_id = '6NA-B1'")
    beds = {b.bed_id: b for b in availability.list_bed_availability(
        db, tomorrow, tomorrow + timedelta(days=1))}
    assert beds["6NA-B1"].available is False


def test_inactive_bed_is_excluded(db, tomorrow):
    db.execute("UPDATE beds SET active = false WHERE bed_id = '1NA-B1'")
    beds = availability.list_bed_availability(db, tomorrow, tomorrow + timedelta(days=1))
    assert "1NA-B1" not in {b.bed_id for b in beds}


# --- holds -----------------------------------------------------------------

def test_active_hold_blocks_bed(db, tomorrow):
    guest = booking.get_or_create_guest(db, phone="+919000000020")
    db.execute(
        """
        INSERT INTO bed_holds (bed_id, guest_id, start_at, end_at, expires_at, status)
        VALUES ('1NA-B1', %s, %s, %s, now() + interval '30 minutes', 'ACTIVE')
        """,
        (guest, tomorrow, tomorrow + timedelta(days=1)),
    )
    assert availability.bed_is_available(
        db, "1NA-B1", tomorrow, tomorrow + timedelta(days=1)) is False


def test_expired_hold_does_not_block_bed(db, tomorrow):
    guest = booking.get_or_create_guest(db, phone="+919000000021")
    db.execute(
        """
        INSERT INTO bed_holds (bed_id, guest_id, start_at, end_at, expires_at, status, created_at)
        VALUES ('1NA-B1', %s, %s, %s, now() - interval '1 minute', 'ACTIVE',
                now() - interval '30 minutes')
        """,
        (guest, tomorrow, tomorrow + timedelta(days=1)),
    )
    assert availability.bed_is_available(
        db, "1NA-B1", tomorrow, tomorrow + timedelta(days=1)) is True


def test_expire_stale_holds_marks_them_expired(db, tomorrow):
    guest = booking.get_or_create_guest(db, phone="+919000000022")
    db.execute(
        """
        INSERT INTO bed_holds (bed_id, guest_id, start_at, end_at, expires_at, status, created_at)
        VALUES ('1NA-B1', %s, %s, %s, now() - interval '1 minute', 'ACTIVE',
                now() - interval '30 minutes')
        """,
        (guest, tomorrow, tomorrow + timedelta(days=1)),
    )
    assert availability.expire_stale_holds(db) == 1
    row = db.query_one("SELECT status FROM bed_holds")
    assert row["status"] == "EXPIRED"


# --- filtering and summary -------------------------------------------------

def test_filter_by_room_type(db, tomorrow):
    ac = availability.available_beds(db, tomorrow, tomorrow + timedelta(days=1),
                                     room_type="AC")
    assert set(ac) == {"2AC-B1", "2AC-B2"}


def test_filter_by_bed_type(db, tomorrow):
    mats = availability.available_beds(db, tomorrow, tomorrow + timedelta(days=1),
                                       bed_type="MAT")
    assert set(mats) == {"DORM-M1", "DORM-M2", "DORM-M3"}


def test_find_bed_prefers_requested_room(db, tomorrow):
    chosen = availability.find_bed(db, tomorrow, tomorrow + timedelta(days=1),
                                   preferred_rooms=["6NA"])
    assert chosen in {"6NA-B1", "6NA-B2", "6NA-B3"}


def test_summary_counts(db, tomorrow):
    end = tomorrow + timedelta(days=2)
    _book(db, "1NA-B1", tomorrow, end)
    db.execute("UPDATE beds SET status = 'MAINTENANCE' WHERE bed_id = '1NA-B2'")
    summary = availability.availability_summary(db, tomorrow, end)
    assert summary["total_beds"] == 10
    assert summary["occupied_beds"] == 1
    assert summary["blocked_beds"] == 1
    assert summary["available_beds"] == 8


def test_invalid_window_rejected(db, tomorrow):
    with pytest.raises(ValidationError):
        availability.list_bed_availability(db, tomorrow, tomorrow)


def test_sync_bed_status_reflects_checked_in(db, tomorrow):
    now = datetime.now(tomorrow.tzinfo)
    _book(db, "1NA-B1", now - timedelta(hours=1), now + timedelta(days=1),
          status="CHECKED_IN")
    availability.sync_bed_status(db)
    row = db.query_one("SELECT status FROM beds WHERE bed_id = '1NA-B1'")
    assert row["status"] == "OCCUPIED"


def test_sync_bed_status_does_not_override_maintenance(db, tomorrow):
    db.execute("UPDATE beds SET status = 'MAINTENANCE' WHERE bed_id = '1NA-B1'")
    availability.sync_bed_status(db)
    row = db.query_one("SELECT status FROM beds WHERE bed_id = '1NA-B1'")
    assert row["status"] == "MAINTENANCE"
