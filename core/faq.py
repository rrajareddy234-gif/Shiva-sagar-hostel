"""Answer common questions from a stored FAQ, without an AI call.

The cheapest AI call is the one never made. Most guest messages are variations
of a few dozen questions the owner has answered many times already. Each answer
is stored once, with the phrasings guests actually use, and matched by trigram
similarity inside PostgreSQL -- no model to download, no API, no cost, and it
runs on the same machine as the database.

Matching is deliberately conservative. A confident wrong answer is worse than
an AI call, so anything below the threshold is handed to the AI as before and
the question is recorded. That record is the list the owner should turn into
new entries, so the system gets cheaper the longer it runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.db import Database

log = logging.getLogger(__name__)

# Calibrated against the starter set: real rewordings of a question score well
# above this, unrelated questions well below. See tests/test_faq.py.
DEFAULT_THRESHOLD = 0.55

# Below this many characters a trigram match is noise ("ok", "hi").
MIN_ASK_LENGTH = 3

# A one- or two-word question ("wifi?", "hot water") shares too few trigrams
# with a full sentence to clear the threshold, yet it is unambiguous. Such a
# short question is accepted on a whole-word match instead, and only against a
# word of at least this length, so a stray "the" or "is" cannot trigger an
# answer. A longer message never uses this path: it must earn its match on
# similarity like everything else.
MAX_KEYWORD_WORDS = 3
MIN_KEYWORD_LENGTH = 4


@dataclass(frozen=True)
class Match:
    faq_id: int
    question: str
    answer: str
    score: float


def _normalise(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def match(db: Database, text: str | None,
          *, threshold: float | None = None) -> Match | None:
    """The best stored answer for this question, or None if none is close enough.

    Similarity is measured against the canonical question and against every
    alias, and the best of those wins. Aliases exist because a trigram cannot
    know that "khana" and "meals" mean food.
    """
    asked = _normalise(text)
    if len(asked) < MIN_ASK_LENGTH:
        return None

    row = db.query_one(
        """
        SELECT f.faq_id, f.question, f.answer,
               GREATEST(
                   similarity(%(asked)s, f.question),
                   COALESCE((SELECT max(similarity(%(asked)s, a))
                             FROM unnest(f.aliases) AS a), 0)
               )::float AS score
        FROM faq f
        WHERE f.active
        ORDER BY score DESC, f.priority DESC, f.faq_id
        LIMIT 1
        """,
        {"asked": asked},
    )
    if not row:
        return None

    limit = DEFAULT_THRESHOLD if threshold is None else threshold
    if row["score"] < limit:
        # A short, unambiguous question ("wifi?", "hot water") may still be
        # answered, but only on a whole-word match against a stored word.
        return _keyword_match(db, asked)
    return Match(faq_id=row["faq_id"], question=row["question"],
                 answer=row["answer"], score=float(row["score"]))


def _keyword_match(db: Database, asked: str) -> Match | None:
    """Answer a very short question on a whole-word match, or give up.

    Only messages of a few words are eligible, and only words of four or more
    characters count, so a stray preposition cannot pull up an answer. The
    match is a whole word, never a substring: "wifi" matches "wifi", but "the"
    matches nothing.
    """
    words = [w.strip("?.,!") for w in asked.split()]
    words = [w for w in words if len(w) >= MIN_KEYWORD_LENGTH]
    if not words or len(asked.split()) > MAX_KEYWORD_WORDS:
        return None

    row = db.query_one(
        """
        SELECT f.faq_id, f.question, f.answer
        FROM faq f
        WHERE f.active
          AND EXISTS (
              SELECT 1 FROM unnest(
                  ARRAY[f.question] || f.aliases
              ) AS haystack
              WHERE EXISTS (
                  SELECT 1 FROM unnest(%(words)s::text[]) AS needle
                  WHERE haystack ~* ('\\m' || needle || '\\M')
              )
          )
        ORDER BY f.priority DESC, f.faq_id
        LIMIT 1
        """,
        {"words": words},
    )
    if not row:
        return None
    # A keyword hit is a weaker signal than a similarity hit; it is recorded
    # with a score below the threshold so the owner can see it was a fallback.
    return Match(faq_id=row["faq_id"], question=row["question"],
                 answer=row["answer"], score=0.0)


def answer(db: Database, text: str | None,
           *, threshold: float | None = None) -> str | None:
    """The stored answer, or None. Does not record anything."""
    found = match(db, text, threshold=threshold)
    return found.answer if found else None


def record(db: Database, asked: str | None, found: Match | None) -> None:
    """Remember what was asked and whether the FAQ could answer it.

    A miss is the useful row: it is a question the AI had to handle, which is
    exactly the one worth storing next. Recording must never break a reply.
    """
    text = (asked or "").strip()
    if not text:
        return
    try:
        db.execute(
            "INSERT INTO faq_hits (asked, faq_id, score, answered) "
            "VALUES (%s, %s, %s, %s)",
            (text, found.faq_id if found else None,
             found.score if found else None, found is not None),
        )
    except Exception as exc:
        log.warning("could not record an FAQ hit: %s", exc)


def answer_and_record(db: Database, text: str | None,
                      *, threshold: float | None = None) -> str | None:
    """Match, record either way, and return the answer when there is one."""
    found = match(db, text, threshold=threshold)
    record(db, text, found)
    return found.answer if found else None


# --- the owner's side ------------------------------------------------------

def add(db: Database, *, question: str, answer: str, category: str = "general",
        aliases: list[str] | None = None, priority: int = 0,
        source: str = "owner") -> int:
    """Store a question and its answer. Re-adding a question replaces it.

    The write goes through a transaction, not query_one: that helper closes its
    connection without committing, so a write through it is silently lost.
    """
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO faq (question, answer, category, aliases, priority, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (question) DO UPDATE
                SET answer = EXCLUDED.answer,
                    category = EXCLUDED.category,
                    aliases = EXCLUDED.aliases,
                    priority = EXCLUDED.priority,
                    updated_at = now()
            RETURNING faq_id
            """,
            (question.strip(), answer.strip(), category,
             list(aliases or []), priority, source),
        )
        return cur.fetchone()["faq_id"]


def deactivate(db: Database, faq_id: int) -> bool:
    """Stop using an entry without losing its history."""
    changed = db.execute(
        "UPDATE faq SET active = false, updated_at = now() WHERE faq_id = %s",
        (faq_id,))
    return bool(changed)


def all_active(db: Database) -> list[dict]:
    return db.query_all(
        "SELECT faq_id, question, answer, category, aliases, priority, source "
        "FROM faq WHERE active ORDER BY category, priority DESC, faq_id")


def recent_misses(db: Database, *, limit: int = 20) -> list[dict]:
    """Questions the FAQ could not answer, most common first."""
    return db.query_all(
        """
        SELECT asked, count(*) AS times
        FROM faq_hits
        WHERE answered = false
        GROUP BY asked
        ORDER BY times DESC, max(at) DESC
        LIMIT %s
        """,
        (limit,))
