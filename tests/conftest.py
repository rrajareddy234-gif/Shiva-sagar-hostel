"""Shared test fixtures.

Tests run against a real PostgreSQL database. A throwaway database is created
per session, the schema is applied, and each test gets a clean slate. Dummy data
only — never real customer records.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from core.channel.adapter import ChannelAdapter, SendResult
from core.db import Database

ADMIN_DSN = os.environ.get(
    "TEST_ADMIN_DSN", "postgresql://sagar:devpass@localhost:5432/sagarpg"
)
TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "sagarpg_test")
TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", f"postgresql://sagar:devpass@localhost:5432/{TEST_DB_NAME}"
)

IST = timezone(timedelta(hours=5, minutes=30))

# Every table is read from the schema, not typed out here. A hand-kept list
# silently goes stale the moment a table is added, and a table missing from the
# truncate leaks rows from one test into the next. Deriving the list makes that
# class of bug impossible.
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def _tables_from_schema() -> tuple[str, ...]:
    sql = _SCHEMA_PATH.read_text()
    names = re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_][a-z0-9_]*)", sql)
    if not names:
        raise RuntimeError(f"no tables found in {_SCHEMA_PATH}; the schema moved?")
    return tuple(sorted(set(names)))


TABLES = _tables_from_schema()


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Create a fresh test database once per session."""
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL is not available: {exc}")

    with admin, admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (TEST_DB_NAME,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    admin.close()

    Database(TEST_DSN).apply_schema()
    yield TEST_DSN


@pytest.fixture
def db(_database) -> Database:
    database = Database(TEST_DSN)
    # Truncate before each test so tests are independent.
    with database.transaction() as cur:
        cur.execute(
            "TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
        )
    return database


@pytest.fixture
def seeded(db: Database) -> Database:
    """A small, representative hostel: AC, non-AC and a dormitory."""
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO rooms (room_id, room_type, total_beds) VALUES "
            "('1NA','NON_AC',2), ('2AC','AC',2), ('6NA','NON_AC',3), ('DORM','DORM',3)"
        )
        cur.execute(
            "INSERT INTO beds (bed_id, room_id, bed_type) VALUES "
            "('1NA-B1','1NA','NON_AC'), ('1NA-B2','1NA','NON_AC'), "
            "('2AC-B1','2AC','AC'), ('2AC-B2','2AC','AC'), "
            "('6NA-B1','6NA','NON_AC'), ('6NA-B2','6NA','NON_AC'), ('6NA-B3','6NA','NON_AC'), "
            "('DORM-M1','DORM','MAT'), ('DORM-M2','DORM','MAT'), ('DORM-M3','DORM','MAT')"
        )
        cur.execute(
            "INSERT INTO rate_plans (name, room_type, food_included, stay_mode, daily_rate, hourly_rate, min_hours) VALUES "
            "('Non-AC with food per night','NON_AC',true,'NIGHT',250,20,1), "
            "('Non-AC with food per day','NON_AC',true,'DAY',350,20,1), "
            "('Non-AC without food per night','NON_AC',false,'NIGHT',200,20,1), "
            "('Non-AC without food per day','NON_AC',false,'DAY',250,20,1), "
            "('AC with food per night','AC',true,'NIGHT',350,30,1), "
            "('AC with food per day','AC',true,'DAY',450,30,1), "
            "('AC without food per night','AC',false,'NIGHT',300,30,1), "
            "('AC without food per day','AC',false,'DAY',350,30,1), "
            "('Dormitory with food per night','DORM',true,'NIGHT',200,10,1), "
            "('Dormitory with food per day','DORM',true,'DAY',300,10,1), "
            "('Dormitory mat per night','DORM',false,'NIGHT',150,10,1), "
            "('Dormitory mat per day','DORM',false,'DAY',200,10,1)"
        )
        cur.execute(
            "INSERT INTO monthly_plans (name, room_type, food_included, plan_kind, stay_mode, monthly_rate) VALUES "
            "('Monthly AC with food, night','AC',true,'STANDARD','NIGHT',8000), "
            "('Monthly AC with food, full day','AC',true,'STANDARD','DAY',11000), "
            "('Monthly AC no food, night','AC',false,'STANDARD','NIGHT',5000), "
            "('Monthly AC no food, full day','AC',false,'STANDARD','DAY',7000), "
            "('Monthly Non-AC with food','NON_AC',true,'STANDARD','NIGHT',5500), "
            "('Monthly Non-AC with food, full day','NON_AC',true,'STANDARD','DAY',5500), "
            "('Monthly Non-AC no food','NON_AC',false,'STANDARD','NIGHT',4000), "
            "('Monthly Non-AC no food, full day','NON_AC',false,'STANDARD','DAY',4000), "
            "('Monthly dormitory with food','DORM',true,'STANDARD','NIGHT',4500), "
            "('Monthly dormitory with food, full day','DORM',true,'STANDARD','DAY',4500), "
            "('Monthly dormitory no food','DORM',false,'STANDARD','NIGHT',3500), "
            "('Monthly dormitory no food, full day','DORM',false,'STANDARD','DAY',3500)"
        )
        cur.execute(
            "INSERT INTO extras (name, price, unit) VALUES "
            "('Towel',50,'PER_ITEM'), "
            "('Bedsheet',50,'PER_ITEM'), "
            "('Pillow',50,'PER_ITEM'), "
            "('Locker',50,'PER_MONTH')"
        )
        cur.execute(
            "INSERT INTO hostel_facts (key, value, category, source) VALUES "
            "('policy.night_meals','A night stay includes dinner only.','policy','owner'), "
            "('policy.day_meals','A day stay is 24 hours and includes tiffin, lunch and dinner.','policy','owner'), "
            "('policy.timings','We are open 24/7.','policy','owner') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        cur.execute(
            "INSERT INTO services (service_code, name, price, duration_minutes, needs_bed) VALUES "
            "('FRESH_UP','Fresh-up',50,0,false), "
            "('REST_3H','Three-hour rest',100,180,true) "
            "ON CONFLICT (service_code) DO NOTHING"
        )
    return db


