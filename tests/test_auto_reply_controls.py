"""Ways to pause and resume automatic replies: the dashboard, a text, the AI.

The owner is not a programmer, so the switch has to be reachable without the
server. These tests defend three things:

* the owner can pause and resume from the dashboard, from a WhatsApp text, and
  by asking the assistant in their own words;
* the state lives in the database, so it survives a restart and takes effect at
  once -- no `.env` edit and no SSH;
* only the owner can change it, and every change is audited.
"""

from __future__ import annotations

import pytest

from core import auto_reply, commands, security, tools
from core.db import Database
from core.tools import ToolContext
from tests.conftest import DASHBOARD_PASSWORD, TEST_DSN

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
GUEST = "+919000000701"


def _handle(db, text, *, phone=OWNER):
    return commands.handle(db, phone=phone, text=text, owner_phone=OWNER)


# --- the switch itself ------------------------------------------------------

def test_default_is_enabled_when_nothing_has_been_chosen(db):
    assert auto_reply.is_paused(db) is False


def test_pausing_then_resuming_round_trips(db):
    auto_reply.set_paused(db, True, actor="test")
    assert auto_reply.is_paused(db) is True
    auto_reply.set_paused(db, False, actor="test")
    assert auto_reply.is_paused(db) is False


def test_the_choice_is_stored_in_the_database_not_in_memory(db):
    """It must survive a restart, so it cannot live only in the process."""
    auto_reply.set_paused(db, True, actor="test")
    # A second connection sees the same choice, as a restart would.
    fresh = Database(TEST_DSN)
    assert auto_reply.is_paused(fresh) is True


def test_the_change_is_audited_with_who_did_it(db):
    auto_reply.set_paused(db, True, actor="owner-dashboard")
    row = db.query_one(
        "SELECT actor, action FROM audit_log WHERE action = 'auto_reply_paused'")
    assert row is not None, "a change to the switch must be auditable"
    assert row["actor"] == "owner-dashboard"


def test_a_broken_setting_falls_back_to_the_env_default(db):
    """A pause is explicit; a bad value must not silently change behaviour."""
    db.execute("INSERT INTO settings (key, value) VALUES (%s, %s::jsonb)",
               (auto_reply.KEY, '"not-a-bool"'))
    assert auto_reply.is_paused(db, default_enabled=True) is False
    assert auto_reply.is_paused(db, default_enabled=False) is True


# --- from WhatsApp: typed commands ------------------------------------------

@pytest.mark.parametrize("text", [
    "pause replies", "Pause Replies", "stop replying", "pause", "mute",
    "stop auto replies", "disable auto reply", "turn off replies",
])
def test_owner_can_pause_by_text(db, text):
    result = _handle(db, text)
    assert result is not None and result.applied
    assert auto_reply.is_paused(db) is True


@pytest.mark.parametrize("text", [
    "resume replies", "Resume", "unpause", "start replying",
    "turn on auto reply", "enable replies", "unmute",
])
def test_owner_can_resume_by_text(db, text):
    auto_reply.set_paused(db, True, actor="test")
    result = _handle(db, text)
    assert result is not None and result.applied
    assert auto_reply.is_paused(db) is False


@pytest.mark.parametrize("text", [
    "auto reply status", "are replies on?", "is the bot paused",
    "auto reply enabled?",
])
def test_owner_can_check_status_by_text(db, text):
    auto_reply.set_paused(db, True, actor="test")
    result = _handle(db, text)
    assert result is not None
    assert "PAUSED" in result.text.upper()


def test_the_pause_confirmation_reaches_the_owner(db):
    """A pause that told nobody would leave the owner unsure it worked."""
    result = _handle(db, "pause replies")
    assert "paused" in result.text.lower()


def test_owner_help_lists_the_pause_and_resume_words(db):
    result = _handle(db, "owner help")
    assert result is not None
    assert "pause replies" in result.text
    assert "resume replies" in result.text


def test_a_guest_cannot_pause_the_system(db):
    """The command path is gated on the sender being the owner."""
    result = _handle(db, "pause replies", phone=GUEST)
    assert result is None, "a guest message is not an owner command"
    assert auto_reply.is_paused(db) is False


def test_a_customer_claiming_to_be_the_owner_still_cannot_pause(db):
    result = commands.handle(db, phone=GUEST, text="pause replies",
                             owner_phone=OWNER)
    assert result is None
    assert auto_reply.is_paused(db) is False


def test_pause_is_not_read_as_cancel(db):
    """'stop' on its own cancels a staged command; 'stop replying' pauses."""
    assert commands.parse_command("stop replying")["action"] == "pause_replies"
    assert commands.is_cancellation("stop")


# --- from the dashboard -----------------------------------------------------

def _login(client) -> None:
    # The client follows redirects, so a good password lands on the page (200)
    # while a wrong one stays on the login form (401).
    response = client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    assert response.status_code == 200, "the login must succeed"


def test_dashboard_button_pauses_and_resumes(client, db):
    _login(client)
    client.post("/dashboard/auto-reply", data={"action": "pause"})
    assert auto_reply.is_paused(db) is True

    client.post("/dashboard/auto-reply", data={"action": "resume"})
    assert auto_reply.is_paused(db) is False


def test_the_dashboard_shows_the_state_and_the_button(client, db):
    _login(client)
    page = client.get("/dashboard").text
    assert "Pause replies" in page, "the button must be on the page when on"

    auto_reply.set_paused(db, True, actor="test")
    page = client.get("/dashboard").text
    assert "Resume replies" in page
    assert "PAUSED" in page


def test_the_dashboard_refuses_an_anonymous_pause(client, db):
    # No cookie: the request must not change the switch.
    client.post("/dashboard/auto-reply", data={"action": "pause"})
    assert auto_reply.is_paused(db) is False


def test_the_dashboard_rejects_an_unknown_action(client, db):
    _login(client)
    client.post("/dashboard/auto-reply", data={"action": "explode"})
    assert auto_reply.is_paused(db) is False


# --- from the assistant, in the owner's own words ---------------------------

def _ctx(db, *, phone=OWNER, role=security.OWNER) -> ToolContext:
    return ToolContext(db=db, phone=phone, role=role)


def test_the_ai_tool_can_pause_and_resume(db):
    assert tools.execute(_ctx(db), "auto_reply", {"action": "pause"})["paused"] is True
    assert auto_reply.is_paused(db) is True
    assert tools.execute(_ctx(db), "auto_reply", {"action": "resume"})["paused"] is False
    assert auto_reply.is_paused(db) is False


def test_the_ai_tool_reports_status(db):
    auto_reply.set_paused(db, True, actor="test")
    result = tools.execute(_ctx(db), "auto_reply", {"action": "status"})
    assert result["paused"] is True


def test_the_ai_tool_is_hidden_from_guests(db):
    """A guest must not even see the tool, and running it must be refused."""
    assert "auto_reply" not in [s.name for s in tools.specs_for(security.CUSTOMER)]
    result = tools.execute(_ctx(db, phone=GUEST, role=security.CUSTOMER),
                           "auto_reply", {"action": "pause"})
    assert "error" in result
    assert auto_reply.is_paused(db) is False


def test_the_ai_tool_is_hidden_from_staff(db):
    assert "auto_reply" not in [s.name for s in tools.specs_for(security.STAFF)]
