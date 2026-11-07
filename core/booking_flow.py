"""A tap-driven booking conversation.

The guest books by tapping, so the request is created by code rather than by the
AI. That is the whole point: a request is made every time, and the owner is told
every time, whether or not the AI is up.

Every figure shown comes from the pricing engine. Nothing here computes an
amount, so no rate is ever guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import availability, billing, booking, hostel_facts, inbox, pricing
from core.channel.adapter import Button, Prompt
from core.db import Database
from core.errors import BedUnavailable, BookingConflict, DomainError
from core.pricing import money

log = logging.getLogger(__name__)

STATE_KEY = "booking_flow"

# Button ids are ours; the guest only ever reads the title.
_NS = "bk:"

ROOM_LABEL = {"AC": "AC", "NON_AC": "Non-AC", "DORM": "Dormitory"}
ROOM_ORDER = ("AC", "NON_AC", "DORM")

NIGHTS_MAX = 10
GUESTS_MAX = 5
# How far ahead a single tap can push the check-out. A guest can always type a
# later date; this only caps the quick buttons, so a two-month stay is reachable.
CHECKOUT_MAX_NIGHTS = 60

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

    if state.get("step") == _CONFIRM_STEP:
        return _answer_confirm(db, phone=phone, state=state, text=text)

    if state.get("step") == "date":
        parsed = _parse_date(text)
        if parsed:
            return _confirm_typed(db, phone=phone, state=state, field="check_in",
                                  value=parsed.date().isoformat(),
                                  next_step="checkout",
                                  label=parsed.date().strftime("%d %B %Y"))
        return FlowResult("I couldn't read that date. Please send it as "
                          "2026-11-20, or tap Today, Tomorrow or Day after "
                          "tomorrow.")

    if state.get("step") == "checkout":
        parsed = _parse_date(text)
        if parsed:
            check_in = _date_of(state.get("check_in"))
            if check_in and parsed.date() <= check_in:
                return FlowResult(
                    "The check-out must be after the check-in. Please send a "
                    "later date.", prompt=_ask_checkout(db, state).prompt)
            return _confirm_typed(db, phone=phone, state=state, field="check_out",
                                  value=parsed.date().isoformat(),
                                  next_step="stay",
                                  label=parsed.date().strftime("%d %B %Y"))
        return FlowResult("I couldn't read that date. Please send it as "
                          "2026-11-20, or tap a choice below.")

    if state.get("step") == "stay":
        lowered = (text or "").lower()
        if "day" in lowered and "night" not in lowered:
            return _tap(db, phone=phone, button_id=f"{_NS}stay:DAY")
        if "night" in lowered:
            return _tap(db, phone=phone, button_id=f"{_NS}stay:NIGHT")
        return _ask_stay(db)

    if state.get("step") == "times":
        if _is_skip(text):
            return _tap(db, phone=phone, button_id=f"{_NS}times:skip")
        parsed = _parse_time_range(text, state)
        if parsed:
            return _confirm_typed(db, phone=phone, state=state, field="times",
                                  value=parsed, next_step="guests",
                                  label=_times_label(parsed))
        return _ask_times(db, state)

    if state.get("step") == "guests":
        n = _clamp(_first_number(text), 1, GUESTS_MAX)
        if n is None:
            return _ask_guests(db)
        return _confirm_typed(db, phone=phone, state=state, field="guests", value=n,
                              next_step="room", label=_guests_label(n))

    if state.get("step") == "room":
        picked = _room_from_text(db, text)
        if picked:
            return _select_room(db, phone=phone, state=state, value=picked, typed=True)
        return _ask_room(db, state)

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


def start(db: Database, *, phone: str, text: str | None = None) -> FlowResult:
    """Begin a booking, using whatever the guest already told us.

    A guest who opens with "book from 1 December for 2 nights, AC with food" has
    answered three of our questions before we ask them. Reading those answers
    here means we ask only for what is still missing, instead of ignoring the
    message and starting from scratch.
    """
    state: dict = {}
    if text:
        _prefill(db, state, text)
    state["step"] = "date"
    _save(db, phone, state)
    return _first_missing(db, phone=phone, state=state)


def _prefill(db: Database, state: dict, text: str) -> None:
    """Seed the flow from the guest's opening message. Never guesses."""
    lowered = (text or "").lower()
    check_in = _parse_date(text)
    if check_in:
        state["check_in"] = check_in.date().isoformat()

    nights = _nights_from_text(text)
    if nights and check_in:
        state["check_out"] = (check_in + timedelta(days=nights)).date().isoformat()
        # The unit the guest used settles the night/day question for us: "2
        # nights" is a night stay, "3 days" is a day stay.
        if re.search(r"\d+\s*(nights?|din|roju)", lowered) or \
                re.search(r"\ba night\b|\bper night\b|\bnight stay\b", lowered):
            state["stay_mode"] = "NIGHT"
        elif re.search(r"\d+\s*days?", lowered) or re.search(r"\ba day\b", lowered):
            state["stay_mode"] = "DAY"

    room = _room_from_text(db, text)
    if room:
        state["room_type"] = room
        options = _food_options(db, room)
        if len(options) == 1:
            state["food"] = options[0]

    if "non" not in lowered:
        if re.search(r"\bno food\b|\bwithout food\b", lowered):
            state["food"] = False
        elif re.search(r"\bwith food\b|\bfood\b|\bmeals\b", lowered):
            state["food"] = True

    guests = _guests_from_text(text)
    if guests:
        state["guests"] = guests

    if re.search(r"\bday stay\b|\bfull day\b", lowered):
        state["stay_mode"] = "DAY"
    elif re.search(r"\bnight stay\b|\bper night\b", lowered):
        state["stay_mode"] = "NIGHT"


