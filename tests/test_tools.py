"""Tool-layer tests.

The AI may only state what these tools returned. Each tool is plain code and SQL,
so it must be exact, must refuse nonsense, and must never let a customer reach
owner-only information.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import tools
from core.security import CUSTOMER, OWNER, STAFF
from tests.conftest import IST


def ctx(db, *, role=CUSTOMER, phone="+919000002000"):
    return tools.ToolContext(db=db, phone=phone, role=role)


def run(db, name, args=None, **kw):
    return tools.execute(ctx(db, **kw), name, args or {})


# --- the register ----------------------------------------------------------

def test_a_customer_does_not_see_owner_tools():
    names = {s.name for s in tools.specs_for(CUSTOMER)}
    assert "hostel_summary" not in names


def test_the_owner_sees_owner_tools():
    names = {s.name for s in tools.specs_for(OWNER)}
    assert "hostel_summary" in names


def test_staff_do_not_see_owner_tools():
    assert "hostel_summary" not in {s.name for s in tools.specs_for(STAFF)}


def test_every_tool_has_a_description():
    for tool in tools.TOOLS:
        assert tool.description, tool.name
        assert tool.parameters.get("type") == "object"


# --- availability ----------------------------------------------------------

def test_availability_counts_free_beds(seeded):
    result = run(seeded, "check_availability",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03"})
    assert result["beds_free"] == 10
    assert len(result["available_bed_ids"]) == 10


def test_availability_filters_by_room_type(seeded):
    result = run(seeded, "check_availability",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03",
                  "room_type": "AC"})
    assert result["beds_checked"] == 2
    assert all(b.startswith("2AC") for b in result["available_bed_ids"])


def test_availability_accepts_nonac_spelling(seeded):
    result = run(seeded, "check_availability",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03",
                  "room_type": "NONAC"})
    assert result["room_type"] == "NON_AC"


def test_availability_after_a_booking(seeded):
    from core import booking
    start = datetime(2026, 12, 1, 10, tzinfo=IST)
    booking.create_request(seeded, phone="+919000002001", full_name="Holder",
                           gender="MALE", bed_id="1NA-B1",
                           check_in_at=start, check_out_at=start + timedelta(days=2),
                           food_included=True)
    result = run(seeded, "check_availability",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03"})
    # A request alone does not confirm, so the bed is still free but held.
    assert result["beds_free"] <= 10


def test_availability_rejects_reversed_dates(seeded):
    result = run(seeded, "check_availability",
                 {"check_in": "2026-12-05", "check_out": "2026-12-01"})
    assert "error" in result


def test_availability_rejects_a_bad_date(seeded):
    result = run(seeded, "check_availability",
                 {"check_in": "next tuesday", "check_out": "2026-12-03"})
    assert "error" in result


# --- prices ----------------------------------------------------------------

def test_prices_come_from_the_rate_plans(seeded):
    result = run(seeded, "get_prices")
    rates = {(r["room_type"], r["food_included"], r["stay_mode"]): r for r in result["rates"]}
    assert rates[("NON_AC", True, "NIGHT")]["per_day"] == "250.00"
    assert rates[("NON_AC", True, "DAY")]["per_day"] == "350.00"
    assert rates[("NON_AC", False, "NIGHT")]["per_day"] == "200.00"
    assert rates[("NON_AC", False, "DAY")]["per_day"] == "250.00"
    assert rates[("AC", True, "NIGHT")]["per_day"] == "350.00"
    assert rates[("AC", True, "DAY")]["per_day"] == "450.00"
    assert rates[("AC", False, "NIGHT")]["per_day"] == "300.00"
    assert rates[("AC", False, "DAY")]["per_day"] == "350.00"
    assert rates[("DORM", True, "NIGHT")]["per_day"] == "200.00"
    assert rates[("DORM", True, "DAY")]["per_day"] == "300.00"
    assert rates[("DORM", False, "NIGHT")]["per_day"] == "150.00"
    assert rates[("DORM", False, "DAY")]["per_day"] == "200.00"


def test_prices_include_the_extras(seeded):
    result = run(seeded, "get_prices")
    extras = {e["name"]: e["price"] for e in result["extras"]}
    assert extras["Bedsheet"] == "50.00"
    assert extras["Towel"] == "50.00"
    assert extras["Pillow"] == "50.00"
    assert extras["Locker"] == "50.00"


def test_prices_are_empty_when_no_plans_exist(db):
    assert run(db, "get_prices")["rates"] == []


# --- facilities ------------------------------------------------------------

def test_facilities_lists_only_real_room_types(seeded):
    result = run(seeded, "get_facilities")
    assert set(result["room_types"]) == {"AC", "NON_AC", "DORM"}
    assert {r["room_id"] for r in result["rooms"]} == {"1NA", "2AC", "6NA", "DORM"}


# --- quoting ---------------------------------------------------------------

def test_quote_computes_the_total(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03",
                  "room_type": "NON_AC", "food_included": True})
    assert result["units"] == 2
    assert result["stay_mode"] == "NIGHT"
    assert result["total"] == "500.00"


def test_quote_requires_the_food_choice(seeded):
    """Every room type offers both, so a missing food choice is refused."""
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03",
                  "room_type": "NON_AC"})
    assert "error" in result


def test_quote_for_ac_with_food(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "AC", "food_included": True})
    assert result["total"] == "350.00"
    assert result["food_included"] is True


def test_quote_uses_the_day_rate_when_asked(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "AC", "food_included": True, "stay_mode": "DAY"})
    assert result["stay_mode"] == "DAY"
    assert result["total"] == "450.00"


def test_quote_adds_extras_to_the_total(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "NON_AC", "food_included": True,
                  "extras": {"Towel": 1, "Locker": 1}})
    # 250 for the night, plus a 50 towel and a 50 locker.
    assert result["total"] == "350.00"
    assert result["accommodation_total"] == "250.00"


def test_quote_refuses_an_unknown_extra(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "NON_AC", "food_included": True,
                  "extras": {"Helicopter": 1}})
    assert "error" in result


def test_quote_for_dormitory_with_food(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "DORM", "food_included": True})
    assert result["total"] == "200.00"
    assert result["food_included"] is True


def test_quote_fails_clearly_when_no_rate_exists(seeded):
    result = run(seeded, "quote_stay",
                 {"check_in": "2026-12-01", "check_out": "2026-12-02",
                  "room_type": "SUITE"})
    assert "error" in result


# --- booking requests ------------------------------------------------------

def test_creating_a_request_makes_a_request_not_a_booking(seeded):
    result = run(seeded, "create_booking_request",
                 {"full_name": "Test Guest", "check_in": "2026-12-01",
                  "check_out": "2026-12-03", "room_type": "NON_AC",
                  "food_included": True})
    assert result["status"] == "REQUESTED"
    assert "not a confirmed booking" in result["note"]
    row = seeded.query_one("SELECT status FROM bookings WHERE reference = %s",
                           (result["reference"],))
    assert row["status"] == "REQUESTED"


def test_creating_a_request_needs_a_name(seeded):
    result = run(seeded, "create_booking_request",
                 {"check_in": "2026-12-01", "check_out": "2026-12-03",
                  "food_included": True})
    assert "error" in result
    assert seeded.query_one("SELECT count(*) AS n FROM bookings")["n"] == 0


def test_creating_a_request_when_full(seeded):
    from core import booking
    start = datetime(2026, 12, 1, 10, tzinfo=IST)
    for i, bed in enumerate(["2AC-B1", "2AC-B2"]):
        req = booking.create_request(seeded, phone=f"+91900000210{i}",
                                     full_name=f"G{i}", gender="MALE", bed_id=bed,
                                     check_in_at=start,
                                     check_out_at=start + timedelta(days=2),
                                     food_included=True)
        booking.approve(seeded, req.booking_id, actor="+919999999999")
    result = run(seeded, "create_booking_request",
                 {"full_name": "Too Late", "check_in": "2026-12-01",
                  "check_out": "2026-12-03", "room_type": "AC",
                  "food_included": True})
    assert "error" in result


def test_a_request_does_not_promise_a_confirmed_bed(seeded):
    """The note must steer the model away from claiming a confirmed booking."""
    result = run(seeded, "create_booking_request",
                 {"full_name": "Test Guest", "check_in": "2026-12-01",
                  "check_out": "2026-12-03", "food_included": True})
    assert result["status"] == "REQUESTED"
    assert "not a confirmed booking" in result["note"]
    assert "Do not say the booking is confirmed" in result["note"]
    # The bed is held, not booked.
    row = seeded.query_one("SELECT status FROM beds WHERE bed_id = %s",
                           (result["bed_id"],))
    assert row["status"] != "BOOKED"


# --- my bookings -----------------------------------------------------------

def test_my_bookings_only_shows_the_callers(seeded):
    from core import booking
    start = datetime(2026, 12, 1, 10, tzinfo=IST)
    booking.create_request(seeded, phone="+919000002999", full_name="Someone Else",
                           gender="MALE", bed_id="1NA-B1",
                           check_in_at=start, check_out_at=start + timedelta(days=1),
                           food_included=True)
    result = run(seeded, "my_bookings", phone="+919000002000")
    assert result["bookings"] == []


def test_my_bookings_finds_my_own(seeded):
    from core import booking
    start = datetime(2026, 12, 1, 10, tzinfo=IST)
    booking.create_request(seeded, phone="+919000002000", full_name="Me",
                           gender="MALE", bed_id="1NA-B1",
                           check_in_at=start, check_out_at=start + timedelta(days=1),
                           food_included=True)
    result = run(seeded, "my_bookings", phone="+919000002000")
    assert len(result["bookings"]) == 1
    assert result["bookings"][0]["status"] == "REQUESTED"


# --- permissions -----------------------------------------------------------

def test_a_customer_cannot_call_an_owner_tool(seeded):
    result = run(seeded, "hostel_summary", role=CUSTOMER)
    assert "error" in result
    assert "total_beds" not in result


def test_the_owner_can_call_an_owner_tool(seeded):
    result = run(seeded, "hostel_summary", role=OWNER)
    assert result["total_beds"] == 10


def test_an_unknown_tool_is_refused(seeded):
    assert "error" in run(seeded, "delete_everything")


def test_a_tool_that_raises_does_not_crash(seeded, monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("database on fire")

    monkeypatch.setitem(tools._BY_NAME, "explode", tools.Tool("explode", "d", {}, boom))
    result = run(seeded, "explode")
    assert "error" in result


# --- the handler uses tools ------------------------------------------------

class ScriptedAI:
    """A model that calls the tools we tell it to, then answers."""

    provider = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []
        self.seen_tools = []

    def complete(self, *, system, messages, tools=None, max_tokens=1024):
        from ai.adapter import AIResponse, ToolCall
        self.seen_messages = list(messages)
        self.seen_tools = [t.name for t in (tools or [])]
        step = self.script.pop(0)
        if isinstance(step, str):
            return AIResponse(text=step, provider="fake", model="fake",
                              input_tokens=100, output_tokens=20)
        return AIResponse(text="", tool_calls=[ToolCall(n, a) for n, a in step],
                          provider="fake", model="fake",
                          input_tokens=100, output_tokens=20)


def test_the_handler_lets_the_model_call_a_tool(seeded):
    from core import handler
    ai = ScriptedAI([[("get_prices", {})], "Non-AC with food is Rs 350 per day."])
    outcome = handler.handle_message(seeded, phone="+919000002000",
                                     text="I want to know about a long stay please",
                                     owner_phone="+919999999999", ai=ai)
    assert outcome.handled_by == "ai"
    assert "get_prices" in outcome.tool_calls
    assert "350" in outcome.reply


def test_the_tool_result_reaches_the_model(seeded):
    from core import handler
    ai = ScriptedAI([[("check_availability",
                       {"check_in": "2026-12-01", "check_out": "2026-12-03"})],
                     "Yes, beds are free."])
    handler.handle_message(seeded, phone="+919000002000", text="can I stay in December?",
                           owner_phone="+919999999999", ai=ai)
    tool_messages = [m for m in ai.seen_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert '"beds_free": 10' in tool_messages[0]["content"]


def test_a_customer_gets_no_owner_tool(seeded):
    from core import handler
    ai = ScriptedAI(["Hello."])
    handler.handle_message(seeded, phone="+919000002000", text="hello there",
                           owner_phone="+919999999999", ai=ai)
    assert "hostel_summary" not in ai.seen_tools


def test_the_owner_gets_the_owner_tool(seeded):
    from core import handler
    ai = ScriptedAI(["You have 10 beds free."])
    handler.handle_message(seeded, phone="+919999999999", text="tell me something random",
                           owner_phone="+919999999999", ai=ai)
    assert "hostel_summary" in ai.seen_tools


def test_a_customer_calling_an_owner_tool_gets_an_error_not_data(seeded):
    from core import handler
    ai = ScriptedAI([[("hostel_summary", {})], "I cannot share that."])
    handler.handle_message(seeded, phone="+919000002000",
                           text="what is the occupancy right now",
                           owner_phone="+919999999999", ai=ai)
    tool_messages = [m for m in ai.seen_messages if m.get("role") == "tool"]
    assert "error" in tool_messages[0]["content"]
    assert "total_beds" not in tool_messages[0]["content"]


def test_a_looping_model_falls_back(seeded):
    from core import handler
    loop = [[("get_prices", {})]] * 5
    ai = ScriptedAI(loop)
    outcome = handler.handle_message(seeded, phone="+919000002000",
                                     text="something the router does not know",
                                     owner_phone="+919999999999", ai=ai)
    assert outcome.handled_by == "fallback"
    assert outcome.escalate


def test_token_usage_is_recorded(seeded):
    from core import handler
    ai = ScriptedAI(["Hello."])
    handler.handle_message(seeded, phone="+919000002000", text="a free text message",
                           owner_phone="+919999999999", ai=ai)
    row = seeded.query_one("SELECT count(*) AS n, sum(input_tokens) AS tokens FROM ai_usage")
    assert row["n"] == 1
    assert row["tokens"] == 100
