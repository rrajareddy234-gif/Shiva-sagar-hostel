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


def test_the_guest_screenshot_renders_as_an_image_bubble():
    html = _bubble({"from": "them", "text": "[screenshot]", "media": True})
    assert "payment-screenshot.jpg" in html
    assert "thumb" in html


def test_the_forwarded_proof_shows_the_filename_and_caption_escaped():
    html = _bubble({"from": "bot", "type": "document",
                    "filename": "SPG-1-payment.jpg",
                    "text": "Check it <script>x</script>"})
    assert "SPG-1-payment.jpg" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_timeline_shows_both_people_and_both_decisions():
    """The demo must show the real two-step journey, not one side of it.

    The whole point of the page is that a bed is confirmed only after the owner
    has seen the payment. That needs the guest's request and screenshot, and the
    owner's Approve and Confirm taps, in one ordered timeline.
    """
    from scripts.build_demo_page import TIMELINE
    who = [w for w, _ in TIMELINE]
    steps = [m for _, m in TIMELINE]
    assert "guest" in who and "owner" in who
    assert steps.index("[Approve]") < steps.index("[screenshot]") < steps.index("[Confirm]")
    assert "[screenshot]" in steps
