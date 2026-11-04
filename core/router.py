"""Deterministic router.

Answers the common questions with code and SQL, with no AI call at all. This is
what keeps the running cost low and the answers exact. The AI is used only for
free text that this router cannot classify with confidence.

Every answer is built from a database query. Nothing here guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core import availability, booking, inbox, payments, pricing
from core.channel.adapter import Button, Prompt
from core.db import Database

HELP = (
    "I can help with room availability, prices, and booking requests for "
    "Sagar PG Hostel. Tell me your check-in date and how many nights you need, "
    "and whether you want AC and food."
)


@dataclass(frozen=True)
class Reply:
    text: str
    handled: bool
    needs_ai: bool = False
    intent: str = "unknown"
    # When set, the guest is offered tappable answers instead of typing. `text`
    # is still the fallback body, so a channel without buttons keeps working.
    prompt: Prompt | None = None


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _rupees(amount: Decimal) -> str:
    return f"Rs {amount:,.0f}"


# --- intent detection ------------------------------------------------------

# Plurals matter: "what are your prices" is one of the most common messages.
_PRICE = re.compile(r"\b(prices?|rates?|costs?|charges?|rent|how much|kitna|"
                    r"evvala|per (day|night|hour))\b", re.I)
_AVAIL = re.compile(r"\b(available beds?|beds? available|any beds?|free beds?|"
                    r"beds? (free|left)|vacanc\w*|vacant|"
                    r"rooms? (free|available|left)|any rooms?|khali)\b", re.I)
_FULL = re.compile(r"\b(full|no (beds?|rooms?)|occupied|housefull)\b", re.I)
_FACIL_INFO = re.compile(r"\b(food|meals|mess|wifi|wi-fi|water|laundry|parking|"
                         r"timing|rules)\b", re.I)
_FACIL = re.compile(r"\b(food|meals|mess|wifi|wi-fi|water|ac|non[- ]?ac|"
                    r"laundry|parking|timing|rules|facilit\w*)\b", re.I)
_HOURS = re.compile(r"\b(hour|hourly|per hour|few hours|short stay)\b", re.I)
_GREET = re.compile(r"^\s*(hi|hii+|hello|hey|namaste|good (morning|evening|afternoon))\b", re.I)
# A bare "menu" or "help" asks for the options. Anchored to the whole message so
# "help me book a bed" is still handled on its merits.
_MENU_WORD = re.compile(r"^\s*(menu|help|options|start)\s*[.!?]*\s*$", re.I)
_THANKS = re.compile(r"\b(thank|thanks|thank you|dhanyavad)\b", re.I)
_NIGHTS = re.compile(r"(\d+)\s*(night|nights|day|days|din|roju)", re.I)
_AC = re.compile(r"\bnon[- ]?ac\b", re.I)


def classify(text: str | None) -> str:
    """Best-effort intent. Returns 'unknown' when the AI should take over."""
    if not text or not text.strip():
        return "empty"
    if _GREET.match(text) or _MENU_WORD.match(text):
        return "greeting"
    if _THANKS.search(text):
        return "thanks"
    if _PRICE.search(text):
        return "price"
    # An availability question may mention "AC" or "food"; availability wins.
    if _AVAIL.search(text) or _FULL.search(text):
        return "availability"
    if _FACIL_INFO.search(text):
        return "facilities"
    return "unknown"


def route(db: Database, text: str | None, *, role: str = "customer",
          now: datetime | None = None, room_type: str | None = None,
          food_included: bool | None = None,
          nights: int | None = None, button_id: str | None = None) -> Reply:
    """Answer if the question is one we handle deterministically.

    Returns `needs_ai=True` when the message should go to the AI layer instead.
    A tapped button arrives as `button_id`, which is matched before the text:
    the id is stable, where the label the guest saw is not.
    """
    if button_id:
        tapped = _menu_button(db, button_id) or _price_button(db, button_id)
        if tapped is not None:
            return tapped

    intent = classify(text)

    if intent in ("empty", "unknown"):
        return Reply("", handled=False, needs_ai=True, intent=intent)

    if intent == "greeting":
        return _greeting_reply(db)

    if intent == "thanks":
        return Reply("You're welcome. Anything else I can help with?", handled=True,
                     intent=intent)

    if intent == "facilities":
        return Reply(_facilities(db, room_type, food_included), handled=True, intent=intent)

    if intent == "price":
        return _price_reply(db, text, room_type, food_included)

    if intent == "availability":
        return Reply(_availability(db, text, now, room_type, nights), handled=True,
                     intent=intent)

    return Reply("", handled=False, needs_ai=True, intent=intent)


# --- deterministic answers -------------------------------------------------

def _price(db: Database, text: str, room_type: str | None,
           food_included: bool | None) -> str:
    """Quote from the owner's active rate plans. Never invents a figure."""
    rows = db.query_all(
        """
        SELECT room_type, food_included, stay_mode, daily_rate
        FROM rate_plans
        WHERE active AND daily_rate IS NOT NULL
        ORDER BY room_type, food_included DESC, stay_mode
        """
    )
    if not rows:
        return ("I don't have current prices to hand. I'll ask the owner and get "
                "back to you.")

    # Group night and day rates for the same room and food option onto one line.
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row["room_type"], row["food_included"])
        grouped.setdefault(key, {})[row["stay_mode"]] = row["daily_rate"]

    label_of = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory mat (no cot)"}
    lines = ["Our current rates per bed are:"]
    for (rt, food), modes in grouped.items():
        label = label_of.get(rt, rt)
        suffix = " with food" if food else ""
        parts = []
        if "NIGHT" in modes:
            parts.append(f"{_rupees(modes['NIGHT'])} a night")
        if "DAY" in modes:
            parts.append(f"{_rupees(modes['DAY'])} a day")
        lines.append(f"- {label}{suffix}: " + " / ".join(parts))

    monthly = pricing.list_monthly_plans(db)
    if monthly:
        lines.append("")
        lines.append("For a monthly (PG) stay:")
        grouped_m: dict[tuple, dict] = {}
        for plan in monthly:
            if plan["plan_kind"] != "STANDARD" or plan["room_type"] == "ANY":
                continue
            key = (plan["room_type"], plan["food_included"])
            grouped_m.setdefault(key, {})[plan["stay_mode"]] = plan["monthly_rate"]
        for (rt, food), modes in grouped_m.items():
            label = label_of.get(rt, rt)
            suffix = " with food" if food else ""
            parts = []
            if "NIGHT" in modes:
                parts.append(f"{_rupees(modes['NIGHT'])} night")
            if "DAY" in modes and modes.get("DAY") != modes.get("NIGHT"):
                parts.append(f"{_rupees(modes['DAY'])} full day")
            lines.append(f"- {label}{suffix}: " + " / ".join(parts) + " a month")
        concession = [p for p in monthly if p["plan_kind"] == "CONCESSION"]
        if concession:
            lines.append(f"- Student concession: {_rupees(concession[0]['monthly_rate'])} "
                         "a month, any room, with food")

    extras = pricing.list_extras(db)
    if extras:
        lines.append("")
        lines.append("Other items:")
        for extra in extras:
            per = {
                "PER_ITEM": "each",
                "PER_STAY": "per stay",
                "PER_MONTH": "per month",
                "PER_NIGHT": "per night",
            }.get(extra["unit"], "")
            lines.append(f"- {extra['name']}: {_rupees(extra['price'])} {per}".rstrip())

    lines.append("")
    lines.append("Tell me your dates and I can check availability.")
    return "\n".join(lines)


