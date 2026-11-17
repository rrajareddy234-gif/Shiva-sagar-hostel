"""Booking state machine.

A booking request never becomes a confirmed booking silently. The owner approves
or rejects it, and every transition is written to the audit log.

States: REQUESTED -> CONFIRMED -> CHECKED_IN -> CHECKED_OUT
        REQUESTED -> ACCEPTED -> PROOF_SENT -> CONFIRMED
        REQUESTED -> REJECTED
        REQUESTED|CONFIRMED -> CANCELLED
        CONFIRMED -> NO_SHOW

There are no temporary bed holds and no time-based expiry: a live request is
never released by a clock. A bed is blocked only by a CONFIRMED or CHECKED_IN
booking, and every state change is made by the owner (or an operator acting for
them). A request that is never decided simply stays REQUESTED until the owner
acts on it.

Concurrency: approval locks the booking row and re-checks the bed, and the
database exclusion constraint is the final guard. If two approvals race for the
same bed, one wins and the other is told the bed is taken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core import pricing
from core.db import Database
from core.errors import (
    BedUnavailable,
    DuplicateGuestBooking,
    InvalidTransition,
    NotFound,
    ValidationError,
)
from core.pricing import money


def _queue_booking_notice_tx(cur, booking: dict, kind: str) -> None:
    """Persist a guest notice intent with the booking state transition."""
    cur.execute("SELECT phone FROM guests WHERE guest_id=%s", (booking["guest_id"],))
    guest = cur.fetchone()
    if not guest or not guest["phone"]:
        return
    from core.outbox import enqueue_tx
    enqueue_tx(cur, recipient=guest["phone"], message_type="booking_notice",
               payload={"booking_id": str(booking["booking_id"]), "notice_kind": kind},
               idempotency_key=f"booking:{booking['reference']}:notice:{kind}",
               source_event_type="booking_notice", source_event_id=str(booking["booking_id"]))

ALLOWED_TRANSITIONS = {
    # The owner decides twice. First they accept the request, which is what
    # tells the guest to pay. Then, once they have seen the payment, they
    # confirm. A single-step REQUESTED -> CONFIRMED is still allowed for the
    # owner who already knows the money is fine (a guest paying cash in
    # person); it is audited like any other transition.
    #
    # EXPIRED is a terminal legacy state kept only so old rows remain readable;
    # nothing in this system produces it. There is no time-based expiry: a
    # request stays REQUESTED until the owner accepts, rejects or cancels it.
    "REQUESTED": {"ACCEPTED", "CONFIRMED", "REJECTED", "CANCELLED"},
    "ACCEPTED": {"PROOF_SENT", "CONFIRMED", "REJECTED", "CANCELLED"},
    "PROOF_SENT": {"CONFIRMED", "REJECTED", "CANCELLED"},
    "CONFIRMED": {"CHECKED_IN", "CANCELLED", "NO_SHOW"},
    "CHECKED_IN": {"CHECKED_OUT"},
    "CHECKED_OUT": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
    "NO_SHOW": set(),
    "EXPIRED": set(),
}


@dataclass(frozen=True)
class BookingRequest:
    booking_id: str
    reference: str
    guest_id: str
    bed_id: str
    check_in_at: datetime
    check_out_at: datetime
    status: str
    charges_total: Decimal | None
    quote: pricing.Quote
    payment_terms: str = "ADVANCE"
    guests_count: int = 1
    advance_due: Decimal = Decimal("0")


def _audit(cur, actor: str, action: str, entity: str, entity_id: str,
           before=None, after=None) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def get_or_create_guest(db: Database, *, phone: str, full_name: str | None = None,
                        gender: str | None = None,
                        name_source: str = "UNKNOWN") -> str:
    """Find or create the guest for a phone number.

    The name is only overwritten by a source at least as trustworthy as the one
    already stored. A WhatsApp profile name is a convenience and must never
    replace a name the guest typed, or one the owner entered.
    """
    if not phone:
        raise ValidationError("a phone number is required")
    if name_source not in ("UNKNOWN", "WHATSAPP_PROFILE", "GUEST_TYPED", "OWNER", "IMPORT"):
        raise ValidationError(f"unknown name source {name_source!r}")
    # Higher rank wins; a profile name may fill a blank but not overwrite.
    rank = {"UNKNOWN": 0, "WHATSAPP_PROFILE": 1, "IMPORT": 2, "GUEST_TYPED": 3, "OWNER": 4}
    with db.transaction() as cur:
        cur.execute("SELECT guest_id, full_name, name_source FROM guests WHERE phone = %s",
                    (phone,))
        row = cur.fetchone()
        if row:
            current_rank = rank.get(row["name_source"] or "UNKNOWN", 0)
            new_rank = rank[name_source]
            incoming_name = (full_name or "").strip() or None
            if incoming_name and new_rank >= current_rank:
                cur.execute(
                    "UPDATE guests SET full_name = %(name)s, name_source = %(src)s, "
                    "gender = COALESCE(%(gender)s, gender), "
                    "wa_profile_name = CASE WHEN %(src)s = 'WHATSAPP_PROFILE' "
                    "THEN %(name)s ELSE wa_profile_name END "
                    "WHERE guest_id = %(id)s",
                    {"name": incoming_name, "src": name_source,
                     "gender": gender, "id": row["guest_id"]},
                )
            elif gender:
                cur.execute("UPDATE guests SET gender = COALESCE(%s, gender) "
                            "WHERE guest_id = %s", (gender, row["guest_id"]))
            return str(row["guest_id"])
        cur.execute(
            "INSERT INTO guests (full_name, phone, gender, name_source, wa_profile_name) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING guest_id",
            ((full_name or "").strip() or None, phone, gender or "UNKNOWN", name_source,
             (full_name or "").strip() or None if name_source == "WHATSAPP_PROFILE" else None),
        )
        return str(cur.fetchone()["guest_id"])


def get_booking(db: Database, booking_id: str) -> dict:
    row = db.query_one("SELECT * FROM bookings WHERE booking_id = %s", (booking_id,))
    if not row:
        raise NotFound(f"booking {booking_id} not found")
    return row


def _require_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"a booking in state {current} cannot become {target}")


def create_request(db: Database, *, phone: str, full_name: str | None,
                   gender: str | None, bed_id: str | None = None,
                   bed_ids: list[str] | None = None,
                   check_in_at: datetime,
                   check_out_at: datetime, food_included: bool | None = None,
                   mode: str = "daily", extras=0, discount=0,
                   stay_mode: str = "NIGHT",
                   payment_terms: str = "ADVANCE", guests_count: int = 1,
                   enquiry_id: str | None = None,
                   source: str = "WHATSAPP",
                   name_source: str = "UNKNOWN") -> BookingRequest:
    """Create a booking request. Does not confirm anything, and holds nothing.

    A party is booked as ONE booking covering several beds. Pass `bed_ids` for a
    party, or `bed_id` for the single-guest case. `guests_count` must match the
    number of beds: a guest without a bed would be unaccounted for, and a bed
    without a guest would be held for nobody.

    The request blocks no other guest: there is no temporary hold. Only a
    CONFIRMED or CHECKED_IN booking occupies a bed, so several guests may ask
    for the same bed and the owner decides which one to confirm. The database
    exclusion constraint is the hard guarantee that no bed is ever confirmed
    twice.
    """
    if check_out_at <= check_in_at:
        raise ValidationError("the checkout time must be after the check-in time")
    if payment_terms not in ("FULL", "ADVANCE"):
        raise ValidationError("payment terms must be FULL or ADVANCE")
    if int(guests_count or 1) < 1:
        raise ValidationError("there must be at least one guest")

    # Normalise the two ways of naming beds into one list.
    chosen: list[str] = []
    for candidate in (bed_ids or ([bed_id] if bed_id else [])):
        if candidate and candidate not in chosen:
            chosen.append(candidate)
    if not chosen:
        raise ValidationError("at least one bed is required")
    if len(chosen) != int(guests_count):
        raise ValidationError(
            f"{guests_count} guest(s) need {guests_count} bed(s), but "
            f"{len(chosen)} bed(s) were given"
        )
    # The party must all be in one room type, so a single rate can apply.
    room_types = {
        row["room_type"]
        for row in db.query_all(
            "SELECT DISTINCT r.room_type FROM beds b JOIN rooms r ON r.room_id = b.room_id "
            "WHERE b.bed_id = ANY(%s)",
            (chosen,),
        )
    }
    if not room_types:
        raise NotFound(f"bed {chosen[0]} does not exist")
    if len(room_types) > 1:
        raise ValidationError(
            "all beds in one booking must be the same room type; "
            "book separate rooms as separate bookings"
        )
    room_type = room_types.pop()

    guest_id = get_or_create_guest(db, phone=phone, full_name=full_name,
                                   gender=gender, name_source=name_source)
    quote = pricing.quote(db, room_type=room_type, food_included=food_included,
                          start=check_in_at, end=check_out_at, mode=mode,
                          stay_mode=stay_mode, guests=int(guests_count),
                          extras=extras, discount=discount)
    food_included = quote.food_included

    # Imported here to keep booking independent of billing at import time.
    from core import billing
    advance = (billing.advance_due(db, int(guests_count))
               if payment_terms == "ADVANCE" else money(0))

    with db.transaction() as cur:
        # Serialize simultaneous booking requests from the same guest.
        cur.execute(
            "SELECT guest_id FROM guests WHERE guest_id = %s FOR UPDATE",
            (guest_id,),
        )

        # Reject overlapping active bookings for this guest.
        cur.execute(
            """
            SELECT reference, status
            FROM bookings
            WHERE guest_id = %s
              AND status IN ('REQUESTED', 'ACCEPTED', 'PROOF_SENT',
                             'CONFIRMED', 'CHECKED_IN')
              AND tstzrange(check_in_at, check_out_at, '[)') &&
                  tstzrange(%s, %s, '[)')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (guest_id, check_in_at, check_out_at),
        )
        duplicate = cur.fetchone()
        if duplicate:
            reference = duplicate["reference"]
            status = duplicate["status"]
            raise DuplicateGuestBooking(
                f"You already have booking {reference} ({status}) "
                "for overlapping dates. Please contact the owner "
                "if you need to change it."
            )

        # Refuse if any bed in the party is already confirmed or in-house for
        # the same window. A mere request never blocks a bed, so two guests may
        # request the same bed and the owner decides which one to confirm.
        _assert_no_confirmed_overlap(cur, None, chosen, check_in_at, check_out_at)

        cur.execute(
            """
            INSERT INTO bookings (guest_id, bed_id, check_in_at, check_out_at, status,
                                  food_included, agreed_daily_rate, charges_total, source,
                                  payment_terms, guests_count, advance_due, enquiry_id,
                                  beds_count)
            VALUES (%s, %s, %s, %s, 'REQUESTED', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING booking_id, reference
            """,
            (guest_id, chosen[0], check_in_at, check_out_at, food_included,
             quote.unit_rate, quote.charges_total, source, payment_terms,
             int(guests_count), advance, enquiry_id, len(chosen)),
        )
        booking = cur.fetchone()

        # Every bed in the party is recorded, so the no-double-booking rule is
        # enforced per bed even for a multi-bed stay.
        for one_bed in chosen:
            cur.execute(
                """
                INSERT INTO booking_beds (booking_id, bed_id, check_in_at,
                                          check_out_at, status)
                VALUES (%s, %s, %s, %s, 'REQUESTED')
                """,
                (booking["booking_id"], one_bed, check_in_at, check_out_at),
            )

        _audit(cur, phone, "booking_requested", "booking", str(booking["booking_id"]),
               None, {"status": "REQUESTED", "bed_ids": chosen,
                      "check_in_at": check_in_at, "check_out_at": check_out_at,
                      "guests_count": int(guests_count),
                      "payment_terms": payment_terms, "advance_due": str(advance),
                      "charges_total": str(quote.charges_total)})

    return BookingRequest(
        booking_id=str(booking["booking_id"]),
        reference=booking["reference"],
        guest_id=guest_id,
        bed_id=chosen[0],
        check_in_at=check_in_at,
        check_out_at=check_out_at,
        status="REQUESTED",
        charges_total=quote.charges_total,
        quote=quote,
        payment_terms=payment_terms,
        guests_count=int(guests_count),
        advance_due=advance,
    )


