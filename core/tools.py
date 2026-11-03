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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ai.adapter import ToolSpec
from core import availability, pricing
from core.db import Database
from core.errors import DomainError

log = logging.getLogger(__name__)

# A booking request holds its bed for this long before the hold expires.
HOLD_MINUTES = 60


@dataclass(frozen=True)
class ToolContext:
    """What a tool is allowed to know about the caller."""

    db: Database
    phone: str
    role: str


class Tool:
    def __init__(self, name: str, description: str, parameters: dict,
                 run: Callable[[ToolContext, dict], Any], *, owner_only: bool = False):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.run = run
        self.owner_only = owner_only

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
        "SELECT name, room_type, food_included, daily_rate, hourly_rate, min_hours "
        "FROM rate_plans WHERE active = true ORDER BY room_type, food_included")
    return {
        "rates": [
            {"name": r["name"], "room_type": r["room_type"],
             "food_included": r["food_included"],
             "per_day": str(r["daily_rate"]) if r["daily_rate"] is not None else None,
             "per_hour": str(r["hourly_rate"]) if r["hourly_rate"] is not None else None,
             "minimum_hours": r["min_hours"]}
            for r in rows
        ],
        "currency": "INR",
        "note": "These are the owner's current rates. Quote them exactly.",
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
    food = bool(args.get("food_included"))
    result = pricing.quote(ctx.db, room_type=room_type, food_included=food,
                           start=start, end=end, mode=args.get("mode") or "daily")
    return {
        "room_type": room_type,
        "food_included": food,
        "units": result.units,
        "unit_rate": str(result.unit_rate),
        "total": str(result.charges_total),
        "currency": "INR",
        "note": "This total is final for these dates. Do not adjust it.",
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

    beds = availability.list_bed_availability(ctx.db, start, end)
    free = [b for b in beds if b.available]
    if room_type:
        free = [b for b in free if b.room_type == room_type]
    if not free:
        raise DomainError("no bed of that type is free for those dates")

    request = booking.create_request(
        ctx.db, phone=ctx.phone, full_name=full_name,
        gender=(args.get("gender") or "UNKNOWN").upper(),
        bed_id=free[0].bed_id, check_in_at=start, check_out_at=end,
        food_included=bool(args.get("food_included")),
        hold_minutes=HOLD_MINUTES,
    )
    return {
        "reference": request.reference,
        "bed_id": free[0].bed_id,
        "status": "REQUESTED",
        "note": ("This is a request only, not a confirmed booking. Tell the guest "
                 "the owner will confirm shortly. Do not say the booking is confirmed."),
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
                "food_included": {"type": "boolean"},
                "gender": {"type": "string", "enum": ["MALE", "FEMALE", "OTHER"]},
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
]

_BY_NAME = {t.name: t for t in TOOLS}


def specs_for(role: str) -> list[ToolSpec]:
    """The tools this role may see. A customer never sees an owner tool."""
    from core import security
    owner = role == security.OWNER
    return [t.spec() for t in TOOLS if owner or not t.owner_only]


def execute(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Run one tool call. Returns a result the model can read, never raises."""
    from core import security
    tool = _BY_NAME.get(name)
    if tool is None:
        log.warning("the model asked for an unknown tool %r", name)
        return {"error": f"no tool named {name}"}
    if tool.owner_only and ctx.role != security.OWNER:
        log.warning("a %s tried to call the owner-only tool %r", ctx.role, name)
        return {"error": "that information is not available here"}

    try:
        return tool.run(ctx, arguments or {})
    except DomainError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # a broken tool must not break the conversation
        log.exception("tool %s failed", name)
        return {"error": f"the check failed: {exc}"}
