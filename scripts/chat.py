"""Talk to the hostel assistant from the terminal, without WhatsApp.

This drives the real code path: the same router, the same pricing engine, the
same booking flow the live webhook uses. The only thing faked is the transport —
replies are printed instead of sent to Meta.

It runs against a scratch database, so nothing here can touch real guests or
real bookings. By default that database is created fresh from `db/schema.sql`
and `db/seed.sql`, so it starts with the real rooms and the real rate card.

    python scripts/chat.py                      # seeded scratch database
    python scripts/chat.py --keep               # keep the database after exit
    python scripts/chat.py --phone +919876543210

In the session:
    /who          show which phone number you are sending as
    /owner        switch to the owner's number (owner commands only work here)
    /as +91...    switch to any phone number
    /reset        wipe the scratch database and start over
    /quit         leave

A line that starts with `[` is a tap: `[AC]` is sent as the button the guest
would have pressed, not as text.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from core.channel.adapter import (  # noqa: E402
    ChannelAdapter,
    InboundMessage,
    Prompt,
    SendResult,
)
from core.config import Settings, load_dotenv  # noqa: E402
from core.db import Database  # noqa: E402
from core.dispatcher import build_ai, dispatch  # noqa: E402
from core.inbox import store_inbound  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = "sagarpg_demo"


class ConsoleChannel(ChannelAdapter):
    """A channel that prints replies instead of sending them."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        # Outbound ids must be unique across the whole session, not per send
        # kind, because the id is the primary key of the recorded message.
        self.out_seq = 0
        # title -> id of the most recent prompt. WhatsApp sends the id back when
        # a guest taps, so the simulator must too; sending the label would test
        # a path the live app never takes.
        self.last_options: dict[str, str] = {}

    def _next_out_id(self, kind: str) -> str:
        self.out_seq += 1
        return f"{kind}.{self.out_seq}"

    def verify_inbound(self, raw_body: bytes, headers: dict) -> bool:
        return True

    def parse_inbound(self, raw_body: bytes) -> list[InboundMessage]:
        return []

    def send_text(self, to_phone: str, text: str) -> SendResult:
        self.sent.append((to_phone, text))
        _print_reply(text)
        return SendResult(ok=True, provider_message_id=self._next_out_id("out"))

    def send_template(self, to_phone, template_name, params=None, language="en"):
        _print_reply(f"(template: {template_name})")
        return SendResult(ok=True, provider_message_id=self._next_out_id("tpl"))

    def send_interactive(self, to_phone: str, prompt: Prompt) -> SendResult:
        # Show the body, then the tappable answers as the guest would see them.
        _print_reply(prompt.body)
        options = prompt.rows or prompt.buttons
        if options:
            self.last_options.update({o.title: o.id for o in options})
            line = "  ".join(f"[{o.title}]" for o in options)
            print(f"   {_dim(line)}")
            print(f"   {_dim('(tap: type the bracketed label, e.g. ' + '[' + options[0].title + ']' + ')')}")
        return SendResult(ok=True, provider_message_id=self._next_out_id("btn"))

    def send_document(self, to_phone, filename, content, media_type="text/csv",
                      caption=None):
        _print_reply(caption or f"(file: {filename})")
        print(f"   {_dim(f'[{len(content)} bytes of {media_type} attached]')}")
        return SendResult(ok=True, provider_message_id=self._next_out_id("doc"))


