#!/usr/bin/env bash
# One-command launcher for the Tradli-style platform.
set -u
cd "$(dirname "$0")"
PORT="${PORT:-12001}"
export PORT

echo "→ Installing dependencies (if needed)…"
python3 -m pip install --quiet -r requirements.txt

echo "→ Stopping any previous instance…"
pkill -f "app/server.py" 2>/dev/null
sleep 1

echo "→ Starting on http://localhost:${PORT}"
nohup python3 app/server.py > server.log 2>&1 &
sleep 3

code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/api/health")
if [ "$code" = "200" ]; then
  echo "✅ Up. Open http://localhost:${PORT}   (logs: server.log)"
else
  echo "❌ Failed to start (HTTP $code). See server.log"
  tail -20 server.log
fi

# Build a portable zip for sharing / moving to another machine.
# Pass "zip" as an argument, or run ./run.sh zip
if [ "${1:-}" = "zip" ]; then
  ./make-portable.sh
fi
