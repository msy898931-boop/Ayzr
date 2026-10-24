"""
Signal ingest.
==============

The signals hub is rendered into the page, so accepting arbitrary signals from
an unauthenticated caller would let anyone write markup into every visitor's
screen. These tests pin the guard: off by default, token required, payload
validated and bounded.
"""
from __future__ import annotations

import pytest

TOKEN = "ingest-token-abc"


@pytest.fixture()
def ingest_on(clean, monkeypatch):
    monkeypatch.setattr(clean["config"], "SIGNAL_INGEST_TOKEN", TOKEN)
    return clean


def _post(client, payload, token=TOKEN):
    return client.post("/api/signals/ingest", json=payload,
                       headers={"Authorization": f"Bearer {token}"})


def test_ingest_off_by_default(client):
    r = client.post("/api/signals/ingest", json={"channel": "x", "symbol": "XAUUSD",
                                                 "direction": "BUY"})
    assert r.status_code == 503


def test_ingest_rejects_bad_token(client, ingest_on):
    r = _post(client, {"channel": "x", "symbol": "XAUUSD", "direction": "BUY"},
              token="nope")
    assert r.status_code == 403


def test_ingest_accepts_valid_signal(client, ingest_on):
    r = _post(client, {"channel": "Desk", "symbol": "xauusd", "direction": "buy",
                       "entry": 2400, "stop": 2390, "targets": [2410, 2420],
                       "credibility": 70})
    assert r.status_code == 201
    s = r.get_json()["signal"]
    assert s["symbol"] == "XAUUSD"
    assert s["direction"] == "BUY"
    assert s["credibility"] == 70
    # And it is now visible through the public read endpoint.
    listed = client.get("/api/signals").get_json()["signals"]
    assert any(x["id"] == s["id"] for x in listed)


def test_ingest_rejects_bad_direction(client, ingest_on):
    r = _post(client, {"channel": "x", "symbol": "XAUUSD", "direction": "HOLD"})
    assert r.status_code == 400


def test_ingest_clamps_and_bounds_fields(client, ingest_on):
    r = _post(client, {"channel": "  A   B  " + "x" * 100, "symbol": "eurusd",
                       "direction": "SELL", "credibility": 9999})
    s = r.get_json()["signal"]
    assert s["channel"].startswith("A B ")
    assert len(s["channel"]) <= 40           # bounded, so it cannot flood the UI
    assert s["credibility"] == 100           # clamped


def test_ingest_rejects_missing_fields(client, ingest_on):
    assert _post(client, {"symbol": "XAUUSD", "direction": "BUY"}).status_code == 400
    assert _post(client, {"channel": "x", "direction": "BUY"}).status_code == 400