# --- the greeting menu, answered by tapping --------------------------------

_MENU = "menu:"

# The menu's own buttons never carry a figure, and the sub-answers reuse the
# price flow, so the rate card stays the only source of a price.
_MENU_TITLES = (
    ("menu:price", "Prices"),
    ("menu:availability", "Availability"),
    ("menu:facilities", "Facilities"),
)


def _greeting_reply(db: Database) -> Reply:
    """Greet with tappable options, falling back to the help text."""
    buttons = tuple(Button(bid, title) for bid, title in _MENU_TITLES)
    prompt = Prompt(
        body="Hello! What would you like to know about Sagar PG Hostel?",
        buttons=buttons)
    return Reply(HELP, handled=True, intent="greeting", prompt=prompt)


def _menu_button(db: Database, button_id: str) -> Reply | None:
    """Answer a menu tap. Returns None if the id is not ours."""
    if not button_id.startswith(_MENU):
        return None
    what = button_id[len(_MENU):]
    if what == "price":
        return _price_reply(db, "", None, None)
    if what == "availability":
        return Reply(_availability(db, "", None, None, None), handled=True,
                     intent="availability")
    if what == "facilities":
        return Reply(_facilities(db, None, None), handled=True, intent="facilities")
    return None


# --- the price flow, answered by tapping -----------------------------------

