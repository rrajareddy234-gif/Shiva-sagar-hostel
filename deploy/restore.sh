#!/usr/bin/env bash
# Restore a backup. This DESTROYS the current database.
#
#   ./deploy/restore.sh backups/sagarpg-2026-11-03-0215.sql.gz
#
# Rehearse this first with ./deploy/restore_drill.sh, which restores into a
# scratch database and leaves the live one alone. An untested restore is not a
# recovery plan.

set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=deploy/_pg.sh
. deploy/_pg.sh

if [ $# -ne 1 ]; then
  echo "usage: $0 <backup-file.sql.gz>" >&2
  exit 2
fi

FILE="$1"

if [ ! -s "$FILE" ]; then
  echo "refusing to restore: $FILE is missing or empty" >&2
  exit 1
fi

if ! gzip -t "$FILE"; then
  echo "refusing to restore: $FILE is corrupt" >&2
  exit 1
fi

echo "About to REPLACE database '$PG_DB' with $FILE."
echo "Type the database name to confirm:"
read -r CONFIRM
if [ "$CONFIRM" != "$PG_DB" ]; then
  echo "aborted"
  exit 1
fi

# Take a fresh backup first, so a wrong restore is itself recoverable.
./deploy/backup.sh

echo "Stopping the app so nothing writes during the restore"
case "$(_pg_mode)" in
  compose) $DOCKER compose -f "$COMPOSE_FILE" stop app ;;
  *) echo "  (not a compose deployment; stop the app yourself before continuing)" ;;
esac

echo "Dropping and recreating '$PG_DB'"
_pg_run postgres -q \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$PG_DB' AND pid <> pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"$PG_DB\";" \
  -c "CREATE DATABASE \"$PG_DB\";"

# The extensions live outside the dump's table data, so restore them first.
_pg_run "$PG_DB" -q -c "CREATE EXTENSION IF NOT EXISTS btree_gist;"
_pg_run "$PG_DB" -q -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
_pg_run "$PG_DB" -q -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

echo "Restoring"
zcat "$FILE" | _pg_run "$PG_DB" -q -v ON_ERROR_STOP=1

echo "Starting the app"
case "$(_pg_mode)" in
  compose) $DOCKER compose -f "$COMPOSE_FILE" start app ;;
  *) echo "  (not a compose deployment; start the app yourself)" ;;
esac

echo "Restore finished. Checking row counts:"
_pg_run "$PG_DB" -tAc "SELECT 'bookings=' || count(*) FROM bookings;"
_pg_run "$PG_DB" -tAc "SELECT 'payments=' || count(*) FROM payments;"
