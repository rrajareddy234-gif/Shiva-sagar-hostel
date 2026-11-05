"""Bed-level availability engine.

This is deterministic code and SQL. No AI is involved in deciding whether a bed
is free. Availability is derived from the authoritative records:

  * bookings with status CONFIRMED or CHECKED_IN block the bed
  * bed_holds with status ACTIVE and an unexpired expires_at block the bed
  * beds in CLEANING, MAINTENANCE or UNAVAILABLE are blocked regardless

Intervals are half-open, [start, end). A guest checking out at 10:00 and another
checking in at 10:00 on the same day do not conflict.

The `beds.status` column is an operational label (cleaning, maintenance). It is
NOT the source of truth for occupancy, so a stale label can never cause a
double-booking. Use `sync_bed_status` to keep it tidy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from core.db import Database
from core.errors import NotFound, ValidationError

# Statuses that block a bed no matter what the booking table says.
HARD_BLOCK_STATUSES = ("CLEANING", "MAINTENANCE", "UNAVAILABLE")

# Booking statuses that occupy a bed.
OCCUPYING_BOOKING_STATUSES = ("CONFIRMED", "CHECKED_IN")

_WINDOW = "tstzrange(%(start)s, %(end)s, '[)')"


@dataclass(frozen=True)
class BedAvailability:
    bed_id: str
    room_id: str
    room_type: str
    bed_type: str
    status: str
    booked: bool
    held: bool

    @property
    def available(self) -> bool:
        return not self.booked and not self.held and self.status not in HARD_BLOCK_STATUSES


def _check_window(start: datetime, end: datetime) -> None:
    if start is None or end is None:
        raise ValidationError("both a start and an end date are required")
    if end <= start:
        raise ValidationError("the checkout date must be after the check-in date")


def _fetch_beds(db: Database, start: datetime, end: datetime,
                room_type: str | None = None, bed_type: str | None = None) -> list[BedAvailability]:
    sql = f"""
        SELECT b.bed_id, b.room_id, r.room_type, b.bed_type, b.status,
               EXISTS (
                   SELECT 1 FROM bookings bk
                   WHERE bk.bed_id = b.bed_id
                     AND bk.status = ANY(%(occupying)s)
                     AND tstzrange(bk.check_in_at, bk.check_out_at, '[)')
                         && {_WINDOW}
               ) AS booked,
               EXISTS (
                   SELECT 1 FROM bed_holds h
                   WHERE h.bed_id = b.bed_id
                     AND h.status = 'ACTIVE'
                     AND h.expires_at > now()
                     AND tstzrange(h.start_at, h.end_at, '[)')
                         && {_WINDOW}
               ) AS held
        FROM beds b
        JOIN rooms r ON r.room_id = b.room_id
        WHERE b.active AND r.active
          AND (%(room_type)s::text IS NULL OR r.room_type = %(room_type)s::text)
          AND (%(bed_type)s::text IS NULL OR b.bed_type = %(bed_type)s::text)
        ORDER BY r.room_type, b.room_id, b.bed_id
    """
    rows = db.query_all(sql, {
        "start": start, "end": end,
        "occupying": list(OCCUPYING_BOOKING_STATUSES),
        "room_type": room_type, "bed_type": bed_type,
    })
    return [BedAvailability(**row) for row in rows]


def list_bed_availability(db: Database, start: datetime, end: datetime,
                          room_type: str | None = None,
                          bed_type: str | None = None) -> list[BedAvailability]:
    """Every active bed, with whether it is free for the whole window."""
    _check_window(start, end)
    return _fetch_beds(db, start, end, room_type, bed_type)


def available_beds(db: Database, start: datetime, end: datetime,
                   room_type: str | None = None,
                   bed_type: str | None = None) -> list[str]:
    """Bed ids free for the whole window, cheapest-to-offer order."""
    return [b.bed_id for b in list_bed_availability(db, start, end, room_type, bed_type)
            if b.available]


def bed_is_available(db: Database, bed_id: str, start: datetime, end: datetime) -> bool:
    """True only if this specific bed is free for the whole window."""
    _check_window(start, end)
    bed = db.query_one(
        "SELECT b.bed_id, b.status, b.active, r.active AS room_active "
        "FROM beds b JOIN rooms r ON r.room_id = b.room_id WHERE b.bed_id = %(bed_id)s",
        {"bed_id": bed_id},
    )
    if not bed:
        raise NotFound(f"bed {bed_id} does not exist")
    if not bed["active"] or not bed["room_active"]:
        return False
    if bed["status"] in HARD_BLOCK_STATUSES:
        return False
    conflict = db.query_one(
        f"""
        SELECT 1 AS hit FROM bookings
        WHERE bed_id = %(bed_id)s
          AND status = ANY(%(occupying)s)
          AND tstzrange(check_in_at, check_out_at, '[)') && {_WINDOW}
        LIMIT 1
        """,
        {"bed_id": bed_id, "start": start, "end": end,
         "occupying": list(OCCUPYING_BOOKING_STATUSES)},
    )
    if conflict:
        return False
    held = db.query_one(
        f"""
        SELECT 1 AS hit FROM bed_holds
        WHERE bed_id = %(bed_id)s AND status = 'ACTIVE' AND expires_at > now()
          AND tstzrange(start_at, end_at, '[)') && {_WINDOW}
        LIMIT 1
        """,
        {"bed_id": bed_id, "start": start, "end": end},
    )
    return held is None


def find_bed(db: Database, start: datetime, end: datetime,
             room_type: str | None = None,
             preferred_rooms: Iterable[str] | None = None) -> str | None:
    """Pick a bed to offer, or None if nothing is free.

    Preference is given to rooms the guest asked about, then to beds that are
    already free and in a clean state.
    """
    candidates = [b for b in list_bed_availability(db, start, end, room_type) if b.available]
    if not candidates:
        return None
    if preferred_rooms:
        preferred = set(preferred_rooms)
        ordered = [b for b in candidates if b.room_id in preferred]
        if ordered:
            return ordered[0].bed_id
    return candidates[0].bed_id


def availability_summary(db: Database, start: datetime, end: datetime) -> dict:
    """Counts for the owner's questions: vacancies, full rooms, AC vs non-AC."""
    beds = list_bed_availability(db, start, end)
    by_room: dict[str, dict] = {}
    for bed in beds:
        room = by_room.setdefault(bed.room_id, {
            "room_id": bed.room_id,
            "room_type": bed.room_type,
            "total_beds": 0,
            "available_beds": 0,
            "blocked_beds": 0,
        })
        room["total_beds"] += 1
        if bed.available:
            room["available_beds"] += 1
        elif bed.status in HARD_BLOCK_STATUSES:
            room["blocked_beds"] += 1

    by_type: dict[str, dict] = {}
    for room in by_room.values():
        bucket = by_type.setdefault(room["room_type"], {"total_beds": 0, "available_beds": 0})
        bucket["total_beds"] += room["total_beds"]
        bucket["available_beds"] += room["available_beds"]

    full_rooms = sorted(r["room_id"] for r in by_room.values() if r["available_beds"] == 0)
    return {
        "total_beds": len(beds),
        "available_beds": sum(1 for b in beds if b.available),
        "occupied_beds": sum(1 for b in beds if b.booked),
        "held_beds": sum(1 for b in beds if b.held and not b.booked),
        "blocked_beds": sum(1 for b in beds if b.status in HARD_BLOCK_STATUSES),
        "full_rooms": full_rooms,
        "rooms": sorted(by_room.values(), key=lambda r: r["room_id"]),
        "by_room_type": by_type,
    }