def _dim(text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[2m{text}\033[0m"


def _print_reply(text: str) -> None:
    print()
    for line in text.splitlines() or [""]:
        print(f"  \033[36m{line}\033[0m" if sys.stdout.isatty() else f"  {line}")
    print()


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "sagar")
    password = os.environ.get("POSTGRES_PASSWORD", "devpass")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{DEMO_DB}"


def _admin_dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "sagar")
    password = os.environ.get("POSTGRES_PASSWORD", "devpass")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    maintenance = os.environ.get("POSTGRES_DB", "sagarpg")
    return f"postgresql://{user}:{password}@{host}:{port}/{maintenance}"


def build_scratch_database() -> str:
    """Create a fresh, seeded scratch database. Never touches the real one."""
    dsn = _dsn()
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (DEMO_DB,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{DEMO_DB}"')
            cur.execute(f'CREATE DATABASE "{DEMO_DB}"')

    db = Database(dsn)
    db.apply_schema()
    seed = (ROOT / "db" / "seed.sql").read_text()
    with db.transaction() as cur:
        cur.execute(seed)
    return dsn


def reset_scratch_database() -> str:
    """Empty the scratch database but keep its schema and the hostel's setup."""
    db = Database(_dsn())
    with db.transaction() as cur:
        for table in ("booking_extras", "payments", "invoice_lines", "invoices",
                      "bed_holds", "bookings", "guests", "messages",
                      "conversations", "enquiries", "audit_log", "ai_usage"):
            cur.execute(f"DELETE FROM {table}")
        cur.execute("UPDATE beds SET status = 'AVAILABLE' WHERE status <> 'AVAILABLE'")
    return _dsn()


def _make_settings(dsn: str, owner_phone: str | None) -> Settings:
    base = Settings.from_env()
    return Settings(
        database_url=dsn,
        channel_provider="console",
        owner_phone=owner_phone or base.owner_phone,
        timezone=base.timezone,
        meta_app_secret=None, meta_access_token=None,
        meta_phone_number_id=None, meta_verify_token=None,
        ai_provider=base.ai_provider, ai_model=base.ai_model,
        ai_api_key=base.ai_api_key, ai_base_url=base.ai_base_url,
        ai_timeout=base.ai_timeout, staff_phones=base.staff_phones,
    )


def _parse_tap(text: str) -> str | None:
    """`[AC]` means the guest tapped a button labelled AC, not typed 'AC'."""
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", default="+919000000001",
                        help="the guest's number (default +919000000001)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the scratch database after exit")
    parser.add_argument("--reuse", action="store_true",
                        help="reuse the existing scratch database instead of rebuilding")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set; copy .env.example to .env first.", file=sys.stderr)
        return 2

    try:
        dsn = _dsn() if args.reuse else build_scratch_database()
    except psycopg.OperationalError as exc:
        print(f"cannot reach PostgreSQL: {exc}", file=sys.stderr)
        print("Is the container running?  sudo docker start sagarpg-pg", file=sys.stderr)
        return 2

    settings = _make_settings(dsn, None)
    db = Database(dsn)
    channel = ConsoleChannel()
    ai = build_ai(settings)

    phone = args.phone
    owner = settings.owner_phone

    print()
    print(f"  Sagar PG Hostel — chat simulator   {_dim('(scratch database, not real data)')}")
    print(f"  You are: {phone}" + (f"  {_dim('(this is the owner number)')}" if phone == owner else ""))
    print(f"  AI: {'connected' if ai else 'off (answers come from the router and the database)'}")
    print(f"  {_dim('/who  /owner  /as <phone>  /reset  /quit')}")
    print()
    print("  Try: hi   |   what are your prices   |   do you have a room free")
    print("       I want to book from 1 December for 2 nights")
    print()

    counter = 0
    while True:
        try:
            line = input("\033[1mguest> \033[0m" if sys.stdout.isatty() else "guest> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = line.strip()
        if not text:
            continue

        if text in ("/quit", "/exit"):
            break
        if text == "/who":
            print(f"  sending as {phone}")
            continue
        if text == "/owner":
            if not owner:
                print("  OWNER_PHONE is not set in .env, so there is no owner number.")
                continue
            phone = owner
            print(f"  now sending as the owner: {phone}")
            continue
        if text.startswith("/as "):
            phone = text[4:].strip()
            print(f"  now sending as {phone}")
            continue
        if text == "/reset":
            dsn = reset_scratch_database()
            db = Database(dsn)
            print("  scratch database cleared; beds free again")
            continue

        counter += 1
        tap_label = _parse_tap(text)
        button_id = None
        send_text = text
        if tap_label is not None:
            # WhatsApp delivers the button's id, never its label. Map back so
            # the simulator exercises the same path as a real tap. The visible
            # text that accompanies a tap is the label the guest saw.
            button_id = channel.last_options.get(tap_label, tap_label)
            send_text = tap_label
        message = InboundMessage(
            provider_message_id=f"sim.{counter}",
            from_phone=phone,
            text=send_text,
            message_type="interactive" if button_id else "text",
            button_id=button_id,
            profile_name="Test Guest",
        )

        stored = store_inbound(db, message)
        if stored is None:
            print("  (duplicate ignored)")
            continue

        try:
            dispatch(db, channel, settings, message, ai, stored.message_id)
        except Exception as exc:  # pragma: no cover - a real failure to show
            print(f"  \033[31mERROR: {type(exc).__name__}: {exc}\033[0m")
            import traceback
            traceback.print_exc()

    if not args.keep and not args.reuse:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (DEMO_DB,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{DEMO_DB}"')
        print("  scratch database dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
