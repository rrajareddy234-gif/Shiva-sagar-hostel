"""The preview site must never leak raw HTML from a guest's message."""

from __future__ import annotations

from scripts.build_demo_page import _bubble, _conversation, _esc


def test_esc_escapes_markup():
    assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_guest_text_is_escaped_in_the_bubble():
    turn = {"from": "them", "text": "<img src=x onerror=alert(1)>"}
    html = _bubble(turn)
    assert "<img" not in html
    assert "&lt;img" in html


def test_bot_text_is_escaped_and_newlines_become_breaks():
    turn = {"from": "bot", "type": "text", "text": "line one\nline two <b>x</b>"}
    html = _bubble(turn)
    assert "line one<br>line two" in html
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_a_tap_is_marked_and_options_are_listed():
    tap = _bubble({"from": "them", "text": "AC", "tap": True})
    assert "tapped" in tap

    prompt = {
        "from": "bot",
        "type": "buttons",
        "text": "Which room?",
        "button_text": "Choose",
        "options": [{"id": "price:room:AC", "title": "AC"}],
    }
    html = _bubble(prompt)
    assert "Which room?" in html
    assert "AC" in html


def test_conversation_keeps_every_turn_in_order():
    turns = [
        {"from": "them", "text": "first"},
        {"from": "bot", "type": "text", "text": "second"},
        {"from": "them", "text": "third"},
    ]
    html = _conversation(turns)
    assert html.index("first") < html.index("second") < html.index("third")
