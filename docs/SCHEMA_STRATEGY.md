# Schema strategy

`db/schema.sql` is the single authoritative definition of the database. There is
no separate migrations directory and no migration tool: the file is both the
initial install and the upgrade path.

## One file, re-runnable

`core/db.py::Database.apply_schema` runs the whole file on every app start, and
`db/schema.sql` is written so that a second run changes nothing:

- New tables use `CREATE TABLE IF NOT EXISTS`.
- New columns on an existing table use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- Constraints that may need to change use
  `ALTER TABLE ... DROP CONSTRAINT IF EXISTS` followed by `ADD CONSTRAINT`.
- Seed-like backfills are guarded so they do not overwrite live rows.

That means a fresh database and a database that has been running for months both
end up at the same shape after one `apply_schema()` call. `scripts/chat.py`
applies `db/schema.sql` and then `db/seed.sql` to a throwaway database, and the
deploy path (`deploy/go-live.sh`, `deploy/docker-compose.lite.yml`) does the same
against the real one.

## Additive only

A change must never require dropping and recreating a table with live data. To
add a value to a `CHECK` list, drop the old constraint and add the new one, as
`outbound_messages_message_type_check` does for the `flow` message type. To
retire a table, stop creating it and leave a comment in its place, as the removed
`bed_holds` block does; an existing database may drop the leftover table in a
maintenance window.

## Verify after a schema change

Run both paths before calling a schema change done:

1. Fresh install: create an empty database, apply `db/schema.sql`, then
   `db/seed.sql`.
2. Re-apply: run `db/schema.sql` and `db/seed.sql` again on the same database and
   confirm nothing errors and no rows are duplicated or overwritten unexpectedly.

`tests/test_deploy_scripts.py` asserts the deploy path loads the schema and seed
in the right order and does not re-seed on every restart.
