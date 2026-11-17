"""Guest reviews: quoted as stored, never invented, never a price."""

from __future__ import annotations

import pytest

from core import reviews, router
from core.errors import ValidationError


def test_no_reviews_is_told_honestly(db):
    """An empty hostel must not be handed a made-up compliment."""
    text = reviews.render_for_guest(db)
    assert "don't have any guest reviews" in text
    assert "★" not in text


def test_a_stored_review_is_quoted_with_the_name_and_stars(db):
    reviews.add(db, guest_name="Ravi", body="Rooms are clean and the food is good.",
                actor="owner", rating=5)
    text = reviews.render_for_guest(db)
    assert "Ravi" in text
    assert "clean and the food is good" in text
    assert "★★★★★" in text


def test_a_review_without_a_rating_has_no_stars(db):
    reviews.add(db, guest_name="Sita", body="Quiet and safe.", actor="owner")
    text = reviews.render_for_guest(db)
    assert "Sita" in text
    assert "★" not in text


def test_a_review_must_not_carry_a_price(db):
    """A figure inside a review could be quoted back as a price."""
    for body in ("Great value at Rs 8000 a month", "Only ₹350 a night",
                 "Paid 5000 rupees, worth it"):
        with pytest.raises(ValidationError):
            reviews.add(db, guest_name="Ravi", body=body, actor="owner")
    assert reviews.count(db) == 0


def test_a_rating_outside_one_to_five_is_refused(db):
    for bad in (0, 6, -1):
        with pytest.raises(ValidationError):
            reviews.add(db, guest_name="Ravi", body="Fine.", actor="owner",
                        rating=bad)


def test_a_blank_review_is_refused(db):
    with pytest.raises(ValidationError):
        reviews.add(db, guest_name="", body="Fine.", actor="owner")
    with pytest.raises(ValidationError):
        reviews.add(db, guest_name="Ravi", body="   ", actor="owner")


def test_an_unpublished_review_is_hidden_from_guests(db):
    review_id = reviews.add(db, guest_name="Ravi", body="All good.", actor="owner")
    reviews.set_published(db, review_id, published=False, actor="owner")
    assert reviews.count(db) == 0
    assert "Ravi" not in reviews.render_for_guest(db)
    # The owner can put it back.
    reviews.set_published(db, review_id, published=True, actor="owner")
    assert "Ravi" in reviews.render_for_guest(db)


def test_only_the_most_recent_reviews_are_shown(db):
    for i in range(7):
        reviews.add(db, guest_name=f"Guest {i}", body=f"Review number {i}.",
                    actor="owner")
    rows = reviews.published(db, limit=5)
    assert len(rows) == 5
    assert rows[0]["guest_name"] == "Guest 6"


def test_tapping_guest_reviews_answers_without_the_ai(db):
    reviews.add(db, guest_name="Ravi", body="Rooms are clean.", actor="owner",
                rating=4)
    reply = router.route(db, "Guest Reviews", button_id="menu:reviews")
    assert reply.handled is True
    assert reply.needs_ai is False
    assert reply.intent == "reviews"
    assert "Ravi" in reply.text


def test_adding_a_review_is_audited(db):
    review_id = reviews.add(db, guest_name="Ravi", body="Good stay.", actor="owner")
    entry = db.query_one(
        "SELECT action FROM audit_log WHERE entity = 'guest_review' "
        "AND entity_id = %s ORDER BY id DESC LIMIT 1",
        (str(review_id),))
    assert entry["action"] == "review_added"