def booking_beds(db: Database, booking_id: str) -> list[str]:
    """Every bed this booking holds, primary bed first for stability."""
    rows = db.query_all(
        "SELECT bed_id FROM booking_beds WHERE booking_id = %s ORDER BY created_at, bed_id",
        (booking_id,))
    if rows:
        return [r["bed_id"] for r in rows]
    # Fallback for any row that predates the link table.
    row = db.query_one("SELECT bed_id FROM bookings WHERE booking_id = %s", (booking_id,))
    return [row["bed_id"]] if row else []


def _assert_no_confirmed_overlap(cur, booking_id: str | None, beds: list[str],
                                 check_in_at, check_out_at) -> None:
    """Refuse if any bed in the party is already confirmed to someone else.

    This is the soft, friendly half of the no-double-booking rule: the database
    exclusion constraint is the hard half. Checked when a request is created, and
    again before an owner accepts or confirms it, because a guest must never be
    asked to pay for a bed that is already someone else's. `booking_id` is None
    when the row does not exist yet (request creation), in which case every
    confirmed booking is a conflict.
    """
    for one_bed in beds:
        cur.execute(
            """
            SELECT 1 FROM booking_beds
            WHERE bed_id = %s
              AND status = ANY(ARRAY['CONFIRMED','CHECKED_IN'])
              AND (%s::uuid IS NULL OR booking_id <> %s)
              AND tstzrange(check_in_at, check_out_at, '[)')
                  && tstzrange(%s, %s, '[)')
            LIMIT 1
            """,
            (one_bed, booking_id, booking_id, check_in_at, check_out_at),
        )
        if cur.fetchone():
            raise BedUnavailable(
                f"bed {one_bed} is already booked for those dates")