def expire_stale_holds(db: Database) -> int:
    """Mark elapsed holds EXPIRED so beds are never blocked forever."""
    return db.execute(
        "UPDATE bed_holds SET status = 'EXPIRED' "
        "WHERE status = 'ACTIVE' AND expires_at <= now()"
    )


def sync_bed_status(db: Database) -> int:
    """Reconcile the operational `beds.status` label with the booking records.

    Bookings remain the source of truth. This keeps the label honest for staff
    and dashboards. Never overwrites an operational state such as CLEANING or
    MAINTENANCE.
    """
    return db.execute(
        """
        UPDATE beds b SET status = CASE
            WHEN EXISTS (
                SELECT 1 FROM bookings bk
                WHERE bk.bed_id = b.bed_id AND bk.status = 'CHECKED_IN'
                  AND now() >= bk.check_in_at AND now() < bk.check_out_at
            ) THEN 'OCCUPIED'
            WHEN EXISTS (
                SELECT 1 FROM bookings bk
                WHERE bk.bed_id = b.bed_id AND bk.status = 'CONFIRMED'
                  AND now() >= bk.check_in_at AND now() < bk.check_out_at
            ) THEN 'CHECKOUT_PENDING'
            WHEN EXISTS (
                SELECT 1 FROM bookings bk
                WHERE bk.bed_id = b.bed_id AND bk.status = 'CONFIRMED'
                  AND bk.check_in_at > now()
            ) THEN 'BOOKED'
            ELSE 'AVAILABLE'
        END
        WHERE b.status NOT IN ('CLEANING', 'MAINTENANCE', 'UNAVAILABLE')
        """
    )
