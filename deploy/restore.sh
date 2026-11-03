#!/usr/bin/env bash
# Restore a backup. This DESTROYS the current database.
#
#   ./deploy/restore.sh backups/sagarpg-2026-11-03-0215.sql.gz
#
# Rehearse this on a copy before you ever need it. An untested restore is not a
# recovery plan.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
  echo "usage: $0 <backup-file.sql.gz>" >&2
  exit 2
fi

FILE="$1"
DB="${POSTGRES_DB:-sagarpg}"
USER="${POSTGRES_USER:-sagar}"

if [ ! -s "$FILE" ]; then
  echo "refusing to restore: $FILE is missing or empty" >&2
  exit 1
fi

if ! gzip -t "$FILE"; then
  echo "refusing to restore: $FILE is corrupt" >&2
  exit 1
fi

echo "About to REPLACE database '$DB' with $FILE."
echo "Type the database name to confirm:"
read -r CONFIRM
if [ "$CONFIRM" != "$DB" ]; then
  echo "aborted"
  exit 1
fi

# Take a fresh backup first, so a wrong restore is itself recoverable.
./deploy/backup.sh

COMPOSE="docker compose -f deploy/docker-compose.yml"

echo "Stopping the app so nothing writes during the restore"
$COMPOSE stop app

echo "Dropping and recreating '$DB'"
$COMPOSE exec -T postgres psql -U "$USER" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB' AND pid <> pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"$DB\";" \
  -c "CREATE DATABASE \"$DB\";"

echo "Restoring"
zcat "$FILE" | $COMPOSE exec -T postgres psql -U "$USER" -d "$DB" -q

echo "Starting the app"
$COMPOSE start app

echo "Restore finished. Checking:"
sleep 5
curl -fsS http://localhost/health || echo "health check failed, investigate" >&2