def accept(db: Database, booking_id: str, *, actor: str,
           reason: str | None = None) -> dict:
    """The owner's first decision: take this booking, and ask the guest to pay.

    This does NOT confirm the booking and does NOT claim the bed in the
    no-double-booking sense. It moves the request to ACCEPTED and tells the guest
    to pay. Nothing is released on a clock: the owner decides, and the bed is
    claimed only when they confirm. The bed is not blocked for anyone else by an
    acceptance alone, because only a CONFIRMED booking occupies a bed.
    """
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "ACCEPTED")

        # Never ask a guest to pay for a bed that is already confirmed to
        # someone else. Better to tell the owner now than to take money and
        # disappoint the guest at confirmation.
        beds = booking_beds(db, booking_id) or [booking["bed_id"]]
        _assert_no_confirmed_overlap(cur, booking_id, beds,
                                     booking["check_in_at"], booking["check_out_at"])

        cur.execute(
            "UPDATE bookings SET status = 'ACCEPTED', accepted_by = %s, "
            "accepted_at = now() WHERE booking_id = %s",
            (actor, booking_id),
        )
        cur.execute(
            "UPDATE booking_beds SET status = 'ACCEPTED' WHERE booking_id = %s "
            "AND status = 'REQUESTED'",
            (booking_id,),
        )
        _audit(cur, actor, "booking_accepted", "booking", booking_id,
               {"status": booking["status"]},
               {"status": "ACCEPTED", "reason": reason})
        _queue_booking_notice_tx(cur, booking, "accepted")
    return get_booking(db, booking_id)


