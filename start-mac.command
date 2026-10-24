#!/usr/bin/env bash
# Double-click this file on macOS to start the platform.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close…"
  exit 1
fi
python3 launch.py
