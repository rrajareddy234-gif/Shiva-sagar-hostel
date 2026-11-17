#!/usr/bin/env bash
# Bring the whole app back up after the workspace has been reset or slept.
#
# A preview workspace can be recycled at any time: Docker stops, the Python
# packages disappear and the database goes with it. This script puts all of it
# back and leaves the app listening on port 12000, which is what the public
# preview link points at.
#
#   bash scripts/start_app.sh
#
# It is safe to run more than once.

set -u
cd "$(dirname "$0")/.."

D="sudo docker"
DSN="postgresql://sagar:devpass@localhost:5432/sagarpg"
PORT=12000
LOG=/tmp/sagarpg-app.log

echo "1/5  Docker engine"
if ! $D info >/dev/null 2>&1; then
    sudo dockerd > /tmp/dockerd.log 2>&1 &
    for _ in $(seq 1 15); do
        $D info >/dev/null 2>&1 && break
        sleep 1
    done
fi
$D info >/dev/null 2>&1 && echo "     up" || { echo "     FAILED - see /tmp/dockerd.log"; exit 1; }

echo "2/5  Python packages"
python -c "import fastapi, uvicorn, psycopg" 2>/dev/null \
    || pip install -q -r requirements.txt
echo "     ready"

echo "3/5  Database"
if ! $D ps --format '{{.Names}}' | grep -qx sagarpg-pg; then
    $D compose -f deploy/docker-compose.dev.yml up -d >/dev/null 2>&1
    for _ in $(seq 1 30); do
        $D exec sagarpg-pg pg_isready -U sagar -d sagarpg >/dev/null 2>&1 && break
        sleep 1
    done
fi
$D exec sagarpg-pg pg_isready -U sagar -d sagarpg >/dev/null 2>&1 \
    && echo "     accepting connections" \
    || { echo "     FAILED to start postgres"; exit 1; }

# An empty database means the volume was lost with the workspace. The newest
# backup is the best we have; restoring it brings the schema and the rate card.
#
# The backup script writes `*.sql.gz`; an older hand-made dump is `*.sql`. Both
# are looked for, gzipped first, and a compressed dump is decompressed on the
# way in. Looking only for `*.sql` -- as this did -- found nothing once the
# nightly job had run, so a lost volume came back as an empty hostel with the
# owner's real data sitting unread in the backups directory.
if [ "$($D exec -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -tAc \
        "SELECT to_regclass('public.beds') IS NOT NULL" 2>/dev/null)" != "t" ]; then
    newest=$(ls -1t backups/*.sql.gz backups/*.sql 2>/dev/null | head -1)
    if [ -n "$newest" ]; then
        echo "     database is empty - restoring $newest"
        case "$newest" in
            *.gz)
                if ! gzip -t "$newest" 2>/dev/null; then
                    echo "     FAILED: $newest is corrupt; refusing to restore" >&2
                    exit 1
                fi
                gunzip -c "$newest" | $D exec -i -e PGPASSWORD=devpass \
                    sagarpg-pg psql -U sagar -d sagarpg -q
                ;;
            *)
                $D exec -i -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -q < "$newest"
                ;;
        esac
    else
        echo "     database is empty and no backup found - loading schema and seed"
        $D exec -i -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -q < db/schema.sql
    fi
fi

# A backup can be older than the schema. The one on disk predates hostel_facts
# and faq, so restoring it alone left the owner's knowledge base -- address,
# amenities, the map link, the FAQs -- empty and the app answered from nothing.
# The schema is idempotent, so it always runs. The seed runs only when the
# knowledge base is missing: re-running it would reset the owner's own
# confirmation flags, and it is only needed to fill this gap.
echo "     applying schema"
$D exec -i -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -q < db/schema.sql

facts=$(  $D exec -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -tAc \
        "SELECT count(*) FROM hostel_facts" 2>/dev/null)
if [ "${facts:-0}" = "0" ]; then
    echo "     knowledge base is empty - loading the seed"
    $D exec -i -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg -q < db/seed.sql
fi

echo "4/5  App"
pkill -f "uvicorn api.main:app" 2>/dev/null
sleep 1
# No fixed dashboard password: a guessable one would open every guest's
# details to anyone who tried it. Use the owner's, or make one and show it.
DASH_PW="${DASHBOARD_PASSWORD:-$(python -c 'import secrets;print(secrets.token_urlsafe(12))')}"
setsid env DATABASE_URL="$DSN" DASHBOARD_PASSWORD="$DASH_PW" \
    python -m uvicorn api.main:app --host 0.0.0.0 --port "$PORT" \
    > "$LOG" 2>&1 < /dev/null &
for _ in $(seq 1 20); do
    curl -sf -o /dev/null "http://localhost:$PORT/health" && break
    sleep 1
done

echo "5/5  Check"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/health")
if [ "$code" = "200" ]; then
    echo "     app is up on port $PORT"
    echo
    echo "     Dashboard: https://work-1-otnhsbjenexyfgzf.prod-runtime.all-hands.dev/dashboard"
    if [ -z "${DASHBOARD_PASSWORD:-}" ]; then
        echo "     Dashboard password for this run: $DASH_PW"
        echo "     (set DASHBOARD_PASSWORD to keep a fixed one)"
    fi
    echo "     Logs:      tail -f $LOG"
else
    echo "     app did not answer (HTTP $code) - see $LOG"
    exit 1
fi
