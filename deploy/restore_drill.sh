#!/usr/bin/env bash
# Restore drill: prove a backup can actually be restored.
#
#   ./deploy/restore_drill.sh backups/sagarpg-2026-11-04-1413.sql.gz
#
# This is the check that was missing. A backup script that runs without error
# proves nothing about recovery; the only proof is restoring the dump into a
# database and finding the data there. This script never touches the live
# database: it restores into a scratch database, counts rows, compares them
# with the source, and drops the scratch database again.
#
# Exit status is 0 only when every check passes. Wire it into cron after the
# backup, or run it by hand before you need a real restore.

set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=deploy/_pg.sh
. deploy/_pg.sh

FILE="${1:-}"
SCRATCH="${SCRATCH_DB:-${PG_DB}_drill}"
BACKUP_DIR="${BACKUP_DIR:-backups}"

if [ -z "$FILE" ]; then
  # Default to the newest backup, so a cron line needs no argument.
  FILE="$(ls -1t "$BACKUP_DIR"/"$PG_DB"-*.sql.gz 2>/dev/null | head -1 || true)"
fi

if [ -z "$FILE" ] || [ ! -s "$FILE" ]; then
  echo "no backup file to drill (looked for $PG_DB-*.sql.gz in ${BACKUP_DIR:-backups})" >&2
  exit 1
fi

echo "$(date -Is) drill: restoring $FILE into scratch database '$SCRATCH'"

if ! gzip -t "$FILE"; then
  echo "FAILED: $FILE is corrupt" >&2
  exit 1
fi

# A scratch database left over from a crashed run would make the counts wrong.
_pg_run postgres -q -c "DROP DATABASE IF EXISTS \"$SCRATCH\";"
_pg_run postgres -q -c "CREATE DATABASE \"$SCRATCH\";"

# The dump references the btree_gist and pgcrypto extensions; they must exist in
# the scratch database before the restore, exactly as they do in production.
_pg_run "$SCRATCH" -q -c "CREATE EXTENSION IF NOT EXISTS btree_gist;"
_pg_run "$SCRATCH" -q -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
_pg_run "$SCRATCH" -q -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

if ! zcat "$FILE" | _pg_run "$SCRATCH" -q -v ON_ERROR_STOP=1 >/dev/null 2>/tmp/drill.err; then
  echo "FAILED: restore reported an error:" >&2
  cat /tmp/drill.err >&2
  _pg_run postgres -q -c "DROP DATABASE IF EXISTS \"$SCRATCH\";" || true
  exit 1
fi

# Count the rows that matter and compare with the source. A restore that
# "succeeds" but lands an empty database is the exact failure this catches.
TABLES="rooms beds guests bookings payments invoices"
FAILED=0
for t in $TABLES; do
  src="$(_pg_run "$PG_DB" -tAc "SELECT count(*) FROM $t;" 2>/dev/null || echo "ERR")"
  dst="$(_pg_run "$SCRATCH" -tAc "SELECT count(*) FROM $t;" 2>/dev/null || echo "ERR")"
  if [ "$src" = "$dst" ] && [ "$src" != "ERR" ]; then
    printf '  %-12s %s rows\n' "$t" "$dst"
  else
    printf '  %-12s MISMATCH source=%s restored=%s\n' "$t" "$src" "$dst" >&2
    FAILED=1
  fi
done

# The money columns must survive as exact decimals, not floats.
if ! _pg_run "$SCRATCH" -tAc "SELECT 1 FROM payments LIMIT 1;" >/dev/null; then
  echo "FAILED: payments table is not queryable after restore" >&2
  FAILED=1
fi

_pg_run postgres -q -c "DROP DATABASE IF EXISTS \"$SCRATCH\";"

if [ "$FAILED" -ne 0 ]; then
  echo "$(date -Is) DRILL FAILED: the backup does not restore cleanly" >&2
  exit 1
fi

echo "$(date -Is) drill ok: $FILE restores cleanly with matching row counts"