def _first_missing(db: Database, *, phone: str, state: dict) -> FlowResult:
    """Ask for the earliest answer we still need, in the usual order."""
    if not state.get("check_in"):
        return _ask_date(db)
    if not state.get("check_out"):
        state["step"] = "checkout"
        _save(db, phone, state)
        return _ask_checkout(db, state)
    if not state.get("stay_mode"):
        state["step"] = "stay"
        _save(db, phone, state)
        return _ask_stay(db)
    if not state.get("check_in_time"):
        state["step"] = "times"
        _save(db, phone, state)
        return _ask_times(db, state)
    if not state.get("guests"):
        state["step"] = "guests"
        _save(db, phone, state)
        return _ask_guests(db)
    if not state.get("room_type"):
        state["step"] = "room"
        _save(db, phone, state)
        return _ask_room(db, state)
    if state.get("food") is None:
        state["step"] = "food"
        _save(db, phone, state)
        asked = _ask_food(db, state["room_type"])
        if asked is not None:
            return asked
    state["step"] = "terms"
    _save(db, phone, state)
    return _ask_terms(db)


def _nights_from_text(text: str | None) -> int | None:
    """A stay length the guest wrote, in days. None when they did not say."""
    lowered = (text or "").lower()
    match = re.search(r"(\d+)\s*(nights?|days?|din|roju)", lowered)
    if match:
        return _clamp(match.group(1), 1, CHECKOUT_MAX_NIGHTS)
    if re.search(r"\bone week\b|\b1 week\b|\ba week\b", lowered):
        return 7
    if re.search(r"\bmonth\b|\b1 month\b", lowered):
        return 30
    return None


def _guests_from_text(text: str | None) -> int | None:
    match = re.search(r"(\d+)\s*(guests?|people|persons?|members?|log|mandi)",
                      (text or "").lower())
    if match:
        return _clamp(match.group(1), 1, GUESTS_MAX)
    return None


# --- steps -----------------------------------------------------------------

def _ask_date(db: Database) -> FlowResult:
    rows = (
        Button(f"{_NS}date:today", "Today"),
        Button(f"{_NS}date:tomorrow", "Tomorrow"),
        Button(f"{_NS}date:dayafter", "Day after tomorrow"),
    )
    return FlowResult(
        "Happy to help you book. When would you like to check in?\n"
        "Tap below, or type a date like 2026-11-20. Any date is fine, however "
        "far ahead, including a stay of one or two months.",
        prompt=Prompt(body="When would you like to check in?",
                      rows=rows, button_text="Pick a date"))


def _ask_checkout(db: Database, state: dict) -> FlowResult:
    rows = (
        Button(f"{_NS}cout:1", "1 night"),
        Button(f"{_NS}cout:2", "2 nights"),
        Button(f"{_NS}cout:3", "3 nights"),
        Button(f"{_NS}cout:7", "1 week"),
        Button(f"{_NS}cout:30", "1 month"),
    )
    check_in = _date_of(state.get("check_in"))
    body = "When would you like to check out?"
    if check_in:
        body = (f"Checking in on {check_in.strftime('%d %B %Y')}. When would you "
                f"like to check out?")
    return FlowResult(
        body + "\nTap a choice, or type a date like 2026-11-20. A stay can be "
        "as long as you like, even one or two months.",
        prompt=Prompt(body=body, rows=rows, button_text="Pick check-out"))