def mark_proof_sent(db: Database, booking_id: str, *, proof_path: str | None = None,
                    actor: str = "guest", owner_phone: str | None = None,
                    outbound_file_ref: str | None = None,
                    filename: str | None = None,
                    media_type: str = "image/jpeg") -> dict:
    """The guest says they have paid and sent a screenshot.

    The image itself is filed on disk by the caller; this records that it
    arrived and where it is. The bot does not read the image and does not
    decide whether the payment is real -- that is the owner's second approval.
    """
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "PROOF_SENT")
        cur.execute(
            "UPDATE bookings SET status = 'PROOF_SENT', payment_proof_path = %s, "
            "payment_proof_at = now() WHERE booking_id = %s",
            (proof_path, booking_id),
        )
        cur.execute(
            "UPDATE booking_beds SET status = 'PROOF_SENT' WHERE booking_id = %s "
            "AND status = 'ACCEPTED'",
            (booking_id,),
        )
        _audit(cur, actor, "booking_proof_sent", "booking", booking_id,
               {"status": booking["status"]},
               {"status": "PROOF_SENT", "proof_path": proof_path})
        if outbound_file_ref and owner_phone:
            from core.outbox import enqueue_tx
            enqueue_tx(
                cur, recipient=owner_phone, message_type="document",
                payload={"file_ref": outbound_file_ref,
                         "filename": filename or "payment-proof.jpg",
                         "media_type": media_type,
                         "caption": (f"Payment screenshot for {booking['reference']} from the guest. "
                                     "Check it, then tap Confirm to finish the booking."),
                         "fallback_text": (f"Payment screenshot received for {booking['reference']}. "
                                           "Please check it in the dashboard.")},
                idempotency_key=f"booking:{booking['reference']}:payment-proof:owner",
                source_event_type="payment_proof",
                source_event_id=str(booking["booking_id"]))
        _queue_booking_notice_tx(cur, booking, "proof")
    return get_booking(db, booking_id)