_ROOM_LABEL = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory"}

# Button ids are stable and machine-readable. The guest sees the title; the
# router answers on the id, so a relabelled or translated title cannot break it.
_PRICE_ROOM = "price:room:"
_PRICE_FOOD = "price:food:"


def _priced_room_types(db: Database) -> list[str]:
    """Room types that have an active rate, in a fixed, readable order."""
    rows = db.query_all(
        "SELECT DISTINCT room_type FROM rate_plans WHERE active AND daily_rate IS NOT NULL"
    )
    present = {row["room_type"] for row in rows}
    return [rt for rt in ("AC", "NON_AC", "DORM") if rt in present]


def _price_reply(db: Database, text: str, room_type: str | None,
                 food_included: bool | None) -> Reply:
    """Offer the price question as taps, falling back to the full text list.

    The full list stays the fallback body, so a channel that cannot show buttons
    behaves exactly as before.
    """
    full_text = _price(db, text, room_type, food_included)

    if room_type:
        return _price_for_room(db, room_type, food_included)

    rooms = _priced_room_types(db)
    if not rooms:
        return Reply(full_text, handled=True, intent="price")

    # One room type needs no question at all; go straight to its food choice.
    if len(rooms) == 1:
        return _price_for_room(db, rooms[0], food_included)

    choices = tuple(Button(f"{_PRICE_ROOM}{rt}", _ROOM_LABEL.get(rt, rt))
                    for rt in rooms)
    body = "Our rates are per bed. Which room type would you like?"
    if len(choices) <= 3:
        prompt = Prompt(body=body, buttons=choices)
    else:
        prompt = Prompt(body=body, rows=choices, button_text="Choose a room")

    return Reply(full_text, handled=True, intent="price", prompt=prompt)


def _price_for_room(db: Database, room_type: str, food_included: bool | None) -> Reply:
    """Ask the food question if the room offers both, else quote directly."""
    try:
        offered = pricing.food_options(db, room_type)
    except Exception:
        offered = []

    if not offered:
        return Reply(_price(db, "", room_type, food_included), handled=True, intent="price")

    if len(offered) == 1:
        return _quote(db, room_type, offered[0])

    if food_included is not None:
        return _quote(db, room_type, food_included)

    label = _ROOM_LABEL.get(room_type, room_type)
    buttons = tuple(
        Button(f"{_PRICE_FOOD}{room_type}:{'1' if food else '0'}",
               "With food" if food else "Without food")
        for food in (True, False)
    )
    body = f"{label} room. With food or without?"
    return Reply(body, handled=True, intent="price",
                 prompt=Prompt(body=body, buttons=buttons))


