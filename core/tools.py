"""The tools the AI may call.

The AI never states a fact it did not get from one of these. Every tool is plain
code and SQL, so availability and prices are exact regardless of which model is
configured.

Two rules hold here:

  * Read tools answer from the database. They never guess.
  * The only write tool creates a booking *request*. Nothing here can confirm a
    booking, take a payment or change a guest's record. Those stay with the owner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ai.adapter import ToolSpec
from core import availability, payments, pricing
from core.db import Database
from core.errors import DomainError

log = logging.getLogger(__name__)

# Roles, named here so the register can gate tools without importing security
# (which would be a cycle).
_OWNER = "owner"
_STAFF = "staff"

# A booking request holds its bed for this long before the hold expires.
HOLD_MINUTES = 60


@dataclass(frozen=True)
class ToolContext:
    """What a tool is allowed to know about the caller."""

    db: Database
    phone: str
    role: str
    # Side effects worth telling the caller about after the tools have run, such
    # as a booking request that the owner should be asked to approve. A tool
    # appends here instead of talking to the channel itself, so a tool still
    # cannot send a message on its own.
    events: list[dict] = field(default_factory=list)


class Tool:
    def __init__(self, name: str, description: str, parameters: dict,
                 run: Callable[[ToolContext, dict], Any], *, owner_only: bool = False,
                 roles: tuple[str, ...] | None = None):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.run = run
        self.owner_only = owner_only
        # When set, only these roles may see or run the tool.
        self.roles = roles

    def allows(self, role: str) -> bool:
        if self.roles is not None:
            return role in self.roles
        return not self.owner_only or role == _OWNER

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


def _date(value: str | None, *, default: datetime | None = None) -> datetime:
    """Parse a date the model produced. Refuses anything ambiguous."""
    if not value:
        if default is None:
            raise DomainError("a date is required")
        return default
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            continue
    raise DomainError(f"could not read the date {value!r}; use YYYY-MM-DD")


# --- read tools ------------------------------------------------------------

def _check_availability(ctx: ToolContext, args: dict) -> dict:
    start = _date(args.get("check_in"), default=datetime.now().astimezone())
    end = _date(args.get("check_out"), default=start + timedelta(days=1))
    if end <= start:
        raise DomainError("the check-out date must be after the check-in date")

    room_type = (args.get("room_type") or "").upper() or None
    if room_type == "NONAC":
        room_type = "NON_AC"

    beds = availability.list_bed_availability(ctx.db, start, end)
    if room_type:
        beds = [b for b in beds if b.room_type == room_type]

    free = [b for b in beds if b.available]
    return {
        "check_in": start.date().isoformat(),
        "check_out": end.date().isoformat(),
        "room_type": room_type or "any",
        "beds_free": len(free),
        "beds_checked": len(beds),
        "available_bed_ids": [b.bed_id for b in free][:20],
        "note": ("No beds of this type are free for those dates."
                 if not free else "These bed ids are free for the whole period."),
    }


def _get_prices(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.db.query_all(
        "SELECT name, room_type, food_included, stay_mode, daily_rate, hourly_rate, min_hours "
        "FROM rate_plans WHERE active = true ORDER BY room_type, food_included, stay_mode")
    extras = ctx.db.query_all(
        "SELECT name, price, unit FROM extras WHERE active ORDER BY name")
    return {
        "rates": [
            {"name": r["name"], "room_type": r["room_type"],
             "food_included": r["food_included"],
             "stay_mode": r["stay_mode"],
             "per_day": str(r["daily_rate"]) if r["daily_rate"] is not None else None,
             "per_hour": str(r["hourly_rate"]) if r["hourly_rate"] is not None else None,
             "minimum_hours": r["min_hours"]}
            for r in rows
        ],
        "extras": [
            {"name": e["name"], "price": str(e["price"]), "unit": e["unit"]}
            for e in extras
        ],
        "currency": "INR",
        "note": ("These are the owner's current rates. Quote them exactly. "
                 "A NIGHT rate applies to an overnight stay, a DAY rate to a "
                 "daytime stay. Never combine or average them."),
    }


def _get_facilities(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.db.query_all(
        "SELECT room_id, room_type, total_beds FROM rooms WHERE active = true "
        "ORDER BY room_id")
    return {
        "room_types": sorted({r["room_type"] for r in rows}),
        "rooms": [{"room_id": r["room_id"], "type": r["room_type"],
                   "beds": r["total_beds"]} for r in rows],
        "note": "Only describe room types that appear here.",
    }


def _quote_stay(ctx: ToolContext, args: dict) -> dict:
    """A computed price for a specific stay. Never a model's arithmetic."""
    start = _date(args.get("check_in"))
    end = _date(args.get("check_out"))
    room_type = (args.get("room_type") or "NON_AC").upper()
    if room_type == "NONAC":
        room_type = "NON_AC"
    food = args.get("food_included")
    if food is not None:
        food = bool(food)
    stay_mode = (args.get("stay_mode") or "NIGHT").upper()
    if stay_mode in ("NIGHTLY", "OVERNIGHT"):
        stay_mode = "NIGHT"
    elif stay_mode in ("DAILY", "DAYTIME"):
        stay_mode = "DAY"

    # Price any extras the customer asked about, e.g. {"New towel": 2}.
    extras_amount = 0
    extra_lines: list[dict] = []
    requested_extras = args.get("extras") or {}
    if isinstance(requested_extras, dict) and requested_extras:
        extras_amount, extra_lines = pricing.extras_total(ctx.db, requested_extras)

    result = pricing.quote(ctx.db, room_type=room_type, food_included=food,
                           start=start, end=end, mode=args.get("mode") or "daily",
                           stay_mode=stay_mode, extras=extras_amount)
    out = {
        "room_type": room_type,
        "food_included": result.food_included,
        "stay_mode": stay_mode,
        "units": result.units,
        "unit_rate": str(result.unit_rate),
        "total": str(result.charges_total),
        "currency": "INR",
        "note": "This total is final for these dates. Do not adjust it.",
    }
    if extra_lines:
        out["extras"] = extra_lines
        out["accommodation_total"] = str(result.base_amount)
    return out