def _ask_stay(db: Database) -> FlowResult:
    night = hostel_facts.get(db, hostel_facts.KEY_NIGHT_MEALS) or \
        "A night includes dinner only."
    day = hostel_facts.get(db, hostel_facts.KEY_DAY_MEALS) or \
        "A day is 24 hours and includes tiffin, lunch and dinner."
    body = f"{night}\n{day}\n\nWhich would you like?"
    return FlowResult(
        body,
        prompt=Prompt(body=body,
                      buttons=(Button(f"{_NS}stay:NIGHT", "Night (dinner)"),
                               Button(f"{_NS}stay:DAY", "Day (all meals)"))))


def _ask_times(db: Database, state: dict) -> FlowResult:
    day = state.get("stay_mode") == "DAY"
    if day:
        body = ("We are open 24/7. What time would you like to check in? A day "
                "runs 24 hours from that time.")
    else:
        body = ("We are open 24/7. What time would you like to check in? A "
                "night covers dinner and the night.")
    return FlowResult(
        body + "\nReply like '9:00 pm' or '6 am', or tap Skip to use the default "
        "(9:00 pm in, 9:00 am out).",
        prompt=Prompt(body=body,
                      buttons=(Button(f"{_NS}times:skip", "Skip"),)))


def _is_skip(text: str | None) -> bool:
    lowered = (text or "").strip().lower()
    return lowered in ("skip", "default", "any", "any time", "anytime", "no",
                       "not sure", "dont know", "don't know")


def _times_label(value: dict) -> str:
    return (f"check-in at {value['check_in_time']}, "
            f"check-out at {value['check_out_time']}")


def _ask_guests(db: Database) -> FlowResult:
    rows = tuple(Button(f"{_NS}guests:{n}", f"{n} guest" + ("s" if n > 1 else ""))
                 for n in range(1, GUESTS_MAX + 1))
    return FlowResult(
        f"How many guests? Tap a choice, or type the number (1 to {GUESTS_MAX}).",
        prompt=Prompt(body="How many guests are staying?",
                      rows=rows, button_text="Pick guests"))


def _ask_room(db: Database, state: dict | None = None) -> FlowResult:
    rooms = _room_types(db)
    if not rooms:
        return FlowResult("I don't have room rates set up yet, so I can't take a "
                          "booking. I've asked the owner to add them.")
    # A room type with no free bed for the guest's dates is shown, but marked
    # full, so the guest learns the hostel is busy rather than wondering why a
    # room they were told about has vanished. The tap is refused, not ignored.
    full = _full_room_types(db, state)
    buttons = tuple(
        Button(f"{_NS}room:{rt}",
               (f"{ROOM_LABEL.get(rt, rt)} (full)" if rt in full
                else ROOM_LABEL.get(rt, rt)))
        for rt in rooms)
    body = "Which room type would you like?"
    if full:
        body += (" Rooms marked (full) have no bed free for your dates. Pick "
                 "another, or send different dates.")
    if len(buttons) <= 3:
        return FlowResult(body, prompt=Prompt(body=body, buttons=buttons))
    return FlowResult(body, prompt=Prompt(body=body, rows=buttons,
                                          button_text="Pick a room"))


def _full_room_types(db: Database, state: dict | None) -> set[str]:
    """Room types with no free bed for the guest's dates. Empty if unknown.

    Without dates the question cannot be answered, and guessing would mark a
    room full that is not; so nothing is marked until the dates are known.
    """
    if not state or not state.get("check_in") or not state.get("check_out"):
        return set()
    try:
        start_at, end_at = _window(state)
    except (KeyError, ValueError):
        return set()
    summary = availability.availability_summary(db, start_at, end_at)
    return {room_type
            for room_type, counts in summary["by_room_type"].items()
            if counts["available_beds"] == 0}


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
            body=("Reservation pays a minimum now to hold the bed, and the rest "
                  "at check-in. Full pays the whole amount now."),
            buttons=(Button(f"{_NS}terms:ADVANCE", "Reservation"),
                     Button(f"{_NS}terms:FULL", "Full amount"))))


# --- confirming a value the guest typed ------------------------------------
#
# A value typed by hand is easy to fat-finger, and "5 nights" read as "3" sends
# the guest a wrong bill. So a typed date, guest count or room is read back and
# confirmed before it is used. A tapped button is already an explicit choice and
# is never re-asked.

_CONFIRM_STEP = "confirm"


def _guests_label(n: int) -> str:
    return f"{n} guest" + ("s" if n > 1 else "")


