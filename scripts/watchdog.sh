#!/usr/bin/env bash
# Keep Aurora Markets alive: check health every 30s, restart if it is down.
#
# Run it detached so it survives the parent shell:
#   setsid nohup scripts/watchdog.sh > .run/watchdog.log 2>&1 < /dev/null &
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT="${PORT:-12000}"
INTERVAL="${WATCHDOG_INTERVAL:-30}"
HEALTH="http://127.0.0.1:$PORT/api/health"

# One watchdog only.
if [ -f "$ROOT/.run/watchdog.pid" ] && kill -0 "$(cat "$ROOT/.run/watchdog.pid")" 2>/dev/null; then
  exit 0
fi
mkdir -p "$ROOT/.run"
echo $$ > "$ROOT/.run/watchdog.pid"

trap 'rm -f "$ROOT/.run/watchdog.pid"; exit 0' TERM INT

while true; do
  if ! curl -fsS --max-time 5 "$HEALTH" >/dev/null 2>&1; then
    echo "$(date -Is) app is down; restarting"
    PORT="$PORT" "$ROOT/scripts/serve.sh" || true
  fi
  sleep "$INTERVAL"
done