def _quote_monthly_stay(ctx: ToolContext, args: dict) -> dict:
    """A monthly (PG) price. Whole months; never pro-rated by the model."""
    room_type = (args.get("room_type") or "NON_AC").upper()
    if room_type == "NONAC":
        room_type = "NON_AC"
    food = args.get("food_included")
    if food is not None:
        food = bool(food)
    if food is None:
        raise DomainError("say whether the monthly rate includes food")
    months = int(args.get("months") or 1)
    plan_kind = args.get("plan_kind")
    if plan_kind is not None:
        plan_kind = str(plan_kind).upper()
    stay_mode = (args.get("stay_mode") or "NIGHT").upper()
    if stay_mode in ("NIGHTLY", "OVERNIGHT"):
        stay_mode = "NIGHT"
    elif stay_mode in ("DAILY", "DAYTIME", "FULL_DAY"):
        stay_mode = "DAY"

    result = pricing.monthly_quote(ctx.db, room_type=room_type, food_included=food,
                                   months=months, plan_kind=plan_kind,
                                   stay_mode=stay_mode)
    return {
        "room_type": room_type,
        "food_included": result.food_included,
        "plan_kind": result.plan_kind,
        "stay_mode": result.stay_mode,
        "months": result.months,
        "monthly_rate": str(result.monthly_rate),
        "total": str(result.charges_total),
        "currency": "INR",
        "note": ("This total is final for that many whole months. It is not "
                 "pro-rated. This is the same rate for every guest; for any "
                 "other rate, the guest must speak to the owner."),
    }


