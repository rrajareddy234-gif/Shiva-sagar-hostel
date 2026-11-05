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

from core import availability, booking, faq, hostel_facts, inbox, payments, pricing, reviews
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
                         r"timing|rules|facilit\w*|amenit\w*)\b", re.I)
_FACIL = re.compile(r"\b(food|meals|mess|wifi|wi-fi|water|ac|non[- ]?ac|"
                    r"laundry|parking|timing|rules|facilit\w*)\b", re.I)
_HOURS = re.compile(r"\b(hour|hourly|per hour|few hours|short stay)\b", re.I)
# "Where are you?", "address", "location", "how do I reach you", "directions".
_LOCATION = re.compile(r"\b(address|location|located|where\b|directions?|"
                       r"how (do i|to) (reach|come|get there)|landmark|map|"
                       r"ekkada|kahan)\b", re.I)
_GREET = re.compile(r"^\s*(hi|hii+|hello|hey|namaste|good (morning|evening|afternoon))\b", re.I)
# A bare "menu" or "help" asks for the options. Anchored to the whole message so
# "help me book a bed" is still handled on its merits.
_MENU_WORD = re.compile(r"^\s*(menu|help|options|start)\s*[.!?]*\s*$", re.I)
_THANKS = re.compile(r"\b(thank|thanks|thank you|dhanyavad)\b", re.I)
# "Book a Room" typed, and its close cousins. A booking must be caught here so
# it never falls to the AI, which would only chat and never make the request.
_BOOK = re.compile(r"\b(book|booking|reserve|reservation|"
                   r"i want a room|need a room|room (kavali|kavale))\b", re.I)
_BOOKINGS_WORD = re.compile(r"\b(my bookings?|my reservation|my stay|"
                            r"booking status|status of my)\b", re.I)
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
    # Location is checked before price and availability: "where is the hostel"
    # must not be read as an availability question.
    if _LOCATION.search(text):
        return "location"
    # "My bookings" before "book": the phrase contains the word "book", and the
    # guest asking for their own bookings does not want to start a new one.
    if _BOOKINGS_WORD.search(text):
        return "my_bookings"
    # A typed booking request is claimed here, never left to the AI: the AI would
    # chat about it and no request would ever reach the owner.
    if _BOOK.search(text):
        return "book"
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
          nights: int | None = None, button_id: str | None = None,
          phone: str | None = None) -> Reply:
    """Answer if the question is one we handle deterministically.

    Returns `needs_ai=True` when the message should go to the AI layer instead.
    A tapped button arrives as `button_id`, which is matched before the text:
    the id is stable, where the label the guest saw is not.
    """
    if button_id:
        tapped = (_menu_button(db, button_id, phone=phone)
                  or _price_button(db, button_id))
        if tapped is not None:
            return tapped

    intent = classify(text)

    if intent == "empty":
        return Reply("", handled=False, needs_ai=True, intent=intent)

    if intent == "greeting":
        return _greeting_reply(db, phone)

    if intent == "thanks":
        return Reply("You're welcome. Anything else I can help with?", handled=True,
                     intent=intent)

    if intent == "location":
        return Reply(_location(db), handled=True, intent=intent)

    if intent == "my_bookings":
        if not phone:
            return Reply("", handled=False, needs_ai=True, intent=intent)
        from core import booking_flow
        return Reply(booking_flow.my_bookings_reply(db, phone), handled=True,
                     intent=intent)

    if intent == "book":
        if not phone:
            return _booking_prompt(db)
        from core import booking_flow
        flow = booking_flow.start(db, phone=phone)
        return Reply(flow.reply, handled=True, intent=intent, prompt=flow.prompt)

    if intent == "facilities":
        # A stored answer is specific ("yes, there is wifi, password is X"),
        # where the generic list below only says what rooms exist. Try it first
        # so an owner-authored answer wins; fall back to the list when the
        # owner has not written one yet.
        return _faq_reply(db, text) or Reply(
            _facilities(db, room_type, food_included), handled=True, intent=intent)

    if intent == "price":
        return _price_reply(db, text, room_type, food_included)

    if intent == "availability":
        return Reply(_availability(db, text, now, room_type, nights), handled=True,
                     intent=intent, prompt=_book_prompt())

    # Anything the exact rules did not claim -- including 'unknown' -- gets one
    # cheaper chance first: the stored FAQ. A question the owner has already
    # answered costs nothing to answer again. Recorded either way, so the
    # misses become the list of what to store next.
    return _faq_reply(db, text) or Reply("", handled=False, needs_ai=True, intent=intent)


