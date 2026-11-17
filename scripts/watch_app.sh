#!/usr/bin/env bash
# Start the app and the watchdog that keeps it alive.
#
#   bash scripts/watch_app.sh
#
# The app is brought up by start_app.sh. The watchdog then checks /health every
# 30 seconds. If the app stops answering it restarts it and sends the owner one
# WhatsApp message; when the app answers again it sends another.
#
# Both are detached, so they keep running after this shell closes. The watchdog
# survives the app dying, which is the whole point: a dead app cannot report
# itself dead.
#
# To stop the watchdog:  pkill -f scripts/watchdog.py

set -u
cd "$(dirname "$0")/.."

WATCH_LOG=/tmp/sagarpg-watchdog.log
STATE_DIR=${WATCHDOG_STATE_DIR:-/tmp/sagarpg-watchdog}

echo "1/2  App"
bash scripts/start_app.sh || { echo "     app failed to start"; exit 1; }

echo "2/2  Watchdog"
if pgrep -f "scripts/watchdog.py" >/dev/null; then
    echo "     already running (pid $(pgrep -f 'scripts/watchdog.py' | head -1))"
else
    setsid python scripts/watchdog.py > "$WATCH_LOG" 2>&1 < /dev/null &
    sleep 2
fi

if pgrep -f "scripts/watchdog.py" >/dev/null; then
    echo "     watching on port 12000, every 30s"
else
    echo "     watchdog did NOT start - see $WATCH_LOG"
    exit 1
fi

echo
echo "     App log:      /tmp/sagarpg-app.log"
echo "     Watchdog log: $WATCH_LOG"
echo "     Failures:     $STATE_DIR/outages.log"
echo "     Event record: $STATE_DIR/events.jsonl"
echo
echo "     The owner is messaged once per outage, and once on recovery."
