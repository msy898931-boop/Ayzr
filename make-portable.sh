#!/usr/bin/env bash
# Build a portable zip of the platform. The archive carries the source plus
# the per-OS launchers; it deliberately EXCLUDES any real secrets (.env),
# the local database and virtualenv, so it is safe to share or move.
set -eu
cd "$(dirname "$0")"

OUT="dist/Market-AI-Platform.zip"
STAGE="dist/stage"

echo "→ Cleaning previous build…"
rm -rf dist
mkdir -p "$STAGE"

echo "→ Copying project files…"
# Everything a fresh machine needs to run. .env is intentionally NOT copied.
cp -r app "$STAGE/"
cp requirements.txt README.md AGENTS.md run.sh launch.py \
   start-windows.bat start-mac.command start-linux.sh .env.example "$STAGE/"

echo "→ Zipping…"
python3 - "$STAGE" "$OUT" <<'PY'
import os, sys, zipfile
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(stage):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            full = os.path.join(root, name)
            z.write(full, os.path.relpath(full, stage))
PY
rm -rf "$STAGE"

echo "✅ Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "   Contains: app/, launchers for Windows/macOS/Linux, README."
echo "   Excludes: .env (secrets), data/, .venv/"
