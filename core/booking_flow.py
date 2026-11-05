"""A tap-driven booking conversation.

The guest books by tapping, so the request is created by code rather than by the
AI. That is the whole point: a request is made every time, and the owner is told
every time, whether or not the AI is up.

Every figure shown comes from the pricing engine. Nothing here computes an
amount, so no rate is ever guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import availability, billing, booking, hostel_facts, inbox, pricing
from core.channel.adapter import Button, Prompt
from core.db import Database
from core.errors import DomainError

log = logging.getLogger(__name__)

STATE_KEY = "booking_flow"

# Button ids are ours; the guest only ever reads the title.
_NS = "bk:"

ROOM_LABEL = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory"}
ROOM_ORDER = ("AC", "NON_AC", "DORM")

NIGHTS_MAX = 10
GUESTS_MAX = 5

UPI_KEY = "payment.upi_id"
QR_KEY = "payment.qr_image_url"
DEFAULT_UPI = "8688397996@postbank"


@dataclass(frozen=True)
class FlowResult:
    reply: str
    prompt: Prompt | None = None
    events: tuple[dict, ...] = ()


# --- state -----------------------------------------------------------------

def _load(db: Database, phone: str) -> dict:
    return dict(inbox.get_conversation_state(db, phone).get(STATE_KEY) or {})


def _save(db: Database, phone: str, state: dict) -> None:
    # Only our own key is written, and it is merged in the database. A plain
    # read-modify-write of the whole document would drop a key another writer
    # set between our read and our write.
    inbox.merge_conversation_state(db, phone, {STATE_KEY: state})


def _clear(db: Database, phone: str) -> None:
    _save(db, phone, {})


def is_active(db: Database, phone: str) -> bool:
    """True while a booking is part-way through for this guest."""
    return bool(_load(db, phone).get("step"))


# --- entry point -----------------------------------------------------------

def handle(db: Database, *, phone: str, text: str | None = None,
           button_id: str | None = None) -> FlowResult | None:
    """Advance a booking. None means the message is not ours to answer."""
    if button_id and button_id.startswith(_NS):
        return _tap(db, phone=phone, button_id=button_id)

    state = _load(db, phone)
    if not state.get("step"):
        return None

    # An escape hatch: a greeting or an explicit cancel abandons the booking and
    # lets the menu answer, so a guest who changes their mind is never trapped in
    # the question sequence.
    if text:
        from core.router import classify
        lowered = (text or "").strip().lower()
        if lowered in ("cancel", "stop", "exit", "quit"):
            _clear(db, phone)
            return FlowResult("No problem, I've cancelled that booking. "
                              "Tap Book a Room whenever you're ready.")
        if classify(text) == "greeting":
            # Let the greeting menu answer, but stop the booking questions.
            _clear(db, phone)
            return None

    if state.get("step") == "review":
        if any(not state.get(k) for k in _REQUIRED):
            return _restart(db, phone)
        if (text or "").strip().lower() in ("yes", "y", "ok", "okay", "submit",
                                            "confirm", "send"):
            return _tap(db, phone=phone, button_id=f"{_NS}submit")
        return None

    if state.get("step") == "name":
        name = (text or "").strip()
        if 2 <= len(name) <= 80:
            state["name"] = name
            state["name_source"] = "GUEST_TYPED"
            state["step"] = "review"
            _save(db, phone, state)
            return _review(db, phone, state)
        return FlowResult("Please send your full name as you would like it on the booking.",
                          prompt=None)

    if state.get("step") == "date":
        parsed = _parse_date(text)
        if parsed:
            state["check_in"] = parsed.date().isoformat()
            state["step"] = "nights"
            _save(db, phone, state)
            return _ask_nights(db)
        return FlowResult("I couldn't read that date. Please send it as "
                          "2026-11-20, or tap Today or Tomorrow.")

    if state.get("step") == "nights":
        n = _clamp(_first_number(text), 1, NIGHTS_MAX)
        if n is None:
            return _ask_nights(db)
        state["nights"] = n
        state["step"] = "guests"
        _save(db, phone, state)
        return _ask_guests(db)

    if state.get("step") == "guests":
        n = _clamp(_first_number(text), 1, GUESTS_MAX)
        if n is None:
            return _ask_guests(db)
        state["guests"] = n
        state["step"] = "room"
        _save(db, phone, state)
        return _ask_room(db)

    if state.get("step") == "room":
        picked = _room_from_text(db, text)
        if picked:
            return _tap(db, phone=phone, button_id=f"{_NS}room:{picked}")
        return _ask_room(db)

    if state.get("step") == "food":
        lowered = (text or "").lower()
        if any(w in lowered for w in ("no food", "without food", "no", "veg only")):
            return _tap(db, phone=phone, button_id=f"{_NS}food:0")
        if any(w in lowered for w in ("food", "with food", "yes", "meals")):
            return _tap(db, phone=phone, button_id=f"{_NS}food:1")
        return _ask_food(db, state["room_type"])

    if state.get("step") == "terms":
        if "full" in (text or "").lower():
            return _tap(db, phone=phone, button_id=f"{_NS}terms:FULL")
        return _tap(db, phone=phone, button_id=f"{_NS}terms:ADVANCE")

    return None


def start(db: Database, *, phone: str) -> FlowResult:
    """Begin a booking: ask for the check-in date."""
    _save(db, phone, {"step": "date"})
    return _ask_date(db)


# --- steps -----------------------------------------------------------------

def _ask_date(db: Database) -> FlowResult:
    rows = (
        Button(f"{_NS}date:today", "Today"),
        Button(f"{_NS}date:tomorrow", "Tomorrow"),
    )
    return FlowResult(
        "Happy to help you book. When would you like to check in?\n"
        "Tap below, or type a date like 2026-11-20.",
        prompt=Prompt(body="When would you like to check in?",
                      rows=rows, button_text="Pick a date"))


def _ask_nights(db: Database) -> FlowResult:
    rows = tuple(Button(f"{_NS}nights:{n}", f"{n} night" + ("s" if n > 1 else ""))
                 for n in range(1, NIGHTS_MAX + 1))
    return FlowResult(
        f"How many nights? Tap a choice, or type the number (1 to {NIGHTS_MAX}).",
        prompt=Prompt(body="How many nights would you like to stay?",
                      rows=rows, button_text="Pick nights"))


def _ask_guests(db: Database) -> FlowResult:
    rows = tuple(Button(f"{_NS}guests:{n}", f"{n} guest" + ("s" if n > 1 else ""))
                 for n in range(1, GUESTS_MAX + 1))
    return FlowResult(
        f"How many guests? Tap a choice, or type the number (1 to {GUESTS_MAX}).",
        prompt=Prompt(body="How many guests are staying?",
                      rows=rows, button_text="Pick guests"))


def _ask_room(db: Database) -> FlowResult:
    rooms = _room_types(db)
    if not rooms:
        return FlowResult("I don't have room rates set up yet, so I can't take a "
                          "booking. I've asked the owner to add them.")
    buttons = tuple(Button(f"{_NS}room:{rt}", ROOM_LABEL.get(rt, rt)) for rt in rooms)
    if len(buttons) <= 3:
        return FlowResult("Which room type would you like?",
                          prompt=Prompt(body="Which room type would you like?",
                                        buttons=buttons))
    return FlowResult("Which room type would you like?",
                      prompt=Prompt(body="Which room type would you like?",
                                    rows=buttons, button_text="Pick a room"))


def _ask_food(db: Database, room_type: str) -> FlowResult | None:
    options = _food_options(db, room_type)
    if len(options) <= 1:
        return None
    return FlowResult(
        "Would you like food included?",
        prompt=Prompt(body="Would you like food included with the room?",
                      buttons=(Button(f"{_NS}food:1", "With food"),
                               Button(f"{_NS}food:0", "No food"))))


def _ask_terms(db: Database) -> FlowResult:
    return FlowResult(
        "How would you like to pay?",
        prompt=Prompt(
            body=("Advance pays the minimum now and the rest at check-in. "
                  "Full pays the whole amount now."),
            buttons=(Button(f"{_NS}terms:ADVANCE", "Advance"),
                     Button(f"{_NS}terms:FULL", "Full amount"))))


def _ask_name(db: Database, phone: str) -> FlowResult:
    return FlowResult("What name should I put on the booking? Please type your full name.")


def _review(db: Database, phone: str, state: dict) -> FlowResult:
    """Show exactly what will be requested, then let the guest submit."""
    start_at, end_at = _window(state)
    room_type = state["room_type"]
    food = state.get("food")
    guests = int(state["guests"])
    terms = state.get("terms") or "ADVANCE"

    try:
        q = pricing.quote(db, room_type=room_type, food_included=food,
                          start=start_at, end=end_at, mode="daily",
                          stay_mode="NIGHT", guests=guests)
    except DomainError as exc:
        return FlowResult(f"I can't price that stay: {exc.message}")

    total = q.charges_total
    advance = billing.advance_due(db, guests) if terms == "ADVANCE" else total

    lines = [
        "Please check your booking:",
        f"- Room: {ROOM_LABEL.get(room_type, room_type)}",
        f"- Food: {'included' if q.food_included else 'not included'}",
        f"- Check-in: {start_at.date().isoformat()}",
        f"- Nights: {state['nights']}",
        f"- Guests: {guests}",
        f"- Total: {_rupees(total)}",
        f"- Pay now ({'advance' if terms == 'ADVANCE' else 'full'}): {_rupees(advance)}",
        "",
        "Tap Submit and I will send this to the owner. The owner confirms the "
        "booking; nothing is held until they do.",
    ]
    return FlowResult("\n".join(lines),
                      prompt=Prompt(body="Shall I send this request to the owner?",
                                    buttons=(Button(f"{_NS}submit", "Submit"),
                                             Button(f"{_NS}restart", "Start over"),
                                             Button(f"{_NS}cancel", "Cancel"))))


# --- taps ------------------------------------------------------------------

def _tap(db: Database, *, phone: str, button_id: str) -> FlowResult:
    state = _load(db, phone)
    verb, _, value = button_id[len(_NS):].partition(":")

    if verb == "cancel":
        _clear(db, phone)
        return FlowResult("No problem, I've cancelled that. Tap Book a Room when "
                          "you're ready.")

    if verb == "restart":
        _save(db, phone, {"step": "date"})
        return _ask_date(db)

    if not state.get("step"):
        # A stale tap after the flow ended: start again from the top.
        return start(db, phone=phone)

    if verb == "date":
        today = datetime.now().astimezone().date()
        day = today + timedelta(days=1 if value == "tomorrow" else 0)
        state["check_in"] = day.isoformat()
        state["step"] = "nights"
        _save(db, phone, state)
        return _ask_nights(db)

    if verb == "nights":
        n = _clamp(value, 1, NIGHTS_MAX)
        if n is None:
            return _ask_nights(db)
        state["nights"] = n
        state["step"] = "guests"
        _save(db, phone, state)
        return _ask_guests(db)

    if verb == "guests":
        n = _clamp(value, 1, GUESTS_MAX)
        if n is None:
            return _ask_guests(db)
        state["guests"] = n
        state["step"] = "room"
        _save(db, phone, state)
        return _ask_room(db)

    if verb == "room":
        if value not in _room_types(db):
            return _ask_room(db)
        state["room_type"] = value
        options = _food_options(db, value)
        if len(options) == 1:
            state["food"] = options[0]
            state["step"] = "terms"
            _save(db, phone, state)
            return _ask_terms(db)
        state["step"] = "food"
        _save(db, phone, state)
        return _ask_food(db, value)

    if verb == "food":
        state["food"] = value == "1"
        state["step"] = "terms"
        _save(db, phone, state)
        return _ask_terms(db)

    if verb == "terms":
        state["terms"] = "FULL" if value == "FULL" else "ADVANCE"
        if _known_name(db, phone):
            state["name"] = _known_name(db, phone)
            state["step"] = "review"
            _save(db, phone, state)
            return _review(db, phone, state)
        state["step"] = "name"
        _save(db, phone, state)
        return _ask_name(db, phone)

    if verb == "name":
        state["step"] = "name"
        _save(db, phone, state)
        return _ask_name(db, phone)

    if verb == "submit":
        if any(not state.get(k) for k in _REQUIRED):
            return _restart(db, phone)
        return _submit(db, phone=phone, state=state)

    return FlowResult("I didn't understand that tap. Tap Book a Room to start again.")


# --- submit ----------------------------------------------------------------

def _submit(db: Database, *, phone: str, state: dict) -> FlowResult:
    start_at, end_at = _window(state)
    room_type = state["room_type"]
    guests = int(state["guests"])
    name = (state.get("name") or _known_name(db, phone) or "").strip()
    if len(name) < 2:
        state["step"] = "name"
        _save(db, phone, state)
        return _ask_name(db, phone)

    beds = availability.available_beds(db, start_at, end_at, room_type=room_type)
    if len(beds) < guests:
        _clear(db, phone)
        return FlowResult(
            f"Sorry, there are only {len(beds)} {ROOM_LABEL.get(room_type, room_type)} "
            f"bed(s) free for those dates, and you asked for {guests}. "
            "Please try fewer guests or other dates.")

    try:
        request = booking.create_request(
            db, phone=phone, full_name=name, gender=None,
            bed_ids=beds[:guests], check_in_at=start_at, check_out_at=end_at,
            food_included=state.get("food"), mode="daily", stay_mode="NIGHT",
            payment_terms=state.get("terms") or "ADVANCE", guests_count=guests,
            source="WHATSAPP", name_source=state.get("name_source") or "GUEST_TYPED")
    except DomainError as exc:
        _clear(db, phone)
        return FlowResult(f"I couldn't create that request: {exc.message}")

    _clear(db, phone)

    advance = request.advance_due
    total = request.charges_total
    terms = request.payment_terms
    upi = hostel_facts.get(db, UPI_KEY) or DEFAULT_UPI
    qr = hostel_facts.get(db, QR_KEY)

    lines = [
        f"Request {request.reference} sent to the owner.",
        f"Total: {_rupees(total)}",
        (f"Pay now (advance): {_rupees(advance)}" if terms == "ADVANCE"
         else f"Pay now (full): {_rupees(total)}"),
        "",
        f"Pay to UPI: {upi}",
    ]
    if qr:
        lines.append(f"Scan the QR: {qr}")
    lines += [
        "Send your payment screenshot here after paying.",
        "",
        "The owner will confirm shortly. Your bed is not held until they do.",
    ]

    event = {
        "type": "booking_requested",
        "reference": request.reference,
        "bed_ids": list(beds[:guests]),
        "guests": guests,
        "check_in": start_at.date().isoformat(),
        "check_out": end_at.date().isoformat(),
        "charges_total": str(total),
        "advance_due": str(advance),
        "payment_terms": terms,
        "guest_name": name,
    }
    return FlowResult("\n".join(lines), events=(event,))


# --- helpers ---------------------------------------------------------------

_REQUIRED = ("check_in", "nights", "guests", "room_type")


def _window(state: dict) -> tuple[datetime, datetime]:
    start_at = datetime.strptime(state["check_in"], "%Y-%m-%d").replace(
        tzinfo=datetime.now().astimezone().tzinfo)
    return start_at, start_at + timedelta(days=int(state["nights"]))


def _room_types(db: Database) -> list[str]:
    rows = db.query_all(
        "SELECT DISTINCT room_type FROM rate_plans "
        "WHERE active AND daily_rate IS NOT NULL")
    present = {row["room_type"] for row in rows}
    return [rt for rt in ROOM_ORDER if rt in present]


def _food_options(db: Database, room_type: str) -> list[bool]:
    try:
        return list(pricing.food_options(db, room_type))
    except Exception:
        return [False]


def _known_name(db: Database, phone: str) -> str | None:
    row = db.query_one(
        "SELECT full_name, wa_profile_name, name_source FROM guests WHERE phone = %s",
        (phone,))
    if not row:
        return None
    if row["full_name"] and row["name_source"] in ("GUEST_TYPED", "OWNER", "IMPORT"):
        return row["full_name"]
    return row["wa_profile_name"] or row["full_name"]


def _parse_date(text: str | None) -> datetime | None:
    value = (text or "").strip().lower()
    today = datetime.now().astimezone()
    if value in ("today", "now"):
        return today.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "tomorrow":
        return (today + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                   microsecond=0)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=today.tzinfo)
        except ValueError:
            continue
    return None


def _first_number(text: str | None) -> str | None:
    import re
    match = re.search(r"\d+", text or "")
    return match.group(0) if match else None


def _room_from_text(db: Database, text: str | None) -> str | None:
    lowered = (text or "").lower()
    rooms = _room_types(db)
    if "dorm" in lowered and "DORM" in rooms:
        return "DORM"
    if ("non" in lowered and "ac" in lowered) or "nonac" in lowered:
        return "NON_AC" if "NON_AC" in rooms else None
    if "ac" in lowered and "AC" in rooms:
        return "AC"
    return None


def _clamp(value: str, low: int, high: int) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None


def _restart(db: Database, phone: str) -> FlowResult:
    """Recover from a half-saved flow instead of crashing on it."""
    _save(db, phone, {"step": "date"})
    return FlowResult("Sorry, I lost track of that booking. Let's start again.\n\n"
                      "When would you like to check in? Tap Today or Tomorrow, "
                      "or type a date like 2026-11-20.")


def _rupees(amount) -> str:
    return f"Rs {amount:,.0f}"


def my_bookings_reply(db: Database, phone: str) -> str:
    """This guest's own bookings. Never anyone else's."""
    rows = db.query_all(
        """
        SELECT b.reference, b.status, b.bed_id, b.check_in_at, b.check_out_at,
               b.food_included, b.charges_total
        FROM bookings b JOIN guests g ON g.guest_id = b.guest_id
        WHERE g.phone = %s
        ORDER BY b.created_at DESC LIMIT 5
        """,
        (phone,))
    if not rows:
        return ("You don't have any bookings with us yet. Tap Book a Room to make "
                "one.")
    out = ["Your bookings:"]
    for r in rows:
        out.append(
            f"- {r['reference']} ({r['status']})\n"
            f"  {r['check_in_at'].date().isoformat()} to "
            f"{r['check_out_at'].date().isoformat()}, bed {r['bed_id']}, "
            f"{'with food' if r['food_included'] else 'no food'}, "
            f"{_rupees(r['charges_total'] or 0)}")
    return "\n".join(out)
