"""Guest reviews, quoted to a guest exactly as the owner recorded them.

The menu offers "Guest Reviews", so there has to be something to read. A review
is entered by the owner; the assistant quotes it and never writes one. Two
rules hold, and they are the same two that hold for `hostel_facts`.

  * A review is never invented. When none are stored the guest is told that
    plainly, rather than being shown a blank card or a made-up compliment.
  * A review is never a price. A rupee figure inside a review could later be
    quoted as truth, and prices come from the rate card and nowhere else, so a
    review that carries one is refused with an explanation.

Only published reviews reach a guest. Unpublishing hides a review without
losing it, so the owner can take one down and put it back.
"""

from __future__ import annotations

import logging
import re

from core.db import Database
from core.errors import ValidationError

log = logging.getLogger(__name__)

# A review is a short paragraph. A wall of text is not a review.
MAX_BODY_LENGTH = 600
MAX_NAME_LENGTH = 80

VALID_SOURCES = ("owner", "guest")

# A rupee figure in a review would be quoted back as a price. Prices are the
# rate card's alone, so such a review is refused rather than stored.
_MONEY = re.compile(r"(₹|\brs\.?\b|\binr\b|\brupees?\b)", re.I)


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError("a review needs the guest's name")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"a review name must be at most {MAX_NAME_LENGTH} characters")
    return name


def _clean_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise ValidationError("a review needs some words")
    if len(body) > MAX_BODY_LENGTH:
        raise ValidationError(
            f"a review must be at most {MAX_BODY_LENGTH} characters")
    if _MONEY.search(body):
        raise ValidationError(
            "a review must not mention an amount: prices come from the rate "
            "card, and a figure inside a review could be quoted back as one")
    return body


def _clean_rating(rating) -> int | None:
    if rating is None or rating == "":
        return None
    try:
        value = int(rating)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "a rating must be a whole number from 1 to 5") from exc
    if not 1 <= value <= 5:
        raise ValidationError("a rating must be from 1 to 5")
    return value


def add(db: Database, *, guest_name: str, body: str, actor: str,
        rating=None, stayed_on=None, source: str = "owner") -> int:
    """Record one review. Adding one is audited."""
    name = _clean_name(guest_name)
    text = _clean_body(body)
    stars = _clean_rating(rating)
    if source not in VALID_SOURCES:
        raise ValidationError(f"unknown review source {source!r}")

    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO guest_reviews (guest_name, rating, body, stayed_on, source)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING review_id
            """,
            (name, stars, text, stayed_on, source),
        )
        review_id = cur.fetchone()["review_id"]
        cur.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
            "VALUES (%s, 'review_added', 'guest_review', %s, NULL, %s::jsonb)",
            (actor, str(review_id),
             _json({"guest_name": name, "rating": stars, "body": text})),
        )
    return review_id


def set_published(db: Database, review_id, *, published: bool, actor: str) -> str:
    """Show or hide one review. Hiding keeps it; only its visibility changes."""
    row = db.query_one(
        "SELECT review_id, guest_name FROM guest_reviews WHERE review_id = %s",
        (review_id,))
    if not row:
        raise ValidationError(f"no review found for {review_id}")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE guest_reviews SET published = %s WHERE review_id = %s",
            (published, row["review_id"]),
        )
        cur.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
            "VALUES (%s, %s, 'guest_review', %s, NULL, %s::jsonb)",
            (actor, "review_published" if published else "review_hidden",
             str(row["review_id"]), _json({"published": published})),
        )
    return row["guest_name"]


def published(db: Database, limit: int = 5) -> list[dict]:
    """The most recent published reviews, newest first."""
    return db.query_all(
        "SELECT review_id, guest_name, rating, body, stayed_on, created_at "
        "FROM guest_reviews WHERE published ORDER BY created_at DESC, review_id DESC "
        "LIMIT %s",
        (limit,))


def all_reviews(db: Database) -> list[dict]:
    """Every review, published or not, for the owner to manage."""
    return db.query_all(
        "SELECT review_id, guest_name, rating, body, published, created_at "
        "FROM guest_reviews ORDER BY created_at DESC, review_id DESC")


def count(db: Database) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM guest_reviews WHERE published")
    return int(row["n"]) if row else 0


def _stars(rating) -> str:
    if not rating:
        return ""
    return " " + "★" * int(rating) + "☆" * (5 - int(rating))


def render_for_guest(db: Database, limit: int = 5) -> str:
    """What a guest is told when they tap "Guest Reviews".

    Verbatim quotes, with the guest's name and their stars when we hold them.
    With none stored, the honest answer: we have none to show yet. The owner is
    offered nothing here, and no review is ever invented.
    """
    rows = published(db, limit=limit)
    if not rows:
        return ("We don't have any guest reviews to show yet. You are welcome to "
                "come and see the hostel, and the owner can tell you more.")
    lines = ["What guests say about Sagar PG Hostel:"]
    for row in rows:
        lines.append(f"\n- {row['guest_name']}{_stars(row['rating'])}: "
                     f"\"{row['body'].strip()}\"")
    return "\n".join(lines)


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)
