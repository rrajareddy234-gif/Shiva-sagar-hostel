"""Clear demo data, so only real customers remain.

This deletes people and money, never the hostel's own setup. It keeps rooms,
beds, rate plans, extras and settings — the things the owner configured — and
the conversations and messages already received, which are a record of what real
guests sent.

Run it deliberately:

    python scripts/reset_demo_data.py --yes

Without `--yes` it only prints what it would delete. Take a backup first:
    docker exec -e PGPASSWORD=... sagarpg-pg pg_dump -U sagar -d sagarpg > backup.sql
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import Database  # noqa: E402

# People and money. Order matters: children before the guests they point at.
DEMO_TABLES = (
    "booking_extras", "payments", "invoice_lines", "invoices", "bed_holds",
    "bookings", "referrals", "partners", "guests",
    "audit_log", "ai_usage",
)

# The owner's own setup, plus the record of real inbound contact. Never touched.
# An enquiry is created from a real WhatsApp message, so it is kept alongside
# the conversation and messages that produced it.
KEEP_TABLES = ("rooms", "beds", "rate_plans", "monthly_plans", "extras",
               "settings", "conversations", "messages", "enquiries")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="actually delete; without it, only report")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"),
                        help="database to reset (defaults to $DATABASE_URL)")
    args = parser.parse_args()
    if not args.dsn:
        print("DATABASE_URL is not set and --dsn was not given", file=sys.stderr)
        return 2

    db = Database(args.dsn)
    print("Will delete, and keep:")
    for table in DEMO_TABLES:
        print(f"  delete  {table:16} {db.query_one(f'SELECT count(*) AS n FROM {table}')['n']:>5} row(s)")
    for table in KEEP_TABLES:
        print(f"  keep    {table:16} {db.query_one(f'SELECT count(*) AS n FROM {table}')['n']:>5} row(s)")

    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to proceed.")
        return 0

    with db.transaction() as cur:
        for table in DEMO_TABLES:
            cur.execute(f"DELETE FROM {table}")
        # Every bed starts free again; a guest who left does not leave it OCCUPIED.
        cur.execute("UPDATE beds SET status = 'AVAILABLE' WHERE status <> 'AVAILABLE'")
        # The next real booking should read SPG-<year>-000001.
        cur.execute("ALTER SEQUENCE booking_ref_seq RESTART WITH 1")

    print("\nDemo data cleared. Beds set free, reference counter restarted at 1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
