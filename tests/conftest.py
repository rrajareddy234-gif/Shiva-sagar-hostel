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
    "payments", "bed_holds", "bookings", "rate_plans", "enquiries",
    "guests", "beds", "rooms", "settings",
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
            "INSERT INTO rate_plans (name, room_type, food_included, daily_rate, hourly_rate, min_hours) VALUES "
            "('Non-AC with food','NON_AC',true,350,50,1), "
            "('Non-AC without food','NON_AC',false,300,45,1), "
            "('AC with food','AC',true,450,60,1), "
            "('AC without food','AC',false,400,55,1), "
            "('Dormitory mat','DORM',false,200,30,1)"
        )
    return db


@pytest.fixture
def tz():
    return IST


@pytest.fixture
def tomorrow(tz):
    base = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
    return base + timedelta(days=1)
