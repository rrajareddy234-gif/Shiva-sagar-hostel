#!/usr/bin/env bash
# Nightly database backup. Keeps 14 days locally.
#
# Run from cron:
#   15 2 * * * /opt/sagar/deploy/backup.sh >> /var/log/sagar-backup.log 2>&1
#
# A backup on the same server is not a backup. Copy the result off the server,
# for example with rclone to cloud storage, or scp to another machine.
#
# Works against docker compose, a plain container, or a local psql; see _pg.sh.

set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=deploy/_pg.sh
. deploy/_pg.sh

BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%F-%H%M)"

mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/$PG_DB-$STAMP.sql.gz"

echo "$(date -Is) starting backup to $FILE"

_pg_dump "$PG_DB" | gzip > "$FILE"

# A dump that is empty or truncated is worse than no dump, because it looks
# like one. Check it before declaring success.
if [ ! -s "$FILE" ]; then
  echo "$(date -Is) FAILED: $FILE is empty" >&2
  rm -f "$FILE"
  exit 1
fi

if ! gzip -t "$FILE"; then
  echo "$(date -Is) FAILED: $FILE is corrupt" >&2
  exit 1
fi

# Confirm the dump actually contains the tables.
#
# Decompress to a temp file first, then grep it. Piping into `grep -q` would
# look right but is a trap: grep exits as soon as it matches, zcat gets SIGPIPE,
# and `set -o pipefail` turns that into a failed pipeline — so a perfectly good
# backup would be reported as broken and then deleted. Reading a file has no
# such race.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
zcat "$FILE" > "$TMP"
if ! grep -q "CREATE TABLE.*bookings" "$TMP"; then
  echo "$(date -Is) FAILED: $FILE has no bookings table" >&2
  exit 1
fi

SIZE="$(du -h "$FILE" | cut -f1)"
echo "$(date -Is) backup ok: $FILE ($SIZE)"

find "$BACKUP_DIR" -name "$PG_DB-*.sql.gz" -mtime "+$KEEP_DAYS" -delete
echo "$(date -Is) pruned backups older than $KEEP_DAYS days"
