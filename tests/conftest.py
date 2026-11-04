"""Shared test fixtures.

Tests run against a real PostgreSQL database. A throwaway database is created
per session, the schema is applied, and each test gets a clean slate. Dummy data
only — never real customer records.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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

TABLES = (
    "audit_log", "referrals", "partners", "messages", "conversations",
    "payments", "bed_holds", "bookings", "rate_plans", "monthly_plans", "extras",
    "enquiries", "guests", "beds", "rooms", "settings",
)


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Create a fresh test database once per session."""
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL is not available: {exc}")

    with admin:
        with admin.cursor() as cur:
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
            "('New bedsheet',50,'PER_ITEM'), "
            "('New towel',100,'PER_ITEM'), "
            "('New pillow',50,'PER_ITEM'), "
            "('Locker',30,'PER_MONTH')"
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


class FakeChannel(ChannelAdapter):
    """A channel that records sends instead of calling Meta."""

    name = "fake"

    def __init__(self, *, fail: bool = False, fail_interactive: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, object]] = []
        self.fail = fail
        self.fail_interactive = fail_interactive

    def _meta(self):
        from core.channel.meta_cloud import MetaCloudAdapter
        return MetaCloudAdapter(app_secret=APP_SECRET, access_token="t",
                                phone_number_id="1", verify_token=VERIFY_TOKEN)

    def verify_inbound(self, raw_body, headers):
        return self._meta().verify_inbound(raw_body, headers)

    def parse_inbound(self, raw_body):
        return self._meta().parse_inbound(raw_body)

    def verify_challenge(self, query):
        return self._meta().verify_challenge(query)

    def send_text(self, to_phone, text):
        if self.fail:
            return SendResult(ok=False, error="simulated outage")
        self.sent.append((to_phone, text))
        return SendResult(ok=True, provider_message_id=f"out.{len(self.sent)}")

    def send_template(self, to_phone, template_name, params=None, language="en"):
        return SendResult(ok=True, provider_message_id="tpl.1")

    def send_interactive(self, to_phone, prompt):
        if self.fail or self.fail_interactive:
            return SendResult(ok=False, error="simulated interactive failure")
        self.prompts.append((to_phone, prompt))
        return SendResult(ok=True, provider_message_id=f"btn.{len(self.prompts)}")


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def client(db, channel, seeded):
    """A TestClient against the real database with a fake WhatsApp channel."""
    from fastapi.testclient import TestClient

    from api.webhook import create_app
    from core.config import Settings

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
    )
    return TestClient(create_app(settings, channel=channel, ai=None))