def _faq_reply(db: Database, text: str | None) -> Reply | None:
    """The stored answer as a Reply, or None when nothing is close enough."""
    stored = faq.answer_and_record(db, text)
    if stored:
        return Reply(stored, handled=True, intent="faq")
    return None


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
# Seven options, so WhatsApp shows them as a list rather than three buttons.
# The titles are the owner's own words, in the owner's order.
_MENU_TITLES = (
    ("menu:price", "Rooms & Prices", "AC, non-AC and dorm rates"),
    ("menu:availability", "Check Availability", "Which beds are free"),
    ("menu:location", "Location & Directions", "Where the hostel is"),
    ("menu:facilities", "Food & Amenities", "Food, Wi-Fi, water, security"),
    ("menu:reviews", "Guest Reviews", "What other guests say"),
    ("menu:book", "Book a Room", "Send a booking request"),
    ("menu:bookings", "My Bookings", "Your booking status"),
    ("menu:talk", "Talk to Staff", "We will reply here"),
)


def _greeting_reply(db: Database, phone: str | None = None) -> Reply:
    """Greet with tappable options, falling back to the help text.

    Seven choices, so they are offered as a WhatsApp list: three buttons is the
    most a plain button message allows. The body still carries the help text,
    so a channel without lists, or a guest who prefers typing, is not stuck.

    The guest is greeted by the name we hold, when we hold one. A WhatsApp
    display name is a handle, not a verified name, so it is used to be friendly,
    never to claim we know who the person is.
    """
    rows = tuple(Button(bid, title) for bid, title, _ in _MENU_TITLES)
    name = _guest_first_name(db, phone)
    hello = f"Hello {name}! What would you like to know about Sagar PG Hostel?" \
        if name else "Hello! What would you like to know about Sagar PG Hostel?"
    prompt = Prompt(body=hello, rows=rows, button_text="Choose")
    return Reply(HELP, handled=True, intent="greeting", prompt=prompt)


def _guest_first_name(db: Database, phone: str | None) -> str | None:
    """The guest's first name, or None. Used only to say hello."""
    if not phone:
        return None
    row = db.query_one(
        "SELECT full_name FROM guests WHERE phone = %s AND full_name IS NOT NULL "
        "AND btrim(full_name) <> ''",
        (phone,),
    )
    if not row:
        return None
    first = (row["full_name"] or "").strip().split()[0]
    # A display name may be emoji, a business name or a whole sentence. Keep it
    # only if it looks like a short, plain word.
    if not first or len(first) > 40 or not first[0].isalpha():
        return None
    return first


def _menu_button(db: Database, button_id: str, *,
                 phone: str | None = None) -> Reply | None:
    """Answer a menu tap. Returns None if the id is not ours."""
    if not button_id.startswith(_MENU):
        return None
    what = button_id[len(_MENU):]
    if what == "price":
        return _price_reply(db, "", None, None)
    if what == "availability":
        return Reply(_availability(db, "", None, None, None), handled=True,
                     intent="availability", prompt=_book_prompt())
    if what == "facilities":
        return Reply(_facilities(db, None, None), handled=True, intent="facilities")
    if what == "location":
        return Reply(_location(db), handled=True, intent="location")
    if what == "reviews":
        return Reply(reviews.render_for_guest(db), handled=True, intent="reviews")
    if what == "book":
        if not phone:
            return _booking_prompt(db)
        from core import booking_flow
        flow = booking_flow.start(db, phone=phone)
        return Reply(flow.reply, handled=True, intent="book", prompt=flow.prompt)
    if what == "bookings":
        if not phone:
            return None
        from core import booking_flow
        return Reply(booking_flow.my_bookings_reply(db, phone), handled=True,
                     intent="my_bookings")
    if what == "talk":
        return Reply(
            "Of course. Tell me what you need and I will pass it to the owner, "
            "who will reply here personally.",
            handled=True, intent="talk_to_owner")
    return None