def _create_booking_request(ctx: ToolContext, args: dict) -> dict:
    """Create a request. The owner still has to approve it."""
    from core import booking

    start = _date(args.get("check_in"))
    end = _date(args.get("check_out"), default=start + timedelta(days=1))
    full_name = (args.get("full_name") or "").strip()
    if len(full_name) < 2:
        raise DomainError("a guest name is required before a request can be made")

    room_type = (args.get("room_type") or "").upper()
    if room_type == "NONAC":
        room_type = "NON_AC"

    guests = int(args.get("guests") or 1)
    if guests < 1:
        raise DomainError("there must be at least one guest")

    terms = (args.get("payment_terms") or "ADVANCE").upper()
    if terms not in ("FULL", "ADVANCE"):
        raise DomainError("payment terms must be FULL or ADVANCE")

    beds = availability.list_bed_availability(ctx.db, start, end)
    free = [b for b in beds if b.available]
    if room_type:
        free = [b for b in free if b.room_type == room_type]
    if len(free) < guests:
        raise DomainError(
            f"only {len(free)} bed(s) of that type are free for those dates, "
            f"but {guests} are needed")

    request = booking.create_request(
        ctx.db, phone=ctx.phone, full_name=full_name,
        gender=(args.get("gender") or "UNKNOWN").upper(),
        bed_ids=[b.bed_id for b in free[:guests]],
        check_in_at=start, check_out_at=end,
        food_included=args.get("food_included"),
        stay_mode=(args.get("stay_mode") or "NIGHT").upper(),
        guests_count=guests, payment_terms=terms,
        name_source="GUEST_TYPED",
        hold_minutes=HOLD_MINUTES,
    )
    ctx.events.append({
        "type": "booking_requested",
        "reference": request.reference,
        "bed_ids": [b.bed_id for b in free[:guests]],
        "guests": guests,
        "check_in": start.date().isoformat(),
        "check_out": end.date().isoformat(),
        "charges_total": str(request.charges_total),
        "advance_due": str(request.advance_due),
        "payment_terms": terms,
        "guest_name": full_name,
    })
    return {
        "reference": request.reference,
        # `bed_id` is the first bed, kept for the single-guest caller; the full
        # party is in `bed_ids`.
        "bed_id": free[0].bed_id,
        "bed_ids": [b.bed_id for b in free[:guests]],
        "guests": guests,
        "charges_total": str(request.charges_total),
        "advance_due": str(request.advance_due),
        "payment_terms": terms,
        "status": "REQUESTED",
        "note": ("This is a request only, not a confirmed booking. Tell the guest "
                 "the owner will confirm shortly. Do not say the booking is confirmed. "
                 "Quote charges_total and advance_due exactly; never compute a figure."),
    }


