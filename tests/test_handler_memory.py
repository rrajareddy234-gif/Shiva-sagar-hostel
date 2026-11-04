"""The handler's wiring: memory in the system prompt, one copy of the message.

These use a fake AI client, so no network call is made. The point is to pin two
things that were previously wrong or missing:

  * the caller's own message is sent to the model exactly once;
  * what the database knows about the caller is put in the system prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ai.adapter import AIClient, AIResponse
from core import booking, handler, security
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"


class FakeClient(AIClient):
    provider = "fake"

    def __init__(self, text="ok"):
        self.model = "fake-1"
        self._text = text
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools=None, max_tokens=1024):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        return AIResponse(text=self._text, input_tokens=10, output_tokens=5,
                          model=self.model, provider=self.provider)


def _stay(db, *, phone="+919000000901", name="Ramesh Kumar"):
    start = datetime.now(IST) + timedelta(days=1)
    return booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id="1NA-B1",
        check_in_at=start, check_out_at=start + timedelta(days=3),
        food_included=True)


def test_a_free_text_message_is_sent_to_the_model_once(db):
    """The stored inbound turn must not be repeated alongside the new message."""
    ai = FakeClient()
    text = "can I bring my pet cat along with me"
    history = [{"role": "user", "content": text}]

    handler.handle_message(db, phone="+919000000901", text=text,
                           owner_phone=OWNER, ai=ai, history=history)

    sent = ai.calls[0]["messages"]
    assert sum(1 for m in sent if m["content"] == text) == 1


def test_history_without_the_current_message_is_kept(db):
    ai = FakeClient()
    history = [{"role": "user", "content": "hello"},
               {"role": "assistant", "content": "hi"}]

    handler.handle_message(db, phone="+919000000901",
                           text="can I bring my pet cat along with me", owner_phone=OWNER,
                           ai=ai, history=history)

    sent = ai.calls[0]["messages"]
    assert [m["content"] for m in sent][:2] == ["hello", "hi"]


def test_the_system_prompt_carries_what_the_database_knows(db):
    _stay(db)
    ai = FakeClient()
    handler.handle_message(db, phone="+919000000901",
                           text="can I bring my pet cat along with me", owner_phone=OWNER, ai=ai)
    system = ai.calls[0]["system"]
    assert "WHAT YOU KNOW ABOUT THIS PERSON" in system
    assert "Ramesh Kumar" in system


def test_the_system_prompt_carries_no_amounts(db):
    from core import payments
    request = _stay(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=750, method="UPI", actor=OWNER, verified=True)
    ai = FakeClient()
    handler.handle_message(db, phone="+919000000901",
                           text="can I bring my pet cat along with me", owner_phone=OWNER, ai=ai)
    assert "750" not in ai.calls[0]["system"]


def test_the_owner_prompt_is_the_owner_prompt(db):
    ai = FakeClient()
    handler.handle_message(db, phone=OWNER, text="can I bring my pet cat along with me",
                           owner_phone=OWNER, ai=ai)
    system = ai.calls[0]["system"]
    assert "speaking with the owner" in system
    assert "Beds free now" in system


def test_a_guest_never_sees_owner_tool_specs(db):
    ai = FakeClient()
    handler.handle_message(db, phone="+919000000901",
                           text="can I bring my pet cat along with me", owner_phone=OWNER, ai=ai)
    names = {t.name for t in ai.calls[0]["tools"]}
    assert "who_is" not in names
    assert "hostel_summary" not in names


def test_a_staff_message_uses_the_staff_prompt(db):
    ai = FakeClient()
    handler.handle_message(db, phone="+919888888888", text="can I bring my pet cat along with me",
                           owner_phone=OWNER, ai=ai, staff_phones=("+919888888888",))
    system = ai.calls[0]["system"]
    assert "staff member" in system