def _booking_prompt(db: Database) -> Reply:
    """Start a booking by asking for the first thing we need: the dates.

    A price is never quoted here. It is computed from the rate card once the
    dates and the room type are known, so no figure is ever guessed.
    """
    return Reply(
        "Happy to help you book. Please tell me:\n"
        "1. Your check-in date (for example 2026-11-10)\n"
        "2. How many nights\n"
        "3. How many guests\n"
        "4. AC or Non-AC, and whether you want food\n\n"
        "I will then show you the exact amount and hold the bed for the owner "
        "to confirm.",
        handled=True, intent="book")


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


def _book_prompt() -> Prompt:
    """One 'Book a Room' button, offered under the availability answer."""
    return Prompt(body="Would you like to book a room?",
                  buttons=(Button("menu:book", "Book a Room"),))


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
    """Describe only what is actually true: rooms from the database, amenities
    from the owner's recorded facts. Nothing is added that the owner has not
    confirmed."""
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
    amenities = hostel_facts.amenities_block(db)
    for sentence in amenities:
        lines.append(f"- {sentence}")
    lines.append("Food can be included with a room. Ask me about rates or availability.")
    return "\n".join(lines)


def _location(db: Database) -> str:
    """The address, from the owner's recorded fact. Never invented.

    If the owner has not recorded an address the guest is told honestly and the
    question is handed on, rather than being given a plausible guess.
    """
    full = hostel_facts.address_block(db)
    if not full:
        return ("I don't have the exact address recorded yet. I've asked the owner "
                "to add it, and they will confirm it for you shortly.")
    lines = [full]
    landmark = hostel_facts.get(db, "address.landmark")
    if landmark and landmark not in full:
        lines.append(f"Landmark: {landmark}")
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

    # "guest details", "customer list", "show me all guests" ask for everyone.
    # These must be checked before the name lookup, or the word "details" is
    # mistaken for a guest's name and the search finds nobody.
    if re.search(r"\b(?:guest|customer|tenant)s?\b", lowered) and (
            re.search(r"\b(all|list|every|show|details?|data|summary|info|"
                      r"information|how many|count|total)\b", lowered)
            # A bare "guests" or "customers" is also a request for the list.
            or re.fullmatch(r"\s*(?:the\s+)?(?:guest|customer|tenant)s?\s*[?.!]*",
                            lowered)):
        return _guest_list(db)

    # "who is Ramesh", "who is 9876543210", "guest Ramesh", "details of Ramesh".
    # Must come before the enquiry matcher, which would otherwise read "who" as
    # a general question.
    found = re.search(r"\b(?:who\s+is|who's|find\s+(?:guest|customer)|"
                      r"guest|customer|tenant|profile\s+of|details\s+of|"
                      r"info\s+(?:on|about)|about)\s+(.+)$", lowered)
    if found:
        query = found.group(1).strip(" ?.!")
        # A generic word here is a request for the list, not a name to search.
        generic = r"(?:the\s+)?(?:owner|staff|me|us|there|details?|data|list|" \
                  r"summary|info|information|all|everyone|everybody|guests?|" \
                  r"customers?|tenants?|today|now)"
        if query and not re.fullmatch(generic, query):
            return _guest_profile(db, query)

    # "show bill SPG-2026-000012", "bill for SPG-..."
    bill = re.search(r"\b(?:show\s+)?(?:the\s+)?(?:bill|invoice)\s+(?:for\s+)?"
                     r"(spg-\d{4}-\d{6}|inv-\d{4}-\d{6})", lowered)
    if bill:
        return _bill_reply(db, bill.group(1).upper())

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

    # "what are people asking that you cannot answer" -- the FAQ worklist. Each
    # entry added is one fewer paid AI call, so this is a money question.
    # Deliberately not keyed on "unanswered", which already means unanswered
    # enquiries to the owner.
    if re.search(r"\b(faq|missed questions?|"
                 r"what are people asking|questions? to (add|store))\b", lowered):
        rows = faq.recent_misses(db, limit=15)
        if not rows:
            return Reply("Nothing is waiting to be added to the FAQ. Every "
                         "question so far has been covered.", handled=True,
                         intent="owner_faq_misses")
        lines = [f"{len(rows)} question(s) the FAQ could not answer:"]
        for row in rows:
            lines.append(f"- {row['asked']} (asked {row['times']}x)")
        lines.append("Tell me to add one and I will store your answer.")
        return Reply("\n".join(lines), handled=True, intent="owner_faq_misses")

    # "show me the full hostel status" and similar. This is the one question the
    # owner asks most often, so it must not cost an AI call.
    if re.search(r"\b(full|complete|overall|entire)\b.*\b(status|report|summary|"
                 r"picture|update)\b|^status$|^report$", lowered):
        return _full_status(db, now)

    return Reply("", handled=False, needs_ai=True, intent="unknown")