def _my_bookings(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.db.query_all(
        """
        SELECT b.reference, b.status, b.bed_id, b.check_in_at, b.check_out_at,
               b.food_included
        FROM bookings b JOIN guests g ON g.guest_id = b.guest_id
        WHERE g.phone = %s
        ORDER BY b.created_at DESC LIMIT 5
        """,
        (ctx.phone,),
    )
    return {
        "bookings": [
            {"reference": r["reference"], "status": r["status"], "bed_id": r["bed_id"],
             "check_in": r["check_in_at"].date().isoformat(),
             "check_out": r["check_out_at"].date().isoformat(),
             "food_included": r["food_included"]}
            for r in rows
        ]
    }


# --- owner-only tools ------------------------------------------------------

def _owner_summary(ctx: ToolContext, args: dict) -> dict:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    summary = availability.availability_summary(ctx.db, now, start + timedelta(days=1))
    return {
        "total_beds": summary["total_beds"],
        "available_beds": summary["available_beds"],
        "occupied_beds": summary["occupied_beds"],
        "blocked_beds": summary["blocked_beds"],
        "full_rooms": summary["full_rooms"],
    }


# --- billing tools ---------------------------------------------------------

def _my_bill(ctx: ToolContext, args: dict) -> dict:
    """The caller's own bill and balance. Computed live, never remembered."""
    from core import billing

    row = ctx.db.query_one(
        """
        SELECT b.booking_id, b.reference, b.status, b.check_in_at, b.check_out_at,
               b.food_included, b.charges_total, b.payment_terms, b.advance_due
        FROM bookings b JOIN guests g ON g.guest_id = b.guest_id
        WHERE g.phone = %s
        ORDER BY b.created_at DESC LIMIT 1
        """,
        (ctx.phone,))
    if not row:
        return {"bookings": [], "note": "This number has no booking yet."}

    balance = payments.get_balance(ctx.db, str(row["booking_id"]))
    extras = billing.list_booking_extras(ctx.db, str(row["booking_id"]))
    return {
        "reference": row["reference"],
        "status": row["status"],
        "check_in": row["check_in_at"].date().isoformat(),
        "check_out": row["check_out_at"].date().isoformat(),
        "food_included": row["food_included"],
        "payment_terms": row["payment_terms"],
        "room_charge": str(balance.charges_total),
        "extras": [{"name": e["name"], "amount": str(e["amount"])} for e in extras],
        "verified_paid": str(balance.verified_paid),
        "awaiting_verification": str(balance.unverified_pending),
        "balance_due": str(balance.balance_due),
        "currency": "INR",
        "note": "These figures come from the booking record. Quote them exactly.",
    }


def _add_extra(ctx: ToolContext, args: dict) -> dict:
    """Put an extra on a stay. Staff and owner only."""
    from core import billing

    booking = _booking_for(ctx, args.get("reference"))
    name = (args.get("name") or "").strip()
    if not name:
        raise DomainError("which extra?")
    quantity = int(args.get("quantity") or 1)
    result = billing.add_extra(ctx.db, booking_id=str(booking["booking_id"]),
                               name=name, quantity=quantity, actor=ctx.phone)
    return {
        "reference": booking["reference"],
        "extra": result["name"],
        "quantity": result["quantity"],
        "amount": result["amount"],
        "currency": "INR",
        "note": "Added to the guest's bill. It will appear on the invoice.",
    }


def _record_payment_tool(ctx: ToolContext, args: dict) -> dict:
    """Record money against a booking. It stays unverified until the owner says so."""
    amount = args.get("amount")
    if amount in (None, ""):
        raise DomainError("how much was paid?")
    booking = _booking_for(ctx, args.get("reference"))
    method = (args.get("method") or "CASH").upper().replace(" ", "_")
    if method == "BANKTRANSFER":
        method = "BANK_TRANSFER"
    refundable = args.get("refundable")
    refundable = True if refundable is None else bool(refundable)
    payment_id = payments.record_payment(
        ctx.db, booking_id=str(booking["booking_id"]), guest_id=None,
        amount=amount, method=method, actor=ctx.phone,
        txn_ref=(args.get("txn_ref") or None),
        refundable=refundable, notes="recorded in conversation")
    return {
        "payment_id": payment_id,
        "reference": booking["reference"],
        "amount": str(payments.money(amount)),
        "method": method,
        "refundable": refundable,
        "verified": False,
        "currency": "INR",
        "note": ("Recorded, but NOT verified. The owner must verify it before it "
                 "counts. Tell the guest the owner will confirm it."),
    }


def _issue_invoice(ctx: ToolContext, args: dict) -> dict:
    """Bill a stay. Owner and staff only."""
    from core import billing

    booking = _booking_for(ctx, args.get("reference"))
    invoice = billing.issue_invoice(
        ctx.db, booking_id=str(booking["booking_id"]), actor=ctx.phone,
        discount=args.get("discount") or 0)
    return {
        "invoice": invoice.number,
        "reference": invoice.reference,
        "total": str(invoice.total),
        "paid": str(invoice.paid),
        "balance_due": str(invoice.balance_due),
        "non_refundable": str(invoice.non_refundable),
        "lines": [{"description": line["description"], "amount": str(line["amount"])}
                  for line in invoice.lines],
        "currency": "INR",
    }


def _hostel_facts(ctx: ToolContext, args: dict) -> dict:
    """The owner's recorded facts, never a price and never a guess."""
    from core import hostel_facts

    category = (args.get("category") or "").strip()
    rows = hostel_facts.by_category(ctx.db, category) if category \
        else hostel_facts.all_facts(ctx.db)
    facts = {row["key"]: row["value"] for row in rows}
    if not facts:
        return {"facts": {}, "note": "The owner has not recorded this yet. Say so; "
                                    "do not invent an answer."}
    return {"facts": facts}


def _who_is(ctx: ToolContext, args: dict) -> dict:
    """One guest's whole profile. Owner only."""
    from core import billing

    query = (args.get("query") or "").strip()
    if not query:
        raise DomainError("a name or phone number is required")

    digits = "".join(ch for ch in query if ch.isdigit())
    rows = ctx.db.query_all(
        """
        SELECT g.guest_id, g.full_name, g.phone, g.gender, g.notes, g.created_at
        FROM guests g
        WHERE g.full_name ILIKE %(like)s
           OR (%(digits)s <> '' AND regexp_replace(g.phone, '\\D', '', 'g')
               LIKE '%%' || %(digits)s || '%%')
        ORDER BY g.created_at DESC LIMIT 5
        """,
        {"like": f"%{query}%", "digits": digits})
    if not rows:
        return {"found": 0, "note": f"No guest matches {query!r}."}

    people = []
    for guest in rows:
        bookings = ctx.db.query_all(
            """
            SELECT booking_id, reference, status, bed_id, check_in_at, check_out_at,
                   charges_total
            FROM bookings WHERE guest_id = %s ORDER BY created_at DESC LIMIT 10
            """, (guest["guest_id"],))
        stay = []
        for b in bookings:
            balance = payments.get_balance(ctx.db, str(b["booking_id"]))
            extras = billing.list_booking_extras(ctx.db, str(b["booking_id"]))
            stay.append({
                "reference": b["reference"],
                "status": b["status"],
                "bed": b["bed_id"],
                "from": b["check_in_at"].date().isoformat(),
                "to": b["check_out_at"].date().isoformat(),
                "bill": str(balance.charges_total),
                "paid": str(balance.verified_paid),
                "balance_due": str(balance.balance_due),
                "extras": [{"name": e["name"], "amount": str(e["amount"])}
                           for e in extras],
            })
        people.append({
            "name": guest["full_name"],
            "phone": guest["phone"],
            "since": guest["created_at"].date().isoformat(),
            "notes": guest["notes"],
            "bookings": stay,
        })
    return {"found": len(people), "guests": people, "currency": "INR"}


def _booking_for(ctx: ToolContext, reference: str | None) -> dict:
    """Find a booking by reference, or the only one waiting when unambiguous."""
    from core.errors import ValidationError

    if not reference:
        rows = ctx.db.query_all(
            "SELECT booking_id, reference FROM bookings "
            "WHERE status IN ('REQUESTED','CONFIRMED','CHECKED_IN') "
            "ORDER BY created_at DESC LIMIT 2")
        if len(rows) == 1:
            return rows[0]
        raise ValidationError("please include the booking reference")
    row = ctx.db.query_one(
        "SELECT * FROM bookings WHERE reference = %(ref)s OR booking_id::text = %(ref)s",
        {"ref": reference})
    if not row:
        raise DomainError(f"no booking found for {reference}")
    return row


# --- the register ----------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        "check_availability",
        "Check how many beds are genuinely free for a date range, from the "
        "booking records. Use this before telling anyone a bed is free.",
        {
            "type": "object",
            "properties": {
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "room_type": {"type": "string", "enum": ["AC", "NON_AC", "DORM"]},
            },
            "required": ["check_in", "check_out"],
        },
        _check_availability,
    ),
    Tool(
        "get_prices",
        "The owner's current room rates. Quote only what this returns.",
        {"type": "object", "properties": {}},
        _get_prices,
    ),
    Tool(
        "get_facilities",
        "The room types and rooms that actually exist.",
        {"type": "object", "properties": {}},
        _get_facilities,
    ),
    Tool(
        "quote_stay",
        "Compute the exact total for a specific stay. Never do this arithmetic "
        "yourself; always call this.",
        {
            "type": "object",
            "properties": {
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "room_type": {"type": "string", "enum": ["AC", "NON_AC", "DORM"]},
                "food_included": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["daily", "hourly"]},
            },
            "required": ["check_in", "check_out", "room_type"],
        },
        _quote_stay,
    ),
    Tool(
        "quote_monthly_stay",
        "Compute the exact total for a monthly (PG) stay, in whole months. "
        "The rate is the same for every guest; there is no concession. If a "
        "guest asks for a lower rate, tell them to speak to the owner.",
        {
            "type": "object",
            "properties": {
                "room_type": {"type": "string", "enum": ["AC", "NON_AC", "DORM"]},
                "food_included": {"type": "boolean"},
                "months": {"type": "integer", "minimum": 1},
                "stay_mode": {"type": "string", "enum": ["NIGHT", "DAY"]},
            },
            "required": ["room_type", "food_included", "months"],
        },
        _quote_monthly_stay,
    ),
    Tool(
        "my_bookings",
        "The bookings belonging to the person you are talking to.",
        {"type": "object", "properties": {}},
        _my_bookings,
    ),
    Tool(
        "create_booking_request",
        "Create a booking request for the guest you are talking to. This does "
        "NOT confirm a booking; the owner approves every request. Collect the "
        "guest's name first.",
        {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "room_type": {"type": "string", "enum": ["AC", "NON_AC", "DORM"]},
                "guests": {"type": "integer", "minimum": 1,
                           "description": "how many people are in the party"},
                "food_included": {"type": "boolean"},
                "gender": {"type": "string", "enum": ["MALE", "FEMALE", "OTHER"]},
                "payment_terms": {"type": "string", "enum": ["FULL", "ADVANCE"],
                                  "description": "FULL or ADVANCE (default)"},
            },
            "required": ["full_name", "check_in", "check_out"],
        },
        _create_booking_request,
    ),
    Tool(
        "hostel_summary",
        "Live occupancy figures. Owner only.",
        {"type": "object", "properties": {}},
        _owner_summary,
        owner_only=True,
    ),
    Tool(
        "my_bill",
        "The bill and balance for the person you are talking to. Use this for "
        "'what do I owe' and never calculate a balance yourself.",
        {"type": "object", "properties": {}},
        _my_bill,
    ),
    Tool(
        "add_extra",
        "Add an extra such as a towel or locker to a guest's stay. It goes on "
        "their bill. Staff and owner only.",
        {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "e.g. SPG-2026-000012"},
                "name": {"type": "string", "description": "the extra's name"},
                "quantity": {"type": "integer", "minimum": 1},
            },
            "required": ["reference", "name"],
        },
        _add_extra,
        roles=(_OWNER, _STAFF),
    ),
    Tool(
        "record_payment",
        "Record money received against a booking. It is recorded unverified; the "
        "owner must still verify it. Set refundable false for a booking advance, "
        "which is never returned. Staff and owner only.",
        {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "e.g. SPG-2026-000012"},
                "amount": {"type": "string", "description": "e.g. 200"},
                "method": {"type": "string",
                           "enum": ["UPI", "CASH", "BANK_TRANSFER", "GATEWAY", "OTHER"]},
                "txn_ref": {"type": "string"},
                "refundable": {"type": "boolean"},
            },
            "required": ["reference", "amount"],
        },
        _record_payment_tool,
        roles=(_OWNER, _STAFF),
    ),
    Tool(
        "issue_invoice",
        "Produce the bill for a stay. Re-issuing returns the same bill. Staff and "
        "owner only.",
        {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "discount": {"type": "string"},
            },
            "required": ["reference"],
        },
        _issue_invoice,
        roles=(_OWNER, _STAFF),
    ),
    Tool(
        "who_is",
        "Look up one guest by name or phone number, with their bookings, bills and "
        "balances. Owner only.",
        {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "a name or a phone number"}},
            "required": ["query"],
        },
        _who_is,
        owner_only=True,
    ),
    Tool(
        "hostel_facts",
        "The facts the owner has recorded about the hostel: the address, the "
        "amenities, the policies. Use this for questions about where the hostel "
        "is or what it offers. It never returns a price; use a quote tool for "
        "prices.",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["location", "amenity", "identity", "policy", "general"],
                    "description": "narrow the facts to one category",
                },
            },
        },
        _hostel_facts,
    ),
]

_BY_NAME = {t.name: t for t in TOOLS}


def specs_for(role: str) -> list[ToolSpec]:
    """The tools this role may see. A customer never sees a staff or owner tool."""
    return [t.spec() for t in TOOLS if t.allows(role)]


def execute(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Run one tool call. Returns a result the model can read, never raises."""
    tool = _BY_NAME.get(name)
    if tool is None:
        log.warning("the model asked for an unknown tool %r", name)
        return {"error": f"no tool named {name}"}
    if not tool.allows(ctx.role):
        log.warning("a %s tried to call the tool %r", ctx.role, name)
        return {"error": "that action is not available to you"}

    try:
        return tool.run(ctx, arguments or {})
    except DomainError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # a broken tool must not break the conversation
        log.exception("tool %s failed", name)
        return {"error": f"the check failed: {exc}"}
