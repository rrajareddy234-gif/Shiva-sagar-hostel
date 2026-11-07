"""Hostel facts: the things only the owner knows, in the owner's own words.

The address, the amenities, the timings. The assistant reads these and repeats
them; it never invents them. A fact here is authoritative and is quoted
verbatim, which is why it is kept apart from `faq`: an FAQ entry is matched by
similarity, a fact is looked up by key.

Two rules hold.

  * A fact is never a price. Prices come from the rate card and nowhere else.
    Storing a figure here would let a stale number be quoted as truth.
  * A fact is never a guess. If a key is absent the caller is told so, rather
    than being handed a plausible-sounding sentence.

Every fact records its source, so the owner can tell at a glance which lines
they confirmed and which came from the seed file.
"""

from __future__ import annotations

import logging

from core.db import Database
from core.errors import ValidationError

log = logging.getLogger(__name__)

# Keys the assistant leans on. Kept here so a typo is caught by the tests.
KEY_ADDRESS = "address.full"
KEY_NAME = "hostel.name"

# What a night and a day include. Stored as facts, not written into code, so the
# owner can reword them without a release. A "night" is dinner only; a "day" is
# tiffin, lunch and dinner over 24 hours.
KEY_NIGHT_MEALS = "policy.night_meals"
KEY_DAY_MEALS = "policy.day_meals"
KEY_TIMINGS = "policy.timings"

VALID_SOURCES = ("owner", "seed")
VALID_CATEGORIES = ("identity", "location", "amenity", "policy", "general")

# A fact is a short, single statement. A wall of text is a sign the owner meant
# to write an FAQ entry instead, and it would crowd out the rest of the facts.
MAX_VALUE_LENGTH = 500


def get(db: Database, key: str) -> str | None:
    """The value of one fact, or None if the owner has not recorded it."""
    row = db.query_one("SELECT value FROM hostel_facts WHERE key = %s", (key,))
    return row["value"] if row else None


def by_category(db: Database, category: str) -> list[dict]:
    return db.query_all(
        "SELECT key, value, category, source, verified, updated_at "
        "FROM hostel_facts WHERE category = %s ORDER BY key",
        (category,))


def all_facts(db: Database) -> list[dict]:
    return db.query_all(
        "SELECT key, value, category, source, verified FROM hostel_facts "
        "ORDER BY category, key")


def set_fact(db: Database, *, key: str, value: str, actor: str,
             category: str = "general", source: str = "owner") -> str:
    """Record or update a fact. Changing one is audited."""
    key = (key or "").strip()
    value = (value or "").strip()
    if not key:
        raise ValidationError("a fact needs a key")
    if not value:
        raise ValidationError("a fact needs a value")
    if len(value) > MAX_VALUE_LENGTH:
        raise ValidationError(
            f"a fact must be at most {MAX_VALUE_LENGTH} characters; "
            "a longer answer belongs in the FAQ")
    if category not in VALID_CATEGORIES:
        raise ValidationError(f"unknown fact category {category!r}")
    if source not in VALID_SOURCES:
        raise ValidationError(f"unknown fact source {source!r}")

    before = get(db, key)
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO hostel_facts (key, value, category, source, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, category = EXCLUDED.category,
                source = EXCLUDED.source, updated_at = now()
            """,
            (key, value, category, source),
        )
        cur.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
            "VALUES (%s, 'fact_set', 'hostel_fact', %s, %s::jsonb, %s::jsonb)",
            (actor, key,
             _json({"value": before}) if before is not None else None,
             _json({"value": value, "category": category})),
        )
    return value


def address_block(db: Database) -> str | None:
    """The full address as one line, or None when it is not recorded."""
    full = get(db, KEY_ADDRESS)
    if not full:
        return None
    return full


def map_link(db: Database) -> str | None:
    """The owner's Google Maps link, or None when it is not recorded.

    The link is the owner's own share link, so it always opens the right pin. It
    is never built from coordinates here: a pin guessed from an address can send
    a guest to the wrong street.
    """
    return get(db, "address.map_link")


def amenities_block(db: Database) -> list[str]:
    """Every amenity the owner has recorded, as plain sentences."""
    return [row["value"] for row in by_category(db, "amenity")]


def render_for_prompt(db: Database) -> str:
    """Every fact, rendered for the assistant's system prompt.

    Only verified facts are given to the model, and the block is clearly marked
    as fact so the model repeats it rather than paraphrasing it into something
    new. An empty database yields an empty block, never invented text.
    """
    rows = db.query_all(
        "SELECT key, value FROM hostel_facts WHERE verified ORDER BY category, key")
    if not rows:
        return ""
    lines = ["FACTS ABOUT THE HOSTEL (recorded by the owner; repeat these as given, "
             "never change a figure or add a detail that is not here):"]
    for row in rows:
        lines.append(f"- {row['key']}: {row['value']}")
    return "\n".join(lines)


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)
