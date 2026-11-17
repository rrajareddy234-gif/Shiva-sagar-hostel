"""The dashboard must tell the two switches apart on the page itself.

The failure these tests defend against: the owner sees "Automatic replies: ON"
and assumes guests are being messaged, when in fact the deployment master switch
(`SEND_ENABLED`) is off and nothing can leave the building. The page must say
both facts -- the pause state and whether sending is allowed at all -- so "ON"
can never be read as "messages are being delivered".

Only the display is under test here. The resume/pause action must behave exactly
as before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.dashboard_page import render
from api.webhook import create_app
from core import auto_reply
from core.config import Settings
from tests.conftest import (
    APP_SECRET,
    DASHBOARD_PASSWORD,
    TEST_DSN,
    VERIFY_TOKEN,
    FakeChannel,
)

pytestmark = pytest.mark.usefixtures("seeded")


def _settings(*, send: bool) -> Settings:
    return Settings(
        database_url=TEST_DSN, channel_provider="meta_cloud",
        owner_phone="+919999999999", timezone="Asia/Kolkata",
        meta_app_secret=APP_SECRET, meta_access_token="test-token",
        meta_phone_number_id="123456789", meta_verify_token=VERIFY_TOKEN,
        ai_provider="gemini", ai_model="gemini-2.5-flash",
        ai_api_key=None, ai_base_url=None, send_enabled=send)


def _client(*, send: bool, monkeypatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD)
    return TestClient(create_app(_settings(send=send), channel=FakeChannel(), ai=None))


def _page(client) -> str:
    client.post("/dashboard/login", data={"password": DASHBOARD_PASSWORD})
    return client.get("/dashboard").text


# --- the two lines are shown, and kept apart --------------------------------

def test_the_page_shows_both_the_pause_and_the_sending_state(db, monkeypatch):
    page = _page(_client(send=True, monkeypatch=monkeypatch))
    assert "Automatic replies:" in page
    assert "WhatsApp sending:" in page


def test_sending_is_enabled_when_the_master_switch_is_true(db, monkeypatch):
    page = _page(_client(send=True, monkeypatch=monkeypatch))
    assert "WhatsApp sending: ENABLED" in page
    assert "WhatsApp sending: BLOCKED" not in page


def test_sending_is_blocked_when_the_master_switch_is_false(db, monkeypatch):
    page = _page(_client(send=False, monkeypatch=monkeypatch))
    assert "WhatsApp sending: BLOCKED" in page


def test_on_replies_never_implies_delivery_while_sending_is_blocked(db, monkeypatch):
    """The exact confusion: replies ON, but the deployment cannot send."""
    auto_reply.set_paused(db, False, actor="test")  # replies ON
    page = _page(_client(send=False, monkeypatch=monkeypatch))
    assert "Automatic replies: ON" in page
    assert "WhatsApp sending: BLOCKED" in page
    # The page names the switch and says plainly that nothing leaves.
    assert "SEND_ENABLED" in page
    assert ("No outbound WhatsApp messages can leave this deployment while "
            "SEND_ENABLED is disabled.") in page


def test_both_states_are_shown_when_both_are_on(db, monkeypatch):
    auto_reply.set_paused(db, False, actor="test")
    page = _page(_client(send=True, monkeypatch=monkeypatch))
    assert "Automatic replies: ON" in page
    assert "WhatsApp sending: ENABLED" in page


def test_the_paused_line_and_a_blocked_sending_line_can_coexist(db, monkeypatch):
    auto_reply.set_paused(db, True, actor="test")
    page = _page(_client(send=False, monkeypatch=monkeypatch))
    assert "Automatic replies: PAUSED" in page
    assert "WhatsApp sending: BLOCKED" in page


# --- the resume/pause action is unchanged -----------------------------------

def test_resume_still_lifts_the_pause_even_when_sending_is_blocked(db, monkeypatch):
    client = _client(send=False, monkeypatch=monkeypatch)
    _page(client)
    auto_reply.set_paused(db, True, actor="test")
    client.post("/dashboard/auto-reply", data={"action": "resume"})
    assert auto_reply.is_paused(db) is False, "the resume action must still work"


def test_pause_still_works_when_sending_is_enabled(db, monkeypatch):
    client = _client(send=True, monkeypatch=monkeypatch)
    _page(client)
    client.post("/dashboard/auto-reply", data={"action": "pause"})
    assert auto_reply.is_paused(db) is True


def test_the_button_follows_the_pause_not_the_sending_state(db, monkeypatch):
    client = _client(send=False, monkeypatch=monkeypatch)
    page = _page(client)
    assert "Pause replies" in page, "replies are on, so the pause button shows"
    auto_reply.set_paused(db, True, actor="test")
    page = client.get("/dashboard").text
    assert "Resume replies" in page


# --- missing / invalid environment values -----------------------------------

def test_missing_send_enabled_reads_as_blocked(db, monkeypatch):
    monkeypatch.delenv("SEND_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    settings = Settings.from_env()
    assert settings.send_enabled is False
    page = render(db, settings=settings)
    assert "WhatsApp sending: BLOCKED" in page


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "2", "enabled", "tru"])
def test_invalid_send_enabled_reads_as_blocked(db, monkeypatch, raw):
    monkeypatch.setenv("SEND_ENABLED", raw)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    settings = Settings.from_env()
    assert settings.send_enabled is False, f"{raw!r} must not enable sending"
    page = render(db, settings=settings)
    assert "WhatsApp sending: BLOCKED" in page


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", " True "])
def test_valid_send_enabled_reads_as_enabled(db, monkeypatch, raw):
    monkeypatch.setenv("SEND_ENABLED", raw)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    settings = Settings.from_env()
    assert settings.send_enabled is True
    page = render(db, settings=settings)
    assert "WhatsApp sending: ENABLED" in page


def test_render_without_settings_reports_sending_blocked(db):
    """A caller that passes no settings must not be shown a false ENABLED."""
    page = render(db)
    assert "WhatsApp sending: BLOCKED" in page