def _confirm_typed(db: Database, *, phone: str, state: dict, field: str,
                   value, next_step: str, label: str) -> FlowResult:
    """Read a typed answer back before moving on, with Yes / Change buttons."""
    state["pending"] = {"field": field, "value": value, "next_step": next_step}
    state["step"] = _CONFIRM_STEP
    _save(db, phone, state)
    return FlowResult(
        f"I have {label}. Is that correct?",
        prompt=Prompt(body=f"Just to confirm: {label}?",
                      buttons=(Button(f"{_NS}cf:yes", "Yes, correct"),
                               Button(f"{_NS}cf:no", "Change"))))


def _answer_confirm(db: Database, *, phone: str, state: dict,
                    text: str | None) -> FlowResult:
    """Act on the guest's yes/no to a read-back value."""
    pending = state.get("pending") or {}
    field, value, next_step = (pending.get("field"), pending.get("value"),
                               pending.get("next_step"))
    lowered = (text or "").strip().lower()

    if lowered in ("no", "n", "change", "edit", "wrong", "cancel") or \
            any(w in lowered for w in ("no,", "not correct", "change", "wrong")):
        return _redo_field(db, phone=phone, state=state, field=field)

    if lowered in ("yes", "y", "ok", "okay", "correct", "confirm", "yep", "yeah"):
        return _commit_typed(db, phone=phone, state=state, field=field,
                             value=value, next_step=next_step)

    # Anything else is treated as the corrected value itself, so a guest who
    # simply replies "3" instead of tapping Change is still understood.
    return _reinterpret(db, phone=phone, state=state, field=field, text=text)


def _reask_confirm(db: Database, *, state: dict) -> FlowResult:
    """Show the read-back again, with the value we already hold intact.

    A reply we cannot read as a correction -- a stray word, an answer meant for
    a later question -- must not throw the answer away and send the guest back
    to the first question. The guest is simply asked to confirm again.
    """
    pending = state.get("pending") or {}
    value = pending.get("value")
    field = pending.get("field")
    if field == "times" and isinstance(value, dict):
        label = _times_label(value)
    elif field == "guests":
        label = _guests_label(value)
    elif field in ("check_in", "check_out"):
        parsed = _date_of(value)
        label = parsed.strftime("%d %B %Y") if parsed else str(value)
    elif field == "room_type":
        label = ROOM_LABEL.get(value, value)
    else:
        label = str(value)
    return FlowResult(
        f"I have {label}. Is that correct? Please tap Yes, correct or Change.",
        prompt=Prompt(body=f"Just to confirm: {label}?",
                      buttons=(Button(f"{_NS}cf:yes", "Yes, correct"),
                               Button(f"{_NS}cf:no", "Change"))))


def _commit_typed(db: Database, *, phone: str, state: dict, field: str,
                  value, next_step: str) -> FlowResult:
    state.pop("pending", None)
    if field is None:
        return _restart(db, phone)
    if field == "times":
        state["check_in_time"] = value["check_in_time"]
        state["check_out_time"] = value["check_out_time"]
    else:
        state[field] = value
    state["step"] = next_step
    _save(db, phone, state)
    return _after_step(db, phone=phone, state=state, next_step=next_step)


def _after_step(db: Database, *, phone: str, state: dict, next_step: str) -> FlowResult:
    if next_step == "checkout":
        return _ask_checkout(db, state)
    if next_step == "stay":
        return _ask_stay(db)
    if next_step == "times":
        return _ask_times(db, state)
    if next_step == "guests":
        return _ask_guests(db)
    if next_step == "room":
        return _ask_room(db, state)
    if next_step == "food":
        return _room_chosen(db, phone=phone, state=state)
    return _restart(db, phone)


def _redo_field(db: Database, *, phone: str, state: dict, field: str | None) -> FlowResult:
    """Ask the same question again after the guest said the value was wrong."""
    state.pop("pending", None)
    if field == "check_in":
        state.pop("check_in", None)
        state["step"] = "date"
        _save(db, phone, state)
        return _ask_date(db)
    if field == "check_out":
        state["step"] = "checkout"
        _save(db, phone, state)
        return _ask_checkout(db, state)
    if field == "times":
        state["step"] = "times"
        _save(db, phone, state)
        return _ask_times(db, state)
    if field == "guests":
        state["step"] = "guests"
        _save(db, phone, state)
        return _ask_guests(db)
    if field == "room_type":
        state.pop("room_type", None)
        state["step"] = "room"
        _save(db, phone, state)
        return _ask_room(db, state)
    return _restart(db, phone)


