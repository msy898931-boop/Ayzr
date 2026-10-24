"""
Shared test fixtures.

The app reads its database path and provider keys from the environment at
import time, so we set both *before* importing anything from `app`. Keys are
blanked so the suite never makes a real model call: every analysis test then
exercises the deterministic DEMO path, which is exactly what we want to pin
down.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

_TMP = tempfile.mkdtemp(prefix="tradli-tests-")
os.environ["TRADLI_DB"] = os.path.join(_TMP, "test.db")
os.environ["TRADLI_ENV"] = "development"
for _k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
           "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_k] = ""

import pytest  # noqa: E402


@pytest.fixture()
def mods():
    """The application modules, imported once against the temp database."""
    import agents
    import billing
    import config
    import db
    import engine
    import imaging
    import llm
    import market
    import quant
    import security
    import server
    return {
        "agents": agents, "config": config, "db": db, "engine": engine,
        "imaging": imaging, "llm": llm, "market": market, "quant": quant,
        "security": security, "server": server, "billing": billing,
    }


@pytest.fixture()
def clean(mods):
    """A blank database and no throttling state, before each test."""
    db = mods["db"]
    db.init()
    with db.connect() as con:
        for table in ("users", "usage", "history", "signals", "orders"):
            con.execute(f"DELETE FROM {table}")
    db.seed_signals()
    mods["security"].reset()
    return mods


@pytest.fixture()
def client(clean):
    clean["server"].app.config["TESTING"] = True
    with clean["server"].app.test_client() as c:
        yield c

def register(client, email: str = "tester@example.com", password: str = "secret123"):
    return client.post("/api/auth/register",
                       json={"email": email, "password": password})


@pytest.fixture()
def auth_client(client, clean):
    register(client)
    clean["uid"] = client.get("/api/me").get_json()["user"]["id"]
    return client