def _quote(db: Database, room_type: str, food_included: bool) -> Reply:
    """The exact rate for one room and food choice, read from the rate card."""
    label = _ROOM_LABEL.get(room_type, room_type)
    suffix = " with food" if food_included else " without food"

    nightly = pricing.rates_for(db, room_type, food_included)
    lines = [f"{label}{suffix}:"]
    if "NIGHT" in nightly:
        lines.append(f"{_rupees(nightly['NIGHT'].daily_rate)} a night")
    if "DAY" in nightly:
        lines.append(f"{_rupees(nightly['DAY'].daily_rate)} a day")
    if len(lines) == 1:
        lines.append("I don't have that rate to hand. I'll ask the owner.")

    monthly = [p for p in pricing.list_monthly_plans(db)
               if p["room_type"] == room_type and p["food_included"] == food_included
               and p["plan_kind"] == "STANDARD"]
    if monthly:
        by_mode = {p["stay_mode"]: p["monthly_rate"] for p in monthly}
        parts = []
        if "NIGHT" in by_mode:
            parts.append(f"{_rupees(by_mode['NIGHT'])} night")
        if "DAY" in by_mode and by_mode.get("DAY") != by_mode.get("NIGHT"):
            parts.append(f"{_rupees(by_mode['DAY'])} full day")
        if parts:
            lines.append("")
            lines.append("For a monthly stay: " + " / ".join(parts))

    return Reply("\n".join(lines), handled=True, intent="price")


def _price_button(db: Database, button_id: str) -> Reply | None:
    """Answer a price-flow tap. Returns None if the id is not ours."""
    if not button_id.startswith((_PRICE_ROOM, _PRICE_FOOD)):
        return None
    parts = button_id.split(":")
    if len(parts) == 3 and parts[1] == "room":
        room_type = parts[2]
        if room_type not in _ROOM_LABEL:
            return None
        return _price_for_room(db, room_type, None)
    if len(parts) == 4 and parts[1] == "food":
        room_type, food = parts[2], parts[3]
        if room_type not in _ROOM_LABEL or food not in ("0", "1"):
            return None
        return _quote(db, room_type, food == "1")
    return None


def _availability(db: Database, text: str, now: datetime | None,
                  room_type: str | None, nights: int | None) -> str:
    start = (now or datetime.now().astimezone()).replace(
        hour=12, minute=0, second=0, microsecond=0)
    match = _NIGHTS.search(text or "")
    if match:
        nights = int(match.group(1))
    nights = nights or 1
    end = start + timedelta(days=nights)

    summary = availability.availability_summary(db, start, end)
    free = summary["available_beds"]
    if free == 0:
        return ("We're fully booked for those dates. I can note your details and "
                "the owner will contact you if a bed frees up.")

    parts = [f"We have {free} bed{'s' if free != 1 else ''} free for "
             f"{nights} night{'s' if nights != 1 else ''}."]
    for room_type_name, counts in sorted(summary["by_room_type"].items()):
        label = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory"}.get(
            room_type_name, room_type_name)
        parts.append(f"- {label}: {counts['available_beds']} free")
    parts.append("Send your check-in date and I'll hold a bed for the owner to approve.")
    return "\n".join(parts)


def _facilities(db: Database, room_type: str | None, food_included: bool | None) -> str:
    """Describe only the room types that actually exist in the database."""
    rows = db.query_all(
        "SELECT DISTINCT room_type FROM rooms WHERE active ORDER BY room_type"
    )
    kinds = {row["room_type"] for row in rows}
    lines = ["Sagar PG Hostel offers:"]
    if "AC" in kinds:
        lines.append("- AC rooms")
    if "NON_AC" in kinds:
        lines.append("- Non-AC rooms")
    if "DORM" in kinds:
        lines.append("- A dormitory with mat spaces, no cots")
    lines.append("Food can be included with a room. Ask me about rates or availability.")
    return "\n".join(lines)


# --- owner-side deterministic answers --------------------------------------