def _reinterpret(db: Database, *, phone: str, state: dict, field: str | None,
                 text: str | None) -> FlowResult:
    """Read the guest's reply as a corrected value for the pending field."""
    if field == "check_in":
        parsed = _parse_date(text)
        if parsed:
            return _confirm_typed(db, phone=phone, state=state, field="check_in",
                                  value=parsed.date().isoformat(), next_step="checkout",
                                  label=parsed.date().strftime("%d %B %Y"))
    elif field == "check_out":
        parsed = _parse_date(text)
        if parsed:
            return _confirm_typed(db, phone=phone, state=state, field="check_out",
                                  value=parsed.date().isoformat(), next_step="stay",
                                  label=parsed.date().strftime("%d %B %Y"))
    elif field == "times":
        parsed = _parse_time_range(text, state)
        if parsed:
            return _confirm_typed(db, phone=phone, state=state, field="times",
                                  value=parsed, next_step="guests",
                                  label=_times_label(parsed))
    elif field == "guests":
        n = _clamp(_first_number(text), 1, GUESTS_MAX)
        if n is not None:
            return _confirm_typed(db, phone=phone, state=state, field="guests",
                                  value=n, next_step="room", label=_guests_label(n))
    elif field == "room_type":
        picked = _room_from_text(db, text)
        if picked:
            return _confirm_typed(db, phone=phone, state=state, field="room_type",
                                  value=picked, next_step="food",
                                  label=ROOM_LABEL.get(picked, picked))
    # A reply we cannot read as a new value is not a reason to throw the booking
    # away. Ask for the same confirmation again, keeping the answer we hold.
    return _reask_confirm(db, state=state)


def _select_room(db: Database, *, phone: str, state: dict, value: str,
                 typed: bool) -> FlowResult:
    """Record a room choice. A typed one is read back first; a tap is not."""
    if typed:
        return _confirm_typed(db, phone=phone, state=state, field="room_type",
                              value=value, next_step="food",
                              label=ROOM_LABEL.get(value, value))
    state["room_type"] = value
    return _room_chosen(db, phone=phone, state=state)


def _room_chosen(db: Database, *, phone: str, state: dict) -> FlowResult:
    """The room type is settled: ask about food, or move on to payment terms."""
    room_type = state.get("room_type")
    if not room_type:
        return _restart(db, phone)
    options = _food_options(db, room_type)
    if len(options) == 1:
        state["food"] = options[0]
        state["step"] = "terms"
        _save(db, phone, state)
        return _ask_terms(db)
    state["step"] = "food"
    _save(db, phone, state)
    return _ask_food(db, room_type)


def _ask_name(db: Database, phone: str) -> FlowResult:
    return FlowResult("What name should I put on the booking? Please type your full name.")


