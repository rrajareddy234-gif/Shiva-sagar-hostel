"""A tap-driven conversation for the walk-in services.

A fresh-up and a three-hour rest are sold at the counter, not booked as a night.
The guest taps, the request is created by code, and the owner is told -- the same
promise the booking flow keeps, kept the same way. A rest picks a bed that is
genuinely free for its three hours, so a rest can never collide with a booking.

Every amount comes from the rate card. Nothing here computes one, and neither
service is ever discounted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core import hostel_facts, inbox, pricing, services
from core.channel.adapter import Button, Prompt
from core.db import Database
from core.errors import DomainError
from core.pricing import money

STATE_KEY = "service_flow"
_NS = "sv:"

UPI_KEY = "payment.upi_id"
DEFAULT_UPI = "8688397996@postbank"

# A rest is three hours; the quick window shown when a bed is picked.
REST_MINUTES = 180
REST_HOURS_MAX = 12


@dataclass(frozen=True)
class FlowResult:
    reply: str
    prompt: Prompt | None = None
    events: tuple[dict, ...] = ()


def _load(db: Database, phone: str) -> dict:
    return inbox.get_conversation_state(db, phone).get(STATE_KEY) or {}


def _save(db: Database, phone: str, state: dict) -> None:
    inbox.merge_conversation_state(db, phone, {STATE_KEY: state})


def _clear(db: Database, phone: str) -> None:
    _save(db, phone, {})


def is_active(db: Database, phone: str) -> bool:
    return bool(_load(db, phone).get("step"))


def handle(db: Database, *, phone: str, text: str | None = None,
           button_id: str | None = None) -> FlowResult | None:
    """Advance a service sale. None means the message is not ours to answer."""
    if button_id and button_id.startswith(_NS):
        return _tap(db, phone=phone, button_id=button_id)

    state = _load(db, phone)
    if not state.get("step"):
        return None

    if text:
        lowered = (text or "").strip().lower()
        if lowered in ("cancel", "stop", "exit", "quit"):
            _clear(db, phone)
            return FlowResult("No problem, I've cancelled that. Tap the service "
                              "again whenever you're ready.")

    step = state.get("step")
    if step == "name":
        name = (text or "").strip()
        if 2 <= len(name) <= 80:
            state["name"] = name
            state["name_source"] = "GUEST_TYPED"
            state["step"] = "review"
            _save(db, phone, state)
            return _review(db, phone, state)
        return FlowResult("Please send your full name as you would like it on the "
                          "booking.")
    if step == "bed":
        picked = _bed_from_text(db, text, state)
        if picked:
            return _choose_bed(db, phone=phone, state=state, bed_id=picked)
        return _ask_bed(db, state)
    if step == "hours":
        minutes = _hours_to_minutes(text)
        if minutes:
            state["minutes"] = minutes
            state["step"] = "review"
            _save(db, phone, state)
            return _review(db, phone, state)
        return _ask_hours(db, state)
    if step == "review":
        if (text or "").strip().lower() in ("yes", "y", "ok", "okay", "submit",
                                            "confirm", "send"):
            return _tap(db, phone=phone, button_id=f"{_NS}submit")
        return _review(db, phone, state)
    return None


# --- entry points ----------------------------------------------------------

def start(db: Database, *, phone: str, service_code: str) -> FlowResult:
    """Begin a service sale: ask whatever that service still needs."""
    try:
        service = pricing.get_service(db, service_code)
    except DomainError as exc:
        return FlowResult(f"I can't offer that service right now: {exc.message}")

    state = {"step": "start", "service_code": service_code,
             "minutes": service.duration_minutes or REST_MINUTES}
    _save(db, phone, state)

    if service.needs_bed:
        state["step"] = "hours"
        _save(db, phone, state)
        return _ask_hours(db, state)
    return _continue(db, phone=phone, state=state)


def _continue(db: Database, *, phone: str, state: dict) -> FlowResult:
    """The next thing the guest must give us, or submit if nothing is missing."""
    name = (state.get("name") or _known_name(db, phone) or "").strip()
    if len(name) < 2:
        state["step"] = "name"
        _save(db, phone, state)
        return FlowResult("Please send your full name as you would like it on the "
                          "booking.")
    return _submit(db, phone=phone, state=state)


# --- steps -----------------------------------------------------------------

def _ask_hours(db: Database, state: dict) -> FlowResult:
    rows = tuple(
        Button(f"{_NS}hours:{h}", f"{h} hour" + ("s" if h > 1 else ""))
        for h in (1, 2, 3))
    return FlowResult(
        "How long would you like the bed for? A three-hour rest is the usual. "
        "Tap below, or type the number of hours.",
        prompt=Prompt(body="How many hours do you need the bed?",
                      rows=rows, button_text="Pick hours"))


def _ask_bed(db: Database, state: dict) -> FlowResult:
    beds = _free_beds(db, state)
    if not beds:
        _clear(db, phone="")
        return FlowResult("Sorry, no bed is free for that window right now. "
                          "Please try a shorter time or come to the counter.")
    buttons = tuple(Button(f"{_NS}bed:{bed_id}", bed_id) for bed_id in beds[:3])
    return FlowResult(
        "Which bed would you like? Tap one below.",
        prompt=Prompt(body="Pick a bed for your rest", buttons=buttons))


def _review(db: Database, phone: str, state: dict) -> FlowResult:
    try:
        service = pricing.get_service(db, state["service_code"])
    except DomainError as exc:
        return FlowResult(f"I can't price that: {exc.message}")
    amount = money(service.price)
    start_at, end_at = _window(state)
    lines = [
        "Please check:",
        f"- Service: {service.name}",
        f"- Price: {_rupees(amount)}",
    ]
    if service.needs_bed:
        lines += [
            f"- Bed: {state.get('bed_id')}",
            f"- From: {start_at.strftime('%d %b, %I:%M %p')}",
            f"- Until: {end_at.strftime('%d %b, %I:%M %p')}",
        ]
    lines += [
        "",
        "Tap Submit and I will send this to the owner. The owner confirms it; "
        "nothing is booked until they do.",
    ]
    return FlowResult("\n".join(lines),
                      prompt=Prompt(body="Shall I send this to the owner?",
                                    buttons=(Button(f"{_NS}submit", "Submit"),
                                             Button(f"{_NS}cancel", "Cancel"))))


# --- taps ------------------------------------------------------------------

def _tap(db: Database, *, phone: str, button_id: str) -> FlowResult | None:
    state = _load(db, phone)
    rest = button_id[len(_NS):]
    verb, _, value = rest.partition(":")

    if verb == "cancel":
        _clear(db, phone)
        return FlowResult("Cancelled. Tap the service again whenever you're ready.")

    if verb == "hours":
        minutes = _hours_to_minutes(value)
        if minutes:
            state["minutes"] = minutes
            state["step"] = "bed" if _needs_bed(db, state) else "review"
            _save(db, phone, state)
            if state["step"] == "bed":
                return _ask_bed(db, state)
            return _continue(db, phone=phone, state=state)
        return _ask_hours(db, state)

    if verb == "bed":
        return _choose_bed(db, phone=phone, state=state, bed_id=value)

    if verb == "submit":
        return _submit(db, phone=phone, state=state)

    return None


def _choose_bed(db: Database, *, phone: str, state: dict, bed_id: str) -> FlowResult:
    free = _free_beds(db, state)
    if bed_id not in free:
        return FlowResult(
            f"Sorry, bed {bed_id} is not free for that window. Please pick another.",
            prompt=_ask_bed(db, state).prompt)
    state["bed_id"] = bed_id
    state["step"] = "review"
    _save(db, phone, state)
    return _continue(db, phone=phone, state=state)


# --- submit ----------------------------------------------------------------

def _submit(db: Database, *, phone: str, state: dict) -> FlowResult:
    service_code = state.get("service_code")
    if not service_code:
        _clear(db, phone)
        return FlowResult("Something went wrong with that request. Please tap the "
                          "service again.")
    name = (state.get("name") or _known_name(db, phone) or "").strip()
    if len(name) < 2:
        state["step"] = "name"
        _save(db, phone, state)
        return FlowResult("Please send your full name as you would like it on the "
                          "booking.")

    start_at, end_at = _window(state)
    try:
        order = services.create_order(
            db, phone=phone, service_code=service_code, full_name=name,
            bed_id=state.get("bed_id"), starts_at=start_at,
            duration_minutes=state.get("minutes"),
            source="WHATSAPP")
    except DomainError as exc:
        _clear(db, phone)
        return FlowResult(f"I couldn't create that request: {exc.message}")

    _clear(db, phone)
    service = pricing.get_service(db, service_code)
    upi = hostel_facts.get(db, UPI_KEY) or DEFAULT_UPI
    qr = hostel_facts.get(db, QR_KEY)

    lines = [
        f"Request {order.reference} sent to the owner.",
        f"Service: {service.name}",
        f"Amount: {_rupees(order.amount)}",
    ]
    if order.bed_id:
        lines.append(f"Bed: {order.bed_id}")
    lines += [
        "",
        f"Pay to UPI: {upi}",
    ]
    if qr:
        lines.append(f"Scan the QR: {qr}")
    lines += [
        "Send your payment screenshot here after paying.",
        "",
        "Please WAIT for the final confirmation. The owner will first confirm "
        "your reservation and the payment, and only then is it confirmed.",
        "Until then, please do not send another request. You can still ask me "
        "about prices, directions or facilities while you wait.",
    ]
    event = {
        "type": "service_requested",
        "reference": order.reference,
        "service_code": service_code,
        "service_name": service.name,
        "amount": str(order.amount),
        "bed_id": order.bed_id,
        "guest_name": name,
    }
    return FlowResult("\n".join(lines), events=(event,))


# --- helpers ---------------------------------------------------------------

def _needs_bed(db: Database, state: dict) -> bool:
    try:
        return pricing.get_service(db, state.get("service_code", "")).needs_bed
    except DomainError:
        return False


def _window(state: dict) -> tuple[datetime, datetime]:
    start_at = state.get("starts_at")
    if isinstance(start_at, str):
        start_at = datetime.fromisoformat(start_at)
    if not start_at:
        start_at = datetime.now().astimezone()
    minutes = int(state.get("minutes") or REST_MINUTES)
    return start_at, start_at + timedelta(minutes=minutes)


def _free_beds(db: Database, state: dict) -> list[str]:
    """Beds free for the rest window, so a rest never collides with a booking."""
    start_at, end_at = _window(state)
    from core import availability
    return availability.available_beds(db, start_at, end_at)


def _hours_to_minutes(text: str | None) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        return None
    hours = int(digits)
    if not 1 <= hours <= REST_HOURS_MAX:
        return None
    return hours * 60


def _bed_from_text(db: Database, text: str | None, state: dict) -> str | None:
    candidate = (text or "").strip().upper()
    if not candidate:
        return None
    free = _free_beds(db, state)
    for bed_id in free:
        if bed_id.upper() == candidate:
            return bed_id
    return None


def _known_name(db: Database, phone: str) -> str | None:
    row = db.query_one(
        "SELECT full_name FROM guests WHERE phone = %s AND full_name IS NOT NULL "
        "AND btrim(full_name) <> ''", (phone,))
    return row["full_name"] if row else None


def _rupees(amount) -> str:
    return f"Rs {money(amount):,.0f}"


QR_KEY = "payment.qr_image_url"