def approve(db: Database, booking_id: str, *, actor: str, override: bool = False,
            reason: str | None = None) -> dict:
    """Owner approval.

    A hard conflict with another confirmed or in-house booking is never
    bypassable: the database exclusion constraints guarantee it, and this check
    turns that into a clear message. Every bed in the party is re-checked and
    confirmed together, so a party is never half-confirmed. `override` exists for
    exceptional cases where the owner knowingly places a guest on a bed that is
    not ready, such as one still marked for cleaning or maintenance. Every
    override is audited.
    """
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")

        _require_transition(booking["status"], "CONFIRMED")

        cur.execute(
            "SELECT bed_id FROM booking_beds WHERE booking_id = %s ORDER BY bed_id",
            (booking_id,))
        beds = [r["bed_id"] for r in cur.fetchall()] or [booking["bed_id"]]

        for one_bed in beds:
            cur.execute("SELECT status FROM beds WHERE bed_id = %s", (one_bed,))
            bed = cur.fetchone()
            if bed and bed["status"] in ("CLEANING", "MAINTENANCE", "UNAVAILABLE") \
                    and not override:
                raise BedUnavailable(
                    f"bed {one_bed} is marked {bed['status']}; "
                    "use an owner override to place a guest anyway"
                )

        _assert_no_confirmed_overlap(cur, booking_id, beds,
                                     booking["check_in_at"], booking["check_out_at"])

        cur.execute(
            "UPDATE bookings SET status = 'CONFIRMED', approved_by = %s, approved_at = now(), "
            "final_confirmed_at = now() WHERE booking_id = %s",
            (actor, booking_id),
        )
        # Confirm the party's beds in one statement, so the exclusion constraint
        # sees them together and a partial confirmation cannot survive.
        cur.execute(
            "UPDATE booking_beds SET status = 'CONFIRMED' "
            "WHERE booking_id = %s AND status IN ('REQUESTED','ACCEPTED','PROOF_SENT')",
            (booking_id,),
        )
        _audit(cur, actor, "booking_approved" + ("_override" if override else ""),
               "booking", booking_id, {"status": booking["status"]},
               {"status": "CONFIRMED", "override": override, "reason": reason,
                "beds": beds})
        _queue_booking_notice_tx(cur, booking, "confirmed")

    return get_booking(db, booking_id)


def reject(db: Database, booking_id: str, *, actor: str, reason: str | None = None) -> dict:
    return _close(db, booking_id, actor=actor, target="REJECTED", reason=reason,
                  action="booking_rejected")


def cancel(db: Database, booking_id: str, *, actor: str, reason: str | None = None) -> dict:
    return _close(db, booking_id, actor=actor, target="CANCELLED", reason=reason,
                  action="booking_cancelled")


def _close(db: Database, booking_id: str, *, actor: str, target: str,
           reason: str | None, action: str) -> dict:
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], target)
        cur.execute("UPDATE bookings SET status = %s WHERE booking_id = %s", (target, booking_id))
        cur.execute(
            "UPDATE booking_beds SET status = %s WHERE booking_id = %s "
            "AND status IN ('REQUESTED','ACCEPTED','PROOF_SENT','CONFIRMED')",
            (target, booking_id),
        )
        _audit(cur, actor, action, "booking", booking_id,
               {"status": booking["status"]}, {"status": target, "reason": reason})
        if target == "REJECTED":
            _queue_booking_notice_tx(cur, booking, "rejected")
    return get_booking(db, booking_id)


def check_in(db: Database, booking_id: str, *, actor: str) -> dict:
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "CHECKED_IN")
        cur.execute("UPDATE bookings SET status = 'CHECKED_IN' WHERE booking_id = %s",
                    (booking_id,))
        cur.execute("UPDATE booking_beds SET status = 'CHECKED_IN' WHERE booking_id = %s "
                    "AND status = 'CONFIRMED'", (booking_id,))
        cur.execute("UPDATE beds SET status = 'OCCUPIED' WHERE bed_id = ANY(%s)",
                    (booking_beds(db, booking_id),))
        _audit(cur, actor, "checked_in", "booking", booking_id,
               {"status": booking["status"]}, {"status": "CHECKED_IN"})
    return get_booking(db, booking_id)


