"""The FAQ layer: answer from storage, never guess, record the misses.

The whole point is to avoid a paid AI call for a question already answered.
These tests hold two lines: a real rephrasing is answered without the AI, and
an unrelated question is NOT answered from storage -- a confident wrong answer
is worse than an AI call.
"""

from __future__ import annotations

import pytest

from core import faq, router

pytestmark = pytest.mark.usefixtures("seeded")


@pytest.fixture
def stocked(db):
    faq.add(db, question="What time is check-in?",
            answer="Check-in is from 11am.",
            aliases=["check in time", "when can i check in", "checkin timing"])
    faq.add(db, question="Is there wifi?",
            answer="Yes, there is wifi on every floor.",
            aliases=["do you have internet", "wifi facility", "internet"])
    faq.add(db, question="Where is the hostel?",
            answer="We are near the bus stand in Macherla.",
            aliases=["address", "location", "how to reach you"])
    return db


def test_a_real_rephrasing_is_answered_without_the_ai(stocked):
    reply = router.route(stocked, "when can i check in")
    assert reply.handled is True
    assert reply.needs_ai is False
    assert reply.intent == "faq"
    assert "11am" in reply.text


def test_a_short_alias_answers(stocked):
    reply = router.route(stocked, "wifi?")
    assert reply.handled is True
    assert "wifi" in reply.text.lower()


def test_an_unrelated_question_is_not_answered_from_storage(stocked):
    reply = router.route(stocked, "my dog's name is Bruno")
    assert reply.handled is False
    assert reply.needs_ai is True


def test_a_near_miss_is_handed_to_the_ai(stocked):
    # Close in topic, but not the question asked. Must not be answered here.
    reply = router.route(stocked, "do you allow unmarried couples to stay")
    assert reply.handled is False
    assert reply.needs_ai is True


def test_a_miss_is_recorded_so_it_can_be_added(stocked):
    router.route(stocked, "do you allow pets in the room")
    misses = [row["asked"] for row in faq.recent_misses(stocked)]
    assert "do you allow pets in the room" in misses


def test_a_hit_is_recorded_as_answered(stocked):
    router.route(stocked, "check in time")
    row = stocked.query_one(
        "SELECT answered FROM faq_hits ORDER BY hit_id DESC LIMIT 1")
    assert row["answered"] is True


def test_recording_never_breaks_a_reply(stocked):
    # record() swallows its own errors, so a broken write cannot lose an answer.
    found = faq.match(stocked, "check in time")
    assert found is not None
    faq.record(stocked, "check in time", found)  # must not raise
    assert faq.answer(stocked, "check in time") == "Check-in is from 11am."


def test_an_inactive_entry_is_not_used(stocked):
    faq.deactivate(stocked, 1)
    assert faq.answer(stocked, "what time is check-in?") is None


def test_an_empty_message_matches_nothing(stocked):
    assert faq.match(stocked, "") is None


@pytest.mark.parametrize("text", [
    "[With food]",          # a button label the guest tapped
    "[AC]",
    "wifi (free?)",
    "check-in \\ backslash",
    "a+b*c?",
    "hot water [24x7]",
])
def test_a_short_message_with_regex_characters_does_not_crash(stocked, text):
    """Guest text is data, never a regex.

    A tapped button arrives as a short message. `[With food]` was interpolated
    into a regex unescaped, and Postgres rejected the whole expression, so the
    guest got the error fallback instead of an answer. Whatever a guest types
    must be answered or ignored, never raised.
    """
    result = faq.match(stocked, text)
    assert result is None or result.answer


def test_a_word_is_matched_whole_not_as_a_substring(stocked):
    # "wifi" must match "wifi", but "wif" must not pull up the wifi entry.
    assert faq.match(stocked, "wifi") is not None
    assert faq.match(stocked, "wif") is None
    assert faq.match(stocked, "  ") is None
    assert faq.match(stocked, None) is None


def test_a_grammar_word_never_pulls_up_an_unrelated_answer(stocked):
    """A question must not be answered by a shared stopword.

    "is there parking" shares the word "there" with "Is there Wi-Fi?" and, with
    the stopword counted, matched the Wi-Fi entry -- telling a guest asking
    about parking that Wi-Fi was available. A confidently wrong answer is worse
    than no answer, so the grammar words are not searchable.
    """
    assert faq.match(stocked, "is there parking") is None
    assert faq.match(stocked, "is there anything for parking") is None
    # The real questions still answer, including on the short-keyword path.
    assert "wifi" in (faq.answer(stocked, "is there wifi") or "").lower()
    assert faq.match(stocked, "wifi?") is not None


def test_re_adding_a_question_replaces_it(stocked):
    first = faq.add(stocked, question="What time is check-in?",
                    answer="Check-in is from 11am.")
    second = faq.add(stocked, question="What time is check-in?",
                     answer="Check-in is from noon.")
    assert first == second
    assert faq.answer(stocked, "what time is check-in?") == "Check-in is from noon."


def test_the_seed_answers_room_and_food_questions(db):
    from pathlib import Path
    seed = Path("db/seed.sql").read_text()
    assert "What kind of rooms do you have?" in seed
    assert "dormitory" in seed


def test_facilities_falls_back_when_the_owner_has_not_written_one(db):
    # No wifi entry stored: the generic list must still answer, as before.
    reply = router.route(db, "is wifi available")
    assert reply.handled is True
    assert "Sagar PG" in reply.text


def test_the_owner_can_see_the_worklist_in_chat(stocked):
    router.route(stocked, "do you allow pets")
    reply = router.owner_answer(stocked, "what is on the faq list")
    assert reply.handled is True
    assert reply.intent == "owner_faq_misses"
    assert "do you allow pets" in reply.text


def test_owner_faq_does_not_hijack_unanswered_enquiries(stocked):
    reply = router.owner_answer(stocked, "unanswered enquiries")
    assert reply.intent != "owner_faq_misses"