def _guest_summary_line(db: Database, guest) -> list[str]:
    """The lines describing one guest: who they are, and their latest stay."""
    from core import billing

    lines = [f"{guest['full_name'] or 'Unnamed'} · {guest['phone']}"]
    if guest["gender"] and guest["gender"] != "UNKNOWN":
        lines.append(f"  {guest['gender'].lower()}, guest since "
                     f"{guest['created_at']:%d %b %Y}")
    else:
        lines.append(f"  Guest since {guest['created_at']:%d %b %Y}")

    bookings = db.query_all(
        """
        SELECT booking_id, reference, status, bed_id, check_in_at, check_out_at
        FROM bookings WHERE guest_id = %s ORDER BY created_at DESC LIMIT 5
        """, (guest["guest_id"],))
    if not bookings:
        lines.append("  No bookings yet.")
    for b in bookings:
        balance = payments.get_balance(db, str(b["booking_id"]))
        extras = billing.list_booking_extras(db, str(b["booking_id"]))
        lines.append(
            f"  - {b['reference']} {b['status']} bed {b['bed_id']}, "
            f"{b['check_in_at']:%d %b} to {b['check_out_at']:%d %b}")
        lines.append(
            f"    Bill {_rupees(balance.charges_total)} · "
            f"paid {_rupees(balance.verified_paid)} · "
            f"due {_rupees(balance.balance_due)}")
        if extras:
            lines.append("    Extras: " + ", ".join(
                f"{e['name']} {_rupees(e['amount'])}" for e in extras))
    if guest["notes"]:
        lines.append(f"  Note: {guest['notes']}")
    return lines


def _guest_list(db: Database, *, limit: int = 15) -> Reply:
    """Everyone on the books, with their stays and balances. Owner only.

    A guest with a live booking comes first, then the most recent additions, so
    the people the owner is actually dealing with today are at the top.
    """
    rows = db.query_all(
        """
        SELECT g.guest_id, g.full_name, g.phone, g.gender, g.notes, g.created_at,
               EXISTS (
                   SELECT 1 FROM bookings b
                   WHERE b.guest_id = g.guest_id
                     AND b.status IN ('REQUESTED','CONFIRMED','CHECKED_IN')
               ) AS active
        FROM guests g
        WHERE g.is_owner IS NOT TRUE AND g.is_staff IS NOT TRUE
        ORDER BY active DESC, g.created_at DESC
        LIMIT %s
        """, (limit,))
    if not rows:
        return Reply("No guests on the books yet.", handled=True, intent="owner_guests")

    total = db.query_one(
        "SELECT count(*) AS n FROM guests "
        "WHERE is_owner IS NOT TRUE AND is_staff IS NOT TRUE")["n"]

    blocks = ["\n".join(_guest_summary_line(db, guest)) for guest in rows]
    header = f"{total} guest(s) on the books"
    if total > len(rows):
        header += f", showing the {len(rows)} most relevant"
    return Reply(header + ":\n\n" + "\n\n".join(blocks), handled=True,
                 intent="owner_guests")