def check_out(db: Database, booking_id: str, *, actor: str) -> dict:
    """Check out and put every bed in the party into CLEANING, ready to reuse."""
    with db.transaction() as cur:
        cur.execute("SELECT * FROM bookings WHERE booking_id = %s FOR UPDATE", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            raise NotFound(f"booking {booking_id} not found")
        _require_transition(booking["status"], "CHECKED_OUT")
        beds = booking_beds(db, booking_id)
        cur.execute("UPDATE bookings SET status = 'CHECKED_OUT' WHERE booking_id = %s",
                    (booking_id,))
        cur.execute("UPDATE booking_beds SET status = 'CHECKED_OUT' WHERE booking_id = %s "
                    "AND status = 'CHECKED_IN'", (booking_id,))
        cur.execute("UPDATE beds SET status = 'CLEANING' WHERE bed_id = ANY(%s)", (beds,))
        _audit(cur, actor, "checked_out", "booking", booking_id,
               {"status": booking["status"]}, {"status": "CHECKED_OUT", "beds": beds})
    return get_booking(db, booking_id)


def mark_bed_ready(db: Database, bed_id: str, *, actor: str) -> None:
    """Cleaning finished: the bed becomes AVAILABLE again."""
    with db.transaction() as cur:
        cur.execute("SELECT status FROM beds WHERE bed_id = %s", (bed_id,))
        bed = cur.fetchone()
        if not bed:
            raise NotFound(f"bed {bed_id} not found")
        cur.execute("UPDATE beds SET status = 'AVAILABLE' WHERE bed_id = %s", (bed_id,))
        _audit(cur, actor, "bed_ready", "bed", bed_id,
               {"status": bed["status"]}, {"status": "AVAILABLE"})


def live_booking_for(db: Database, phone: str) -> dict | None:
    """This guest's booking that is still going, or None.

    A booking is "still going" until it is checked out. A cancelled, rejected or
    expired one is finished, so the guest is free to book again. The most recent
    is returned, so the answer is about the booking the guest is thinking of.
    """
    return db.query_one(
        """
        SELECT bk.booking_id, bk.reference, bk.status, bk.check_in_at,
               bk.check_out_at, bk.charges_total
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE g.phone = %s
          AND bk.status NOT IN ('CANCELLED', 'REJECTED', 'EXPIRED', 'NO_SHOW',
                                'CHECKED_OUT')
        ORDER BY bk.created_at DESC
        LIMIT 1
        """,
        (phone,),
    )


def current_bookings(db: Database, *, limit: int = 50) -> list[dict]:
    """Bookings that are still live: requested, accepted, proof sent, confirmed
    or in house. Finished ones (checked out, cancelled, rejected, expired) are
    left out, so the list answers "what is on the books now"."""
    return db.query_all(
        """
        SELECT bk.reference, bk.status, bk.bed_id, bk.check_in_at, bk.check_out_at,
               bk.charges_total, g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('REQUESTED','ACCEPTED','PROOF_SENT','CONFIRMED','CHECKED_IN')
        ORDER BY bk.check_in_at, bk.reference
        LIMIT %s
        """,
        (limit,),
    )


def pending_requests(db: Database) -> list[dict]:
    """Bookings waiting for the owner, whatever the reason.

    Two things wait on the owner: a fresh request, and a booking whose guest has
    sent payment proof. Both are shown together, because both are decisions the
    owner owes someone. The status is carried through so the page and the chat
    can say which of the two it is.
    """
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.status, bk.check_in_at, bk.check_out_at,
               bk.charges_total, g.full_name, g.phone, bk.bed_id
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('REQUESTED', 'PROOF_SENT')
        ORDER BY bk.created_at
        """
    )


def arrivals_on(db: Database, day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_in_at, bk.status,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CONFIRMED','CHECKED_IN')
          AND bk.check_in_at >= %s AND bk.check_in_at < %s
        ORDER BY bk.check_in_at
        """,
        (start, end),
    )


def departures_on(db: Database, day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_out_at, bk.status,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CONFIRMED','CHECKED_IN','CHECKED_OUT')
          AND bk.check_out_at >= %s AND bk.check_out_at < %s
        ORDER BY bk.check_out_at
        """,
        (start, end),
    )


def overdue_checkouts(db: Database, now: datetime | None = None) -> list[dict]:
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_out_at,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status IN ('CHECKED_IN','CONFIRMED')
          AND bk.check_out_at < COALESCE(%s, now())
        ORDER BY bk.check_out_at
        """,
        (now,),
    )


def in_house(db: Database) -> list[dict]:
    return db.query_all(
        """
        SELECT bk.booking_id, bk.reference, bk.bed_id, bk.check_in_at, bk.check_out_at,
               g.full_name, g.phone
        FROM bookings bk JOIN guests g ON g.guest_id = bk.guest_id
        WHERE bk.status = 'CHECKED_IN'
        ORDER BY bk.check_out_at
        """
    )
