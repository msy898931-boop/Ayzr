#!/usr/bin/env bash
# Start Aurora Markets in production mode on a fixed port.
#
# Idempotent: if it is already serving, this does nothing. Safe to call from a
# watchdog or a login hook. Logs go to .run/aurora.log, the pid to .run/aurora.pid.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT="${PORT:-12000}"
RUN_DIR="$ROOT/.run"
LOG="$RUN_DIR/aurora.log"
PIDFILE="$RUN_DIR/aurora.pid"
SECRET_FILE="$RUN_DIR/secret.key"
GUNICORN="$ROOT/.venv/bin/gunicorn"
PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$RUN_DIR"

# A stable SECRET_KEY across restarts; generate it once.
if [ ! -s "$SECRET_FILE" ]; then
  "$PYTHON" -c "import secrets;print(secrets.token_hex(32))" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
fi
export SECRET_KEY="$(cat "$SECRET_FILE")"
export TRADLI_ENV="${TRADLI_ENV:-production}"
export TRUST_PROXY="${TRUST_PROXY:-true}"
export PORT
export CONTACT_EMAIL="${CONTACT_EMAIL:-support@example.com}"

# Already healthy? Leave it alone.
if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  exit 0
fi

# Stale pid file from a previous boot: clear it.
if [ -f "$PIDFILE" ] && ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  rm -f "$PIDFILE"
fi

setsid nohup "$GUNICORN" -c gunicorn.conf.py wsgi:app \
  > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
disown 2>/dev/null || true

# Wait until it answers, so callers know the outcome.
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "aurora: failed to become healthy; see $LOG" >&2
exit 1
