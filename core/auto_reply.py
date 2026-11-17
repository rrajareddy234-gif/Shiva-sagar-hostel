"""The owner's switch for automatic WhatsApp replies.

The system sometimes needs to go quiet for a while -- a holiday, a problem, a
run of junk messages -- without taking the webhook down. This module owns that
switch and the plain-English help the owner reads back.

Where the state lives, and why
-----------------------------
There are two sources, and the rule between them is deliberate:

* `AUTO_REPLY_ENABLED` in `.env` is the **default** and the **safety net**. If
  nothing has been chosen in the app, or the database cannot be read, the
  `.env` value decides. A fresh deployment therefore behaves exactly as before.
* The `settings` table holds the owner's **live choice**, written by the
  dashboard button or the WhatsApp command. It survives a restart because it is
  in the database, and it needs no file editing or SSH access.

So: if a row exists, it wins. If not, the `.env` default applies. The owner
never has to understand this; the commands and the button just work.

Nothing here sends a message. It returns text for the caller to send, exactly
like `core.commands`, so the dispatcher stays the only thing that touches the
channel.
"""

from __future__ import annotations

import json
import logging

from core.db import Database

log = logging.getLogger(__name__)

# The one row that holds the owner's live choice. Same table the advance amount
# and the AI alert state use, so there is nothing new to migrate.
KEY = "auto_reply_enabled"


def _env_default() -> bool:
    """The `.env` default, read the same way `Settings` reads it.

    Used only when the caller does not pass one. Reading it here keeps the
    fallback identical to the value `Settings.from_env()` would produce, because
    both read the same environment variable.
    """
    from core.config import _env_bool
    return _env_bool("AUTO_REPLY_ENABLED", True)


def is_paused(db: Database, *, default_enabled: bool | None = None) -> bool:
    """True when automatic replies are switched off.

    Falls back to the `.env` default (or an explicit `default_enabled`) when no
    choice has been stored or the database cannot be read, so a database hiccup
    never silently changes the behaviour. A pause is explicit; an outage is not
    a reason to stop answering.
    """
    if default_enabled is None:
        default_enabled = _env_default()
    try:
        row = db.query_one("SELECT value FROM settings WHERE key = %s", (KEY,))
    except Exception as exc:  # pragma: no cover - depends on a live database
        log.warning("could not read the auto-reply setting; using the default: %s", exc)
        return not default_enabled
    if not row:
        return not default_enabled
    return not _value_to_bool(row["value"], default=default_enabled)


def _value_to_bool(value, *, default: bool) -> bool:
    """Read the stored value, which may be a bare bool or a {"enabled": ...}."""
    if isinstance(value, dict):
        value = value.get("enabled", value.get("value", default))
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


def _audit(cur, actor: str, action: str, entity: str, entity_id: str,
           before=None, after=None) -> None:
    """Same shape as the audit helper in `core.booking` and `core.billing`."""
    cur.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (actor, action, entity, entity_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def set_paused(db: Database, paused: bool, *, actor: str) -> bool:
    """Turn automatic replies off (paused=True) or on (paused=False).

    Returns the new paused state. Every change is written to the audit log with
    who did it, so the owner can always see when a pause started and who set it.
    """
    paused = bool(paused)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (KEY, json.dumps({"enabled": not paused})),
        )
        _audit(cur, actor, "auto_reply_paused" if paused else "auto_reply_resumed",
               "settings", KEY, None, {"paused": paused})
    log.warning("automatic replies %s by %s", "PAUSED" if paused else "RESUMED", actor)
    return paused


def status_line(db: Database, *, default_enabled: bool | None = None) -> str:
    """One line saying whether replies are going out, for a health check or a reply."""
    if is_paused(db, default_enabled=default_enabled):
        return "PAUSED - guests are not being answered automatically."
    return "ON - guests are answered automatically."


# --- the owner's cheat sheet -------------------------------------------------
#
# Short and long versions of the same thing, so the owner can read the two
# lines they use daily, or the full list when they have forgotten something.
# Kept here, next to the switch, so the help and the behaviour cannot drift.

PAUSE = "pause replies"
RESUME = "resume replies"

SHORT_HELP = (
    "Owner controls (reply with any of these):\n"
    "- 'pause replies'  - stop automatic replies to guests\n"
    "- 'resume replies' - start them again\n"
    "- 'auto reply status' - is it on or paused?\n"
    "- 'owner help' - the full list"
)

LONG_HELP = (
    "Owner commands\n"
    "\n"
    "Everyday:\n"
    "- pause replies          stop automatic replies to guests (messages are "
    "still saved, bookings still work, you are still told)\n"
    "- resume replies         start automatic replies again\n"
    "- auto reply status      is it on or paused?\n"
    "- how many vacancies today\n"
    "- show pending booking requests\n"
    "- approve booking <reference>   (then reply YES)\n"
    "- reject booking <reference>    (then reply YES)\n"
    "- cancel booking <reference>\n"
    "- check in <reference>\n"
    "- check out <reference>\n"
    "- bed <bed id> is ready\n"
    "- record payment <amount> by <method> for <reference>\n"
    "- verify payment <reference>\n"
    "- export customers | export bookings | export payments\n"
    "- what is the AI cost this month\n"
    "\n"
    "Tip: you can also ask in your own words, e.g. 'stop replying to guests "
    "for now' or 'are we answering guests?'. The assistant understands both."
)