def owner_answer(db: Database, text: str, *, now: datetime | None = None) -> Reply:
    """Answer the owner's common questions with SQL, without an AI call."""
    lowered = (text or "").lower()
    now = now or datetime.now().astimezone()

    if re.search(r"\b(vacan\w*|vacant|free beds?|available beds?|bed availability)\b", lowered):
        start = now.replace(hour=12, minute=0, second=0, microsecond=0)
        summary = availability.availability_summary(db, start, start + timedelta(days=1))
        return Reply(
            f"{summary['available_beds']} of {summary['total_beds']} beds are free right now. "
            f"{summary['occupied_beds']} occupied, {summary['blocked_beds']} blocked. "
            f"Full rooms: {', '.join(summary['full_rooms']) or 'none'}.",
            handled=True, intent="owner_availability")

    if re.search(r"\b(outstanding|dues|owes|not paid|hasn'?t paid|unpaid)\b", lowered):
        rows = payments.outstanding_bookings(db)
        if not rows:
            return Reply("No outstanding balances.", handled=True, intent="owner_dues")
        lines = [f"{len(rows)} booking(s) with a balance:"]
        for row in rows[:15]:
            lines.append(f"- {row['full_name'] or row['phone']}: "
                         f"{_rupees(row['balance_due'])} due ({row['reference']})")
        return Reply("\n".join(lines), handled=True, intent="owner_dues")

    # Specific checks first: "pending payments" must not be read as "pending
    # booking requests".
    if re.search(r"\b(unverified|screenshot\w*|awaiting (payment|verification)|"
                 r"payments? (pending|awaiting)|payment verification)\b", lowered):
        rows = payments.unverified_payments(db)
        if not rows:
            return Reply("No payments are awaiting verification.", handled=True,
                         intent="owner_unverified")
        lines = [f"{len(rows)} payment(s) awaiting verification:"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}: {_rupees(row['amount'])} "
                         f"by {row['method']} ({row['reference']})")
        return Reply("\n".join(lines), handled=True, intent="owner_unverified")

    if re.search(r"\b(pending|waiting|awaiting approval|approval required)\b", lowered):
        rows = booking.pending_requests(db)
        if not rows:
            return Reply("No booking requests are waiting for approval.", handled=True,
                         intent="owner_pending")
        lines = [f"{len(rows)} request(s) awaiting your approval:"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}: bed {row['bed_id']}, "
                         f"{row['check_in_at']:%d %b} to {row['check_out_at']:%d %b} "
                         f"({row['reference']})")
        return Reply("\n".join(lines), handled=True, intent="owner_pending")

    if re.search(r"\b(revenue|collect\w*|collection|earned|income)\b", lowered):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = payments.revenue_between(db, start, now + timedelta(days=1))
        return Reply(f"Verified revenue this month: {_rupees(total)}.", handled=True,
                     intent="owner_revenue")

    if re.search(r"\b(ai|model|token|gemini|openai)\b.*\b(cost|spend|usage|bill)\b", lowered):
        from ai import usage as ai_usage
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        summary = ai_usage.cost_between(db, start, now + timedelta(days=1))
        return Reply(
            f"AI usage this month: {summary['calls']} call(s), "
            f"{summary['input_tokens']} tokens in, {summary['output_tokens']} out. "
            f"Estimated cost ${summary['cost_usd']}.",
            handled=True, intent="owner_ai_cost")

    if re.search(r"\b(arriv\w*|checking in|check[- ]?ins?)\b", lowered):
        day = now + timedelta(days=1) if "tomorrow" in lowered else now
        rows = booking.arrivals_on(db, day)
        if not rows:
            return Reply("No arrivals for that day.", handled=True, intent="owner_arrivals")
        lines = [f"{len(rows)} arrival(s):"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']} to bed {row['bed_id']} "
                         f"at {row['check_in_at']:%H:%M}")
        return Reply("\n".join(lines), handled=True, intent="owner_arrivals")

    if re.search(r"\b(overdue|not checked out|departur\w*|leaving|due to leave)\b", lowered):
        rows = booking.overdue_checkouts(db)
        if not rows:
            return Reply("No overdue checkouts.", handled=True, intent="owner_overdue")
        lines = [f"{len(rows)} overdue checkout(s):"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}, bed {row['bed_id']}, "
                         f"due {row['check_out_at']:%d %b %H:%M}")
        return Reply("\n".join(lines), handled=True, intent="owner_overdue")

    if re.search(r"\b(unanswered|no reply|not replied|no response|"
                 r"not received a reply|without a reply)\b", lowered):
        rows = unanswered(db)
        if not rows:
            return Reply("Every enquiry has been answered.", handled=True,
                         intent="owner_unanswered")
        lines = [f"{len(rows)} unanswered enquiry(ies):"]
        for row in rows[:15]:
            lines.append(f"- {row['phone']} since {row['last_message_at']:%d %b %H:%M}")
        return Reply("\n".join(lines), handled=True, intent="owner_unanswered")

    if re.search(r"\b(enquir\w*|enquries|messages?|questions?|how many people)\b", lowered):
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = inbox.enquiries_between(db, day_start, day_start + timedelta(days=1))
        open_now = inbox.open_enquiries(db)
        waiting = unanswered(db)
        return Reply(
            f"Enquiries today: {len(today)}.\n"
            f"Open enquiries: {len(open_now)}.\n"
            f"Waiting for a reply: {len(waiting)}.",
            handled=True, intent="owner_enquiries")

    if re.search(r"\b(occupied|in house|staying|checked in|guests? (in|staying))\b", lowered):
        rows = booking.in_house(db)
        if not rows:
            return Reply("No guests are currently checked in.", handled=True,
                         intent="owner_in_house")
        lines = [f"{len(rows)} guest(s) in house:"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}, bed {row['bed_id']}, "
                         f"until {row['check_out_at']:%d %b}")
        return Reply("\n".join(lines), handled=True, intent="owner_in_house")

    # "show me the full hostel status" and similar. This is the one question the
    # owner asks most often, so it must not cost an AI call.
    if re.search(r"\b(full|complete|overall|entire)\b.*\b(status|report|summary|"
                 r"picture|update)\b|^status$|^report$", lowered):
        return _full_status(db, now)

    return Reply("", handled=False, needs_ai=True, intent="unknown")