@pytest.fixture
def tz():
    return IST


@pytest.fixture
def tomorrow(tz):
    base = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
    return base + timedelta(days=1)


# --- webhook test fixtures, shared by tests/test_webhook.py and test_jobs.py --

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
# The dashboard has no fixed default password, so tests set one explicitly.
DASHBOARD_PASSWORD = "test-dashboard-password"


class FakeChannel(ChannelAdapter):
    """A channel that records sends instead of calling Meta."""

    name = "fake"

    def __init__(self, *, fail: bool = False, fail_interactive: bool = False,
                 fail_document: bool = False, fail_flow: bool = False,
                 throttle: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, object]] = []
        self.documents: list[tuple[str, str, bytes]] = []
        self.flows: list[tuple[str, object]] = []
        self.fail = fail
        self.fail_interactive = fail_interactive
        self.fail_document = fail_document
        self.fail_flow = fail_flow
        self.throttle = throttle
        # Every outbound attempt, whether or not it succeeded. A test that only
        # looks at successes cannot tell "did not try" from "tried and failed",
        # and the whole point of the throttle guard is not to try twice.
        self.attempts: list[str] = []
        # Plain-text sends, by recipient. Kept separate from `sent` (which also
        # holds interactive bodies) so a test can assert that a guest received no
        # bare text message at all -- the interactive-only contract.
        self.texts: list[tuple[str, str]] = []

    def _meta(self):
        from core.channel.meta_cloud import MetaCloudAdapter
        return MetaCloudAdapter(app_secret=APP_SECRET, access_token="t",
                                phone_number_id="1", verify_token=VERIFY_TOKEN)

    def verify_inbound(self, raw_body, headers):
        return self._meta().verify_inbound(raw_body, headers)

    def parse_inbound(self, raw_body):
        return self._meta().parse_inbound(raw_body)

    def parse_statuses(self, raw_body):
        return self._meta().parse_statuses(raw_body)

    def verify_challenge(self, query):
        return self._meta().verify_challenge(query)

    def send_text(self, to_phone, text):
        self.attempts.append("text")
        if self.throttle:
            return SendResult(ok=False, retryable=False, certainty="rejected",
                              error_code="131056", http_status=400,
                              error="HTTP 400: (#131056) pair rate limit hit")
        if self.fail:
            return SendResult(ok=False, error="simulated outage")
        self.texts.append((to_phone, text))
        self.sent.append((to_phone, text))
        return SendResult(ok=True, provider_message_id=f"out.{len(self.sent)}")

    def send_template(self, to_phone, template_name, params=None, language="en"):
        return SendResult(ok=True, provider_message_id="tpl.1")

    def send_interactive(self, to_phone, prompt):
        self.attempts.append("interactive")
        if self.throttle:
            return SendResult(ok=False, retryable=False, certainty="rejected",
                              error_code="131056", http_status=400,
                              error="HTTP 400: (#131056) pair rate limit hit")
        if self.fail:
            # An outage is transient: the same interactive is retried later, so
            # no plain-text fallback and no second message.
            return SendResult(ok=False, error="simulated outage")
        if self.fail_interactive:
            # A rejected interactive is a permanent 4xx from Meta (malformed or
            # disallowed), so it is not retried and never replaced by text.
            return SendResult(ok=False, retryable=False, certainty="rejected",
                              error_code="131009", http_status=400,
                              error="simulated interactive rejection")
        # An interactive message IS the guest's reply, not a decoration around a
        # separate text message, so its body is recorded as a guest-facing send.
        self.prompts.append((to_phone, prompt))
        self.sent.append((to_phone, prompt.body))
        return SendResult(ok=True, provider_message_id=f"btn.{len(self.prompts)}")

    def send_flow(self, to_phone, flow):
        self.attempts.append("flow")
        if self.throttle:
            return SendResult(ok=False, retryable=False, certainty="rejected",
                              error_code="131056", http_status=400,
                              error="HTTP 400: (#131056) pair rate limit hit")
        if self.fail or self.fail_flow:
            return SendResult(ok=False, retryable=False, certainty="rejected",
                              error_code="131009", http_status=400,
                              error="simulated flow rejection")
        self.flows.append((to_phone, flow))
        return SendResult(ok=True, provider_message_id=f"flow.{len(self.flows)}")

    def send_document(self, to_phone, filename, content, media_type="text/csv",
                      caption=None):
        if self.fail or self.fail_document:
            return SendResult(ok=False, error="simulated document failure")
        self.documents.append((to_phone, filename, content))
        return SendResult(ok=True, provider_message_id=f"doc.{len(self.documents)}")

    # What a fetch_media call returns for the next inbound image. Tests set this
    # to stand in for a real WhatsApp download.
    media_bytes: bytes | None = b"\xff\xd8\xff\xe0fake-jpeg"

    def fetch_media(self, media_id):
        if self.media_bytes is None:
            return None
        return self.media_bytes, "image/jpeg"


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def client(db, channel, seeded, monkeypatch, tmp_path):
    """A TestClient against the real database with a fake WhatsApp channel."""
    from fastapi.testclient import TestClient

    from api.webhook import create_app
    from core.config import Settings

    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD)
    settings = Settings(
        database_url=TEST_DSN,
        channel_provider="meta_cloud",
        owner_phone="+919999999999",
        timezone="Asia/Kolkata",
        meta_app_secret=APP_SECRET,
        meta_access_token="test-token",
        meta_phone_number_id="123456789",
        meta_verify_token=VERIFY_TOKEN,
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
        ai_api_key=None,
        ai_base_url=None,
        payment_proof_dir=str(tmp_path / "payment-proofs"),
        # Tests exercise the real delivery path against a FakeChannel, so the
        # master send switch is turned on here explicitly. Production leaves it
        # off by default; the gate's own tests cover the off case.
        send_enabled=True,
    )
    return TestClient(create_app(settings, channel=channel, ai=None))


def flush_outbox(client, *, limit: int = 100) -> dict:
    """Drive the isolated sender explicitly in tests that assert provider output."""
    from core.outbound_worker import run_batch
    app = client.app
    return run_batch(app.state.db, app.state.channel, app.state.settings, limit=limit)
