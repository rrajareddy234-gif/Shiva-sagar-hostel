#!/usr/bin/env python3
"""Manage the FAQ the bot answers without calling the AI.

    python scripts/faq_admin.py list
    python scripts/faq_admin.py misses
    python scripts/faq_admin.py add --question "What time is check-in?" \
        --answer "Check-in is from 11am." --alias "checkin time" --alias "when can i come"
    python scripts/faq_admin.py remove 4

Every entry added is one fewer paid AI call. `misses` lists the questions the
FAQ could not answer, most common first -- that is the worklist.

Do not store a price here. Rates come from the rate card through the pricing
engine, so a stored figure would go stale and contradict the chat.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import faq
from core.config import Settings, load_dotenv
from core.db import Database


def _db() -> Database:
    load_dotenv()
    return Database(Settings.from_env().database_url)


def cmd_list(args) -> int:
    rows = faq.all_active(_db())
    if not rows:
        print("The FAQ is empty. Add one with 'faq_admin.py add'.")
        return 0
    print(f"{len(rows)} active entries:\n")
    for row in rows:
        print(f"  [{row['faq_id']}] ({row['category']}) {row['question']}")
        print(f"        {row['answer']}")
        if row["aliases"]:
            print(f"        also asked as: {', '.join(row['aliases'])}")
    return 0


def cmd_misses(args) -> int:
    rows = faq.recent_misses(_db(), limit=args.limit)
    if not rows:
        print("No unanswered questions recorded yet. Either the FAQ is "
              "covering everything, or nobody has asked a new question.")
        return 0
    print(f"{len(rows)} questions the FAQ could not answer "
          f"(most common first):\n")
    for row in rows:
        print(f"  {row['times']:>3}x  {row['asked']}")
    print("\nAdd the good ones with 'faq_admin.py add'. Each one is a paid "
          "AI call you stop making.")
    return 0


def cmd_add(args) -> int:
    if not args.question or not args.answer:
        print("Both --question and --answer are required.", file=sys.stderr)
        return 2
    faq_id = faq.add(_db(), question=args.question, answer=args.answer,
                     category=args.category, aliases=args.alias or [],
                     priority=args.priority)
    print(f"Saved as entry {faq_id}. The bot will answer this without the AI.")
    return 0


def cmd_remove(args) -> int:
    if faq.deactivate(_db(), args.faq_id):
        print(f"Entry {args.faq_id} is now inactive.")
        return 0
    print(f"No active entry with id {args.faq_id}.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every active entry").set_defaults(func=cmd_list)

    misses = sub.add_parser("misses", help="questions the FAQ could not answer")
    misses.add_argument("--limit", type=int, default=20)
    misses.set_defaults(func=cmd_misses)

    add = sub.add_parser("add", help="store a question and its answer")
    add.add_argument("--question", required=True)
    add.add_argument("--answer", required=True)
    add.add_argument("--category", default="general")
    add.add_argument("--alias", action="append", help="repeatable")
    add.add_argument("--priority", type=int, default=0)
    add.set_defaults(func=cmd_add)

    remove = sub.add_parser("remove", help="stop using an entry")
    remove.add_argument("faq_id", type=int)
    remove.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