def _guest_profile(db: Database, query: str) -> Reply:
    """One guest, everything about them. Owner only. Figures from the database."""
    from core import billing

    digits = "".join(ch for ch in query if ch.isdigit())
    rows = db.query_all(
        """
        SELECT g.guest_id, g.full_name, g.phone, g.notes, g.created_at
        FROM guests g
        WHERE g.full_name ILIKE %(like)s
           OR (%(digits)s <> '' AND regexp_replace(g.phone, '\\D', '', 'g')
               LIKE '%%' || %(digits)s || '%%')
        ORDER BY g.created_at DESC LIMIT 5
        """,
        {"like": f"%{query}%", "digits": digits})
    if not rows:
        return Reply(f"No guest matches {query!r}.", handled=True, intent="owner_guest")

    blocks: list[str] = []
    for guest in rows:
        lines = [f"{guest['full_name'] or 'Unnamed'} · {guest['phone']}",
                 f"Guest since {guest['created_at']:%d %b %Y}"]
        bookings = db.query_all(
            """
            SELECT booking_id, reference, status, bed_id, check_in_at, check_out_at
            FROM bookings WHERE guest_id = %s ORDER BY created_at DESC LIMIT 5
            """, (guest["guest_id"],))
        if not bookings:
            lines.append("No bookings.")
        for b in bookings:
            balance = payments.get_balance(db, str(b["booking_id"]))
            extras = billing.list_booking_extras(db, str(b["booking_id"]))
            lines.append(
                f"- {b['reference']} {b['status']} bed {b['bed_id']}, "
                f"{b['check_in_at']:%d %b} to {b['check_out_at']:%d %b}")
            lines.append(
                f"  Bill {_rupees(balance.charges_total)} · "
                f"paid {_rupees(balance.verified_paid)} · "
                f"due {_rupees(balance.balance_due)}")
            if extras:
                lines.append("  Extras: " + ", ".join(
                    f"{e['name']} {_rupees(e['amount'])}" for e in extras))
        if guest["notes"]:
            lines.append(f"Note: {guest['notes']}")
        blocks.append("\n".join(lines))

    return Reply("\n\n".join(blocks), handled=True, intent="owner_guest")


def _bill_reply(db: Database, reference: str) -> Reply:
    """An itemized bill for one booking. Owner only."""
    from core import billing

    row = db.query_one(
        "SELECT booking_id FROM bookings WHERE reference = %(ref)s "
        "OR booking_id::text = %(ref)s", {"ref": reference})
    if not row:
        return Reply(f"No booking found for {reference}.", handled=True,
                     intent="owner_bill")
    invoice = billing.issue_invoice(db, booking_id=str(row["booking_id"]),
                                    actor="owner")
    return Reply(billing.render_invoice(invoice), handled=True, intent="owner_bill")


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


# --- staff view ------------------------------------------------------------

_STAFF_MENU = "staff:"
# WhatsApp shows at most three buttons, so the fourth action is stated in the
# body instead of becoming a tap.
_STAFF_MENU_TITLES = (
    ("staff:arrivals", "Arrivals"),
    ("staff:departures", "Departures"),
    ("staff:free", "Free beds"),
)


def _staff_menu() -> Reply:
    body = ("Front desk. What do you need?\n"
            "To record a payment, send the reference, the amount and the method, "
            "for example: SPG-2026-000012 paid 500 by UPI.")
    return Reply(
        body, handled=True, intent="staff_menu",
        prompt=Prompt(
            body=body,
            buttons=tuple(Button(id=i, title=t) for i, t in _STAFF_MENU_TITLES),
        ),
    )


