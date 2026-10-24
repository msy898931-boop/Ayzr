"""
Spot anchoring for metals.
===========================

Yahoo's XAUUSD proxy is the GC=F *futures* contract, which trades a few dollars
away from the spot price a trader sees on MT5. These tests pin the fix:

  1. a metal series is shifted so its last close equals the spot price;
  2. the shift preserves every candle's shape, so the indicators do not change;
  3. the spot feed is best-effort -- a failure falls back to the Yahoo series
     rather than breaking the whole read.

The last point is the one that matters operationally: a third-party outage must
degrade the price *basis*, never take the product down.
"""
from __future__ import annotations

import pytest


def candles(n: int = 60) -> dict:
    closes = [100 + i * 0.5 for i in range(n)]
    return {
        "time": list(range(n)),
        "open": [c - 0.2 for c in closes],
        "high": [c + 0.4 for c in closes],
        "low": [c - 0.4 for c in closes],
        "close": closes,
        "volume": [10] * n,
        "symbol": "XAUUSD", "timeframe": "H1", "label": "Gold",
    }


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --------------------------------------------------------------- _shift_to_spot
def test_shift_sets_last_close_to_target(mods):
    m = mods["market"]
    out = m._shift_to_spot(candles(), 200.0)
    assert out["close"][-1] == pytest.approx(200.0)
    assert out["spot"] is True


def test_shift_preserves_candle_shape(mods):
    # Every bar moves by the same offset, so ranges and gaps are untouched --
    # this is why the indicators stay valid after anchoring.
    m = mods["market"]
    src = candles()
    out = m._shift_to_spot(src, 200.0)
    for i in range(len(src["close"])):
        assert out["high"][i] - out["low"][i] == pytest.approx(
            src["high"][i] - src["low"][i])
        assert out["close"][i] - src["close"][i] == pytest.approx(
            out["close"][0] - src["close"][0])


def test_shift_leaves_indicators_unchanged(mods):
    m = mods["market"]
    src = candles(120)
    before = m.snapshot(src)
    after = m.snapshot(m._shift_to_spot(src, 5000.0))
    # A pure translation cannot change any shape-based indicator.
    assert after["rsi14"] == pytest.approx(before["rsi14"])
    assert after["adx14"] == pytest.approx(before["adx14"])
    assert after["atr14"] == pytest.approx(before["atr14"])


def test_shift_is_a_noop_when_already_there(mods):
    m = mods["market"]
    src = candles()
    out = m._shift_to_spot(src, src["close"][-1])
    assert out is src  # no needless copy, no bogus spot flag


def test_shift_does_not_mutate_the_source(mods):
    m = mods["market"]
    src = candles()
    original = list(src["close"])
    m._shift_to_spot(src, 999.0)
    assert src["close"] == original


# --------------------------------------------------------------------- spot()
def test_spot_is_none_for_a_non_metal(mods):
    assert mods["market"].spot("EURUSD") is None


def test_spot_disabled_when_url_blank(mods, monkeypatch):
    monkeypatch.setattr(mods["config"], "GOLD_API_URL", "")
    assert mods["market"].spot("XAUUSD") is None


def test_spot_reads_and_caches(mods, monkeypatch):
    m = mods["market"]
    m._spot_cache.clear()
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return FakeResp({"price": 2500.0})

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert m.spot("XAUUSD") == 2500.0
    assert m.spot("XAUUSD") == 2500.0   # second hit served from cache
    assert calls["n"] == 1


def test_spot_never_raises_on_a_bad_endpoint(mods, monkeypatch):
    m = mods["market"]
    m._spot_cache.clear()

    def boom(url, **kw):
        raise OSError("network down")

    monkeypatch.setattr(m.requests, "get", boom)
    assert m.spot("XAUUSD") is None


def test_spot_rejects_a_non_positive_price(mods, monkeypatch):
    m = mods["market"]
    m._spot_cache.clear()
    monkeypatch.setattr(m.requests, "get", lambda url, **kw: FakeResp({"price": 0}))
    assert m.spot("XAUUSD") is None


# ------------------------------------------------------------- _apply_spot
def test_apply_spot_anchors_a_metal(mods, monkeypatch):
    m = mods["market"]
    monkeypatch.setattr(m, "spot", lambda s: 2000.0)
    out = m._apply_spot("XAUUSD", candles())
    assert out["close"][-1] == pytest.approx(2000.0)


def test_apply_spot_is_a_noop_without_a_quote(mods, monkeypatch):
    m = mods["market"]
    monkeypatch.setattr(m, "spot", lambda s: None)
    src = candles()
    assert m._apply_spot("XAUUSD", src) is src


def test_apply_spot_ignores_non_metals(mods, monkeypatch):
    m = mods["market"]
    monkeypatch.setattr(m, "spot", lambda s: 1.2345)
    src = candles()
    assert m._apply_spot("EURUSD", src) is src


# ------------------------------------------------------------------- engine
def test_quant_analyze_reports_the_spot_basis(clean, monkeypatch):
    e, m = clean["engine"], clean["market"]
    series = candles(260)
    series = m._shift_to_spot(series, 2400.0)
    monkeypatch.setattr(m, "fetch", lambda s, tf="H1", bars=300: series)
    out = e.quant_analyze("XAUUSD", "H1", explain=False)
    assert out["spot"] is True
    assert out["price"] == pytest.approx(2400.0)