def _full_status(db: Database, now: datetime) -> Reply:
    """One screen answering "how is the hostel doing right now"."""
    start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    summary = availability.availability_summary(db, start, start + timedelta(days=1))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue = payments.revenue_between(db, month_start, now + timedelta(days=1))

    pending = booking.pending_requests(db)
    in_house = booking.in_house(db)
    arrivals = booking.arrivals_on(db, now + timedelta(days=1))
    overdue = booking.overdue_checkouts(db)
    dues = payments.outstanding_bookings(db)
    unverified = payments.unverified_payments(db)
    waiting_reply = unanswered(db)

    lines = [
        f"Hostel status, {now:%d %b %Y %H:%M}",
        "",
        f"Beds: {summary['available_beds']} free of {summary['total_beds']} "
        f"({summary['occupied_beds']} occupied, {summary['held_beds']} held, "
        f"{summary['blocked_beds']} blocked)",
        f"Full rooms: {', '.join(summary['full_rooms']) or 'none'}",
        f"Guests in house: {len(in_house)}",
        f"Arrivals tomorrow: {len(arrivals)}",
        f"Overdue checkouts: {len(overdue)}",
        "",
        f"Booking requests awaiting you: {len(pending)}",
        f"Unanswered enquiries: {len(waiting_reply)}",
        f"Payments awaiting verification: {len(unverified)}",
        f"Bookings with a balance: {len(dues)}",
        "",
        f"Verified revenue this month: {_rupees(revenue)}",
    ]
    return Reply("\n".join(lines), handled=True, intent="owner_status")


def unanswered(db: Database) -> list[dict]:
    return inbox.unanswered_enquiries(db)
