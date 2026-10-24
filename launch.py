#!/usr/bin/env python3
"""
Portable launcher for the Tradli-style platform.

Double-click one of the wrappers next to this file:
    Windows : start-windows.bat
    macOS   : start-mac.command
    Linux   : start-linux.sh

It creates a private virtual environment, installs the two dependencies once,
starts the server and opens the dashboard in your browser. Everything stays
inside this folder -- nothing is installed system-wide.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PORT = os.environ.get("PORT", "8765")
URL = f"http://localhost:{PORT}/"
MIN_PY = (3, 9)
NEEDED = "import flask, requests"


def die(msg: str) -> None:
    print("\n" + "!" * 60)
    print("  " + msg)
    print("!" * 60 + "\n")
    input("Press Enter to close…")
    sys.exit(1)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_venv() -> Path:
    py = venv_python()
    if not py.exists():
        print("→ Creating a private environment (first run only)…")
        try:
            venv.create(VENV, with_pip=True)
        except Exception as exc:  # noqa: BLE001
            die(f"Could not create the environment: {exc}")
    return py


def ensure_deps(py: Path) -> None:
    if subprocess.run([str(py), "-c", NEEDED], capture_output=True).returncode == 0:
        return
    print("→ Installing dependencies (first run only, needs internet)…")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    rc = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")]
    ).returncode
    if rc != 0:
        die("Dependency install failed. Check your internet connection and retry.")


def provider_hint() -> str:
    env = ROOT / ".env"
    if env.exists() and "API_KEY=" in env.read_text(encoding="utf-8", errors="ignore"):
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "API_KEY=" in line and line.split("=", 1)[1].strip():
                return line.split("=", 1)[0].replace("_API_KEY", "").lower()
    return "demo"


def main() -> None:
    if sys.version_info < MIN_PY:
        die(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required. You have "
            f"{sys.version_info.major}.{sys.version_info.minor}.")

    print("=" * 60)
    print("  Tradli-style platform — starting")
    print("=" * 60)

    py = ensure_venv()
    ensure_deps(py)

    env = dict(os.environ, PORT=PORT)
    print(f"\n→ Dashboard:  {URL}")
    print(f"→ Provider:   {provider_hint()}   (edit .env to change)")
    print("   (the browser opens automatically; keep this window open)")
    print("   Stop the app with Ctrl+C.\n")

    proc = subprocess.Popen([str(py), str(ROOT / "app" / "server.py")], env=env)
    time.sleep(3)
    try:
        webbrowser.open(URL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n→ Shutting down…")
        proc.terminate()


if __name__ == "__main__":
    main()