def _review(db: Database, phone: str, state: dict) -> FlowResult:
    """Show exactly what will be requested, then let the guest submit."""
    if any(not state.get(k) for k in _REQUIRED):
        return _restart(db, phone)
    start_at, end_at = _window(state)
    room_type = state["room_type"]
    food = state.get("food")
    guests = int(state["guests"])
    terms = state.get("terms") or "ADVANCE"

    try:
        q = pricing.quote(db, room_type=room_type, food_included=food,
                          start=start_at, end=end_at, mode="daily",
                          stay_mode=state.get("stay_mode") or "NIGHT", guests=guests)
    except DomainError as exc:
        return FlowResult(f"I can't price that stay: {exc.message}")

    total = q.charges_total
    advance = billing.advance_due(db, guests) if terms == "ADVANCE" else total
    balance = money(total - advance)
    nights = (end_at.date() - start_at.date()).days or 1
    unit = "night" if q.breakdown.get("stay_mode") == "NIGHT" else "day"

    lines = [
        "Please check your booking:",
        f"- Room: {ROOM_LABEL.get(room_type, room_type)}",
        f"- Food: {'included' if q.food_included else 'not included'}",
        f"- Check-in: {start_at.date().isoformat()} at "
        f"{_clock_label(start_at.hour, start_at.minute)}",
        f"- Check-out: {end_at.date().isoformat()} at "
        f"{_clock_label(end_at.hour, end_at.minute)}",
        f"- Stay: {nights} {unit}{'s' if nights > 1 else ''}",
        f"- Guests: {guests}",
        f"- Rate: {_rupees(q.unit_rate)} per bed per {unit}",
    ]
    if q.discount and q.discount > 0:
        promo = q.breakdown.get("promotion") or "Offer"
        percent = q.breakdown.get("promotion_percent")
        label = f"{promo} ({percent}% off)" if percent else promo
        lines += [
            f"- Subtotal: {_rupees(q.base_amount + q.extras)}",
            f"- {label}: - {_rupees(q.discount)}",
        ]
    lines += [
        f"- Total: {_rupees(total)}",
    ]
    if terms == "ADVANCE":
        lines += [
            f"- Reservation (pay now): {_rupees(advance)}",
            f"- Balance at check-in: {_rupees(balance)}",
            "",
            f"The reservation of {_rupees(advance)} is NOT refundable. It is "
            f"part of your total, not an extra charge.",
        ]
    else:
        lines.append(f"- Pay now (full): {_rupees(total)}")
    lines += [
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

    if verb == "cf":
        if not state.get("step") == _CONFIRM_STEP:
            return _restart(db, phone)
        return _answer_confirm(db, phone=phone, state=state,
                               text="yes" if value == "yes" else "no")

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

    # A tap is only meaningful once the answers it depends on exist. A stale or
    # out-of-order tap (a button from an older message, a double tap) would
    # otherwise walk the flow forward past a question that was never answered,
    # leaving a required field unset and crashing at the review step.
    needed = _PREREQ.get(verb)
    if needed and any(not state.get(k) for k in needed):
        return _restart(db, phone)

    if verb == "date":
        today = datetime.now().astimezone().date()
        offset = {"today": 0, "tomorrow": 1, "dayafter": 2}.get(value)
        if offset is None:
            return _ask_date(db)
        day = today + timedelta(days=offset)
        state["check_in"] = day.isoformat()
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

    if verb == "cout":
        check_in = _date_of(state.get("check_in"))
        if not check_in:
            return _restart(db, phone)
        n = _clamp(value, 1, CHECKOUT_MAX_NIGHTS)
        if n is None:
            return _ask_checkout(db, state)
        day = check_in + timedelta(days=n)
        state["check_out"] = day.isoformat()
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

    if verb == "stay":
        state["stay_mode"] = "DAY" if value == "DAY" else "NIGHT"
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

    if verb == "times":
        check_in = _date_of(state.get("check_in"))
        check_out = _date_of(state.get("check_out"))
        if not check_in or not check_out:
            return _restart(db, phone)
        if value == "skip":
            times = _default_times(check_in, check_out)
        else:
            times = _parse_time_range(value, state) or _default_times(check_in, check_out)
        state["check_in_time"] = times["check_in_time"]
        state["check_out_time"] = times["check_out_time"]
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

    if verb == "guests":
        n = _clamp(value, 1, GUESTS_MAX)
        if n is None:
            return _ask_guests(db)
        state["guests"] = n
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

    if verb == "room":
        if value not in _room_types(db):
            return _ask_room(db, state)
        if value in _full_room_types(db, state):
            # The button was tappable, so the guest must be told why it did not
            # work rather than dropped back on the same question.
            return FlowResult(
                f"Sorry, {ROOM_LABEL.get(value, value)} is full for your dates. "
                f"Pick another room type, or send different dates and I will "
                f"check again.",
                prompt=_ask_room(db, state).prompt)
        return _select_room(db, phone=phone, state=state, value=value, typed=False)

    if verb == "food":
        state["food"] = value == "1"
        state.pop("step", None)
        _save(db, phone, state)
        return _first_missing(db, phone=phone, state=state)

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

    return _reask(db, phone=phone, state=state)


def _reask(db: Database, *, phone: str, state: dict) -> FlowResult:
    """A tap we did not recognise: re-ask the question the guest is on.

    A stray tap -- a stale button, a double tap, an id from an old message --
    must not throw the booking away. The guest is asked the current question
    again, so the flow carries on from where it was.
    """
    step = state.get("step")
    if step == "checkout":
        return _ask_checkout(db, state)
    if step == "stay":
        return _ask_stay(db)
    if step == "times":
        return _ask_times(db, state)
    if step == "guests":
        return _ask_guests(db)
    if step == "room":
        return _ask_room(db, state)
    if step == "food":
        return _ask_food(db, state["room_type"]) or _ask_terms(db)
    if step == "terms":
        return _ask_terms(db)
    if step == "name":
        return _ask_name(db, phone)
    if step == _CONFIRM_STEP:
        return _answer_confirm(db, phone=phone, state=state, text="")
    return _ask_date(db)


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
            food_included=state.get("food"), mode="daily",
            stay_mode=state.get("stay_mode") or "NIGHT",
            payment_terms=state.get("terms") or "ADVANCE", guests_count=guests,
            source="WHATSAPP", name_source=state.get("name_source") or "GUEST_TYPED")
    except (BookingConflict, BedUnavailable):
        # Someone else won the race for the same bed by a hair. The guest did
        # nothing wrong and their details are still in hand, so try once more on
        # whatever is now free rather than making them start over.
        beds = availability.available_beds(db, start_at, end_at, room_type=room_type)
        if len(beds) < guests:
            _clear(db, phone)
            return FlowResult(
                f"Sorry, the last {ROOM_LABEL.get(room_type, room_type)} bed was "
                "just taken. Please try different dates.")
        try:
            request = booking.create_request(
                db, phone=phone, full_name=name, gender=None,
                bed_ids=beds[:guests], check_in_at=start_at, check_out_at=end_at,
                food_included=state.get("food"), mode="daily", stay_mode="NIGHT",
                payment_terms=state.get("terms") or "ADVANCE", guests_count=guests,
                source="WHATSAPP",
                name_source=state.get("name_source") or "GUEST_TYPED")
        except DomainError as exc:
            _clear(db, phone)
            return FlowResult(f"I couldn't create that request: {exc.message}")
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
        f"Room: {ROOM_LABEL.get(state.get('room_type'), state.get('room_type'))}",
        f"Check-in: {start_at.date().isoformat()} at "
        f"{_clock_label(start_at.hour, start_at.minute)}",
        f"Check-out: {end_at.date().isoformat()} at "
        f"{_clock_label(end_at.hour, end_at.minute)}",
        f"Guests: {guests}",
        f"Total: {_rupees(total)}",
        (f"Reservation (pay now): {_rupees(advance)}" if terms == "ADVANCE"
         else f"Pay now (full): {_rupees(total)}"),
    ]
    if terms == "ADVANCE":
        lines.append(f"Balance at check-in: {_rupees(money(total - advance))}")
    lines += [
        "",
        f"Pay to UPI: {upi}",
    ]
    if qr:
        lines.append(f"Scan the QR: {qr}")
    lines += [
        "Send your payment screenshot here after paying.",
    ]
    if terms == "ADVANCE":
        lines += [
            "",
            f"The reservation of {_rupees(advance)} is NOT refundable.",
        ]
    lines += [
        "",
        "Please WAIT for the final confirmation. The owner will first confirm "
        "your reservation and the payment, and only then is your booking "
        "confirmed. Your bed is not held until they do.",
        "Until then, please do not send another booking request. You can still "
        "ask me about prices, directions or facilities while you wait.",
    ]

    event = {
        "type": "booking_requested",
        "reference": request.reference,
        "bed_ids": list(beds[:guests]),
        "guests": guests,
        "check_in": start_at.date().isoformat(),
        "check_out": end_at.date().isoformat(),
        "check_in_time": _clock_label(start_at.hour, start_at.minute),
        "check_out_time": _clock_label(end_at.hour, end_at.minute),
        "stay_mode": state.get("stay_mode") or "NIGHT",
        "charges_total": str(total),
        "advance_due": str(advance),
        "payment_terms": terms,
        "guest_name": name,
    }
    return FlowResult("\n".join(lines), events=(event,))


