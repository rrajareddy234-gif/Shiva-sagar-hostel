"""PostgreSQL access helpers.

Thin wrappers over psycopg 3. The point is that every write that can conflict
runs inside a transaction, and constraint violations are translated into domain
errors the caller can reason about.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from core.errors import BedUnavailable, BookingConflict, PaymentError, ValidationError

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextlib.contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        """Run a block in a transaction, translating constraint violations.

        Commits on success, rolls back on any exception.
        """
        with self.connect() as conn:
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise _translate(exc) from exc

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.transaction() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def apply_schema(self) -> None:
        """Apply db/schema.sql. Written to be re-runnable."""
        sql = SCHEMA_PATH.read_text()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()


def _translate(exc: Exception) -> Exception:
    """Map database constraint failures onto domain errors."""
    if not isinstance(exc, psycopg.Error):
        return exc

    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None) or ""

    # Two guests racing for the last bed can deadlock while both check the
    # no-overlap constraint. The database protects the bed either way, so this
    # is a "try again", not a failure -- left untranslated it reached the guest
    # as a raw database error and their request was silently dropped.
    if isinstance(exc, (psycopg.errors.DeadlockDetected,
                        psycopg.errors.SerializationFailure)):
        return BookingConflict("that bed was taken just now; please try again")

    if isinstance(exc, psycopg.errors.ExclusionViolation):
        if constraint == "bookings_no_overlap":
            return BedUnavailable("that bed is already booked for those dates")
        if constraint == "holds_no_overlap":
            return BedUnavailable("that bed is already held for those dates")
        return BedUnavailable("that bed is not available for those dates")

    if isinstance(exc, psycopg.errors.UniqueViolation):
        if constraint == "uq_payments_txn_ref":
            return PaymentError("that transaction reference has already been recorded")
        if constraint == "messages_wa_message_id_key":
            return BookingConflict("duplicate message")
        return ValidationError("a record with those details already exists")

    if isinstance(exc, psycopg.errors.CheckViolation):
        return ValidationError("a value failed a database check constraint")

    if isinstance(exc, psycopg.errors.ForeignKeyViolation):
        return ValidationError("a referenced record does not exist")

    return exc
