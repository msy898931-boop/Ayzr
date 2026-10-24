"""
Production WSGI entry point.
============================

    gunicorn -c gunicorn.conf.py wsgi:app

Flask's built-in server is single-threaded and explicitly not for production.
This module exposes the same `app` object to a real WSGI server without
changing how `app/server.py` is run in development.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

from server import app  # noqa: E402

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