# --- helpers ---------------------------------------------------------------

_REQUIRED = ("check_in", "check_out", "guests", "room_type")

# What each tap step depends on. A step whose answers are missing means the
# guest arrived there out of order, so the flow restarts rather than guessing.
_PREREQ: dict[str, tuple[str, ...]] = {
    "cout": ("check_in",),
    "stay": ("check_in", "check_out"),
    "times": ("check_in", "check_out"),
    "guests": ("check_in", "check_out"),
    "room": ("check_in", "check_out", "guests"),
    "food": ("check_in", "check_out", "guests", "room_type"),
    "terms": ("check_in", "check_out", "guests", "room_type"),
    "name": ("check_in", "check_out", "guests", "room_type"),
    "submit": _REQUIRED,
}


def _date_of(value: str | None):
    """Parse a stored ISO date into a date, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Read a clock time with AM/PM, or a 24-hour one. Returns (hour, minute).

    A bare number is read as a time only when it is the whole message, so an
    answer meant for another question -- "1 guest", "2 nights" -- is never
    mistaken for 01:00 or 02:00.
    """
    value = (text or "").strip().lower().replace(".", "")
    if not value:
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    has_colon = match.group(2) is not None
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    elif not has_colon and not value.isdigit():
        # A bare number inside a longer sentence is not a time.
        return None
    return hour, minute


def _clock_label(hour: int, minute: int) -> str:
    """A 12-hour label, so a guest never has to read 21:00."""
    meridiem = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {meridiem}"


def _default_times(check_in, check_out) -> dict:
    """The times used when the guest skips: 9:00 pm in, 9:00 am out."""
    return {"check_in_time": "21:00", "check_out_time": "09:00"}