def staff_answer(db: Database, text: str | None, *, button_id: str | None = None,
                 now: datetime | None = None) -> Reply:
    """The front desk's common questions, answered from SQL, with no AI call.

    A staff member sees the day's work and may record what they are told. They
    never see revenue, dues totals, rates or the owner's own reports.
    """
    now = now or datetime.now().astimezone()
    lowered = (text or "").lower().strip()

    if button_id and button_id.startswith(_STAFF_MENU):
        choice = button_id[len(_STAFF_MENU):]
        if choice == "arrivals":
            return _staff_arrivals(db, now)
        if choice == "departures":
            return _staff_departures(db, now)
        if choice == "free":
            return _staff_free(db, now)

    if re.search(r"^\s*(menu|help|options|start)\s*[.!?]*\s*$", lowered):
        return _staff_menu()

    if re.search(r"\b(arriv\w*|checking in|check[- ]?ins?|coming today)\b", lowered):
        return _staff_arrivals(db, now)

    if re.search(r"\b(departur\w*|checking out|check[- ]?outs?|leaving)\b", lowered):
        return _staff_departures(db, now)

    if re.search(r"\b(free|available|vacant|vacanc\w*)\b.*\b(beds?|rooms?)\b|"
                 r"\b(beds?|rooms?)\b.*\b(free|available|vacant)\b", lowered):
        return _staff_free(db, now)

    if re.search(r"\b(occupied|in house|staying|checked in)\b", lowered):
        rows = booking.in_house(db)
        if not rows:
            return Reply("No guests are checked in right now.", handled=True,
                         intent="staff_in_house")
        lines = [f"{len(rows)} guest(s) in house:"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}, bed {row['bed_id']}, "
                         f"until {row['check_out_at']:%d %b}")
        return Reply("\n".join(lines), handled=True, intent="staff_in_house")

    if re.search(r"\b(overdue|not checked out)\b", lowered):
        rows = booking.overdue_checkouts(db)
        if not rows:
            return Reply("No overdue checkouts.", handled=True, intent="staff_overdue")
        lines = [f"{len(rows)} overdue checkout(s):"]
        for row in rows:
            lines.append(f"- {row['full_name'] or row['phone']}, bed {row['bed_id']}, "
                         f"due {row['check_out_at']:%d %b %H:%M}")
        return Reply("\n".join(lines), handled=True, intent="staff_overdue")

    return Reply("", handled=False, needs_ai=True, intent="unknown")


def _staff_arrivals(db: Database, now: datetime) -> Reply:
    rows = booking.arrivals_on(db, now)
    if not rows:
        return Reply("No arrivals today.", handled=True, intent="staff_arrivals")
    lines = [f"{len(rows)} arrival(s) today:"]
    for row in rows:
        lines.append(f"- {row['full_name'] or row['phone']} to bed {row['bed_id']} "
                     f"({row['reference']})")
    return Reply("\n".join(lines), handled=True, intent="staff_arrivals")


def _staff_departures(db: Database, now: datetime) -> Reply:
    rows = booking.departures_on(db, now)
    if not rows:
        return Reply("No departures today.", handled=True, intent="staff_departures")
    lines = [f"{len(rows)} departure(s) today:"]
    for row in rows:
        lines.append(f"- {row['full_name'] or row['phone']}, bed {row['bed_id']} "
                     f"({row['reference']})")
    return Reply("\n".join(lines), handled=True, intent="staff_departures")


def _staff_free(db: Database, now: datetime) -> Reply:
    start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    summary = availability.availability_summary(db, start, start + timedelta(days=1))
    return Reply(
        f"{summary['available_beds']} of {summary['total_beds']} beds are free. "
        f"{summary['occupied_beds']} occupied, {summary['blocked_beds']} blocked. "
        f"Full rooms: {', '.join(summary['full_rooms']) or 'none'}.",
        handled=True, intent="staff_free")
