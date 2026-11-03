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

from core import availability, booking, payments
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
_THANKS = re.compile(r"\b(thank|thanks|thank you|dhanyavad)\b", re.I)
_NIGHTS = re.compile(r"(\d+)\s*(night|nights|day|days|din|roju)", re.I)
_AC = re.compile(r"\bnon[- ]?ac\b", re.I)


def classify(text: str | None) -> str:
    """Best-effort intent. Returns 'unknown' when the AI should take over."""
    if not text or not text.strip():
        return "empty"
    if _GREET.match(text):
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
          nights: int | None = None) -> Reply:
    """Answer if the question is one we handle deterministically.

    Returns `needs_ai=True` when the message should go to the AI layer instead.
    """
    intent = classify(text)

    if intent in ("empty", "unknown"):
        return Reply("", handled=False, needs_ai=True, intent=intent)

    if intent == "greeting":
        return Reply(HELP, handled=True, intent=intent)

    if intent == "thanks":
        return Reply("You're welcome. Anything else I can help with?", handled=True,
                     intent=intent)

    if intent == "facilities":
        return Reply(_facilities(db, room_type, food_included), handled=True, intent=intent)

    if intent == "price":
        return Reply(_price(db, text, room_type, food_included), handled=True, intent=intent)

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
        SELECT room_type, food_included, daily_rate, hourly_rate
        FROM rate_plans
        WHERE active
        ORDER BY room_type, food_included DESC
        """
    )
    if not rows:
        return ("I don't have current prices to hand. I'll ask the owner and get "
                "back to you.")

    lines = ["Our current rates are:"]
    for row in rows:
        label = {
            "AC": "AC",
            "NON_AC": "Non-AC",
            "DORM": "Dormitory mat",
        }.get(row["room_type"], row["room_type"])
        with_food = "with food" if row["food_included"] else "without food"
        if row["daily_rate"] is not None:
            lines.append(f"- {label}, {with_food}: {_rupees(row['daily_rate'])} per day")
        if row["hourly_rate"] is not None:
            lines.append(f"  ({_rupees(row['hourly_rate'])} per hour)")
    lines.append("Prices are per bed. Tell me your dates and I can check availability.")
    return "\n".join(lines)


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

    return Reply("", handled=False, needs_ai=True, intent="unknown")


def unanswered(db: Database) -> list[dict]:
    from core import inbox
    return inbox.unanswered_enquiries(db)