def _parse_time_range(text: str | None, state: dict) -> dict | None:
    """Read a check-in time, and a check-out time when the guest gives one.

    "9 pm" sets the check-in time and leaves the check-out at its default; the
    guest can give both, as "9 pm to 6 am". The times are stored as 24-hour
    strings so the billing engine reads them without guessing an AM/PM.
    """
    value = (text or "").strip()
    if not value:
        return None
    parts = re.split(r"\bto\b|[-–—]|,|\band\b", value)
    times = [t for t in (_parse_clock(p) for p in parts) if t]
    if not times:
        single = _parse_clock(value)
        times = [single] if single else []
    if not times:
        return None
    start = times[0]
    end = times[1] if len(times) > 1 else _parse_clock_hhmm(
        state.get("check_out_time") or "09:00")
    return {"check_in_time": _to_24(start),
            "check_out_time": _to_24(end),
            "check_in_label": _clock_label(*start),
            "check_out_label": _clock_label(*end)}


def _to_24(clock: tuple[int, int]) -> str:
    return f"{clock[0]:02d}:{clock[1]:02d}"


def _window(state: dict) -> tuple[datetime, datetime]:
    """The real check-in and check-out instants, times included."""
    tz = datetime.now().astimezone().tzinfo
    start_day = datetime.strptime(state["check_in"], "%Y-%m-%d")
    end_day = datetime.strptime(state["check_out"], "%Y-%m-%d")
    start_h, start_m = _parse_clock_hhmm(state.get("check_in_time") or "21:00")
    end_h, end_m = _parse_clock_hhmm(state.get("check_out_time") or "09:00")
    start_at = start_day.replace(hour=start_h, minute=start_m, tzinfo=tz)
    end_at = end_day.replace(hour=end_h, minute=end_m, tzinfo=tz)
    if end_at <= start_at:
        raise ValueError("the checkout time must be after the check-in time")
    return start_at, end_at


def _parse_clock_hhmm(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


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
    """Read a date the way a guest writes one.

    Handles "today" / "tomorrow" / "day after tomorrow" anywhere in the message,
    an ISO or numeric date (2026-11-20, 20-11-2026, 20/11/2026) and a date with a
    month name, either order ("1 December", "December 1", "1st Dec"). A
    month-and-day with no year is read as the next time that date comes round, so
    "1 December" in November means this year and in December means next year.
    """
    value = (text or "").strip().lower()
    today = datetime.now().astimezone()
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\bday after tomorrow\b|\bdayafter\b", value):
        return midnight + timedelta(days=2)
    if re.search(r"\btomorrow\b", value):
        return midnight + timedelta(days=1)
    if re.search(r"\btoday\b|\bnow\b|\btonight\b", value):
        return midnight
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=today.tzinfo)
        except ValueError:
            continue
    found = _parse_numeric_date(value, today) or _parse_named_date(value, today)
    if found:
        return found.replace(tzinfo=today.tzinfo)
    return None


# A numeric date sitting inside a longer sentence, as "book 3 days from
# 2026-12-01" or "check in on 20/11/2026".
_NUMERIC_DATE = re.compile(
    r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"
    r"|\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")


def _parse_numeric_date(value: str, today: datetime) -> datetime | None:
    match = _NUMERIC_DATE.search(value)
    if not match:
        return None
    if match.group(1):
        year, month, day = (int(match.group(1)), int(match.group(2)),
                            int(match.group(3)))
    else:
        day, month, year = (int(match.group(4)), int(match.group(5)),
                            int(match.group(6)))
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
# Only real month names count as a month, so "from 1" is not read as month
# "from". The alternation is built from _MONTHS, longest first so "sept" is
# preferred over "sep".
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NAMED_DATE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+({_MONTH_ALT})\b"     # 1 December
    rf"|\b({_MONTH_ALT})\s+(\d{{1,2}})\s*(?:st|nd|rd|th)?\b",   # December 1
    re.I)


def _parse_named_date(value: str, today: datetime) -> datetime | None:
    match = _NAMED_DATE.search(value)
    if not match:
        return None
    if match.group(1):
        day_text, month_text = match.group(1), match.group(2)
    else:
        day_text, month_text = match.group(4), match.group(3)
    month = _MONTHS.get(month_text[:9].lower())
    if not month:
        return None
    day = int(day_text)
    try:
        candidate = datetime(today.year, month, day)
    except ValueError:
        return None
    if candidate.date() < today.date():
        try:
            candidate = candidate.replace(year=today.year + 1)
        except ValueError:
            return None
    return candidate


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
