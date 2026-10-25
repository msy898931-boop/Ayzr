"""
The engine: demo fallback, live path (mocked), and the quant orchestration.
==========================================================================

Network is never touched: `market.fetch` is monkeypatched with a synthetic
candle series, and the LLM layer is exercised by swapping `llm.complete`.
"""
from __future__ import annotations

import base64
import struct
import zlib

import pytest


def png(w: int = 8, h: int = 6) -> bytes:
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + bytes([10, 20, 30]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def ohlc(n: int = 260) -> dict:
    """A gentle uptrend with enough bars for EMA200 and pivots."""
    closes = [100 + i * 0.2 + (i % 5) * 0.05 for i in range(n)]
    return {
        "time": list(range(n)),
        "open": [c - 0.05 for c in closes],
        "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
        "volume": [1000 + (i % 7) * 50 for i in range(n)],
        "symbol": "XAUUSD", "timeframe": "H1", "label": "Gold",
    }


# ----------------------------------------------------------- demo fallback
def test_analyze_demo_when_no_key(clean):
    e, im = clean["engine"], clean["imaging"]
    raw = png()
    out = e.analyze(raw, "image/png", "XAUUSD", "H1")
    assert out["live"] is False
    assert out["provider"] == "demo"
    assert out["direction"] in {"BUY", "SELL", "WAIT"}
    assert out["image"]["fingerprint"] == im.fingerprint(raw)
    assert len(out["agents"]) == 11


def test_demo_is_deterministic(clean):
    e = clean["engine"]
    a = e.analyze(png(), "image/png", "XAUUSD", "H1")
    b = e.analyze(png(), "image/png", "XAUUSD", "H1")
    assert a["direction"] == b["direction"]
    assert a["confidence"] == b["confidence"]


def test_demo_differs_across_images(clean):
    e = clean["engine"]
    a = e.analyze(png(8, 6), "image/png", "XAUUSD", "H1")
    b = e.analyze(png(9, 9), "image/png", "XAUUSD", "H1")
    assert a["image"]["fingerprint"] != b["image"]["fingerprint"]


def test_verify_advise_zones_demo(clean):
    e = clean["engine"]
    v = e.verify(png(), "image/png", "look at my win")
    assert v["live"] is False and v["verdict"] in {"genuine", "suspicious", "fake"}
    adv = e.advise(png(), "image/png", "long XAUUSD")
    assert adv["live"] is False and adv["action"] in {"hold", "reduce", "close", "hedge"}
    z = e.zones(png(), "image/png")
    assert z["live"] is False and "supply" in z and "demand" in z


# --------------------------------------------------------------- live path
def test_analyze_live_fast(clean, monkeypatch):
    e, llm = clean["engine"], clean["llm"]
    monkeypatch.setattr(llm, "has_live_model", lambda: True)
    monkeypatch.setattr(llm, "provider_name", lambda: "gemini")
    monkeypatch.setattr(llm, "model_name", lambda: "gemini-3.5-flash")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (
        '{"direction":"BUY","trend":"up","confidence":77,"entry":1.1,'
        '"stop":1.0,"targets":[1.2,1.3,1.4],"summary_ar":"صعود",'
        '"summary_en":"up","agents":[{"key":"trend","bias":"BUY",'
        '"confidence":80,"note_ar":"","note_en":""}]}'))
    out = e.analyze(png(), "image/png", "XAUUSD", "H1", mode="fast")
    assert out["live"] is True
    assert out["provider"] == "gemini"
    assert out["direction"] == "BUY"
    assert out["confidence"] == 77


def test_analyze_falls_back_when_model_fails(clean, monkeypatch):
    e, llm = clean["engine"], clean["llm"]
    monkeypatch.setattr(llm, "has_live_model", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("all providers failed")
    monkeypatch.setattr(llm, "complete", boom)
    out = e.analyze(png(), "image/png", "XAUUSD", "H1")
    assert out["live"] is False
    assert "fallback_reason" in out


def test_analyze_full_runs_every_agent(clean, monkeypatch):
    e, llm = clean["engine"], clean["llm"]
    monkeypatch.setattr(llm, "has_live_model", lambda: True)
    monkeypatch.setattr(llm, "provider_name", lambda: "groq")
    monkeypatch.setattr(llm, "model_name", lambda: "qwen/qwen3.8-27b")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (
        '{"bias":"SELL","confidence":60,"note_ar":"","note_en":""}'))
    out = e.analyze(png(), "image/png", "XAUUSD", "H1", mode="full")
    assert len(out["agents"]) == 11
    assert out["agreement"]["of"] == 11
    assert out["chief"]["key"] == "chief"


# ----------------------------------------------------------- quant engine
def test_quant_analyze_uses_market(clean, monkeypatch):
    e, market = clean["engine"], clean["market"]
    monkeypatch.setattr(market, "fetch", lambda s, tf="H1", bars=300: ohlc())
    out = e.quant_analyze("XAUUSD", "H1", account=1000, risk_pct=1.0)
    assert out["symbol"] == "XAUUSD"
    assert out["data_live"] is True
    assert out["provider"] == "rules"  # no key -> rules only
    assert "indicators" in out and out["decision"]["verdict"] in {"BUY", "SELL", "WAIT"}


def test_quant_analyze_rejects_unknown_symbol(clean):
    with pytest.raises(ValueError):
        clean["engine"].quant_analyze("FAKE", "H1")


def test_quant_analyze_plan_is_risk_aware(clean, monkeypatch):
    e, market = clean["engine"], clean["market"]
    monkeypatch.setattr(market, "fetch", lambda s, tf="H1", bars=300: ohlc())
    rich = e.quant_analyze("XAUUSD", "H1", account=1_000_000, risk_pct=1.0,
                           explain=False)
    assert rich["plan"]["stop_mode"] == "volatility"
    assert rich["plan"]["feasible"] is True
    assert rich["size"]["actual_pct"] <= 1.0


def test_quant_analyze_flags_account_too_small(clean, monkeypatch):
    e, market = clean["engine"], clean["market"]
    monkeypatch.setattr(market, "fetch", lambda s, tf="H1", bars=300: ohlc())
    tiny = e.quant_analyze("XAUUSD", "H1", account=20, risk_pct=1.0, explain=False)
    assert tiny["plan"]["feasible"] is False
    assert tiny["size"]["warning"]["level"] == "high"
    assert tiny["plan"]["min_account_for_floor_stop"] > 20


def test_quant_analyze_explains_when_model_available(clean, monkeypatch):
    e, market, llm = clean["engine"], clean["market"], clean["llm"]
    monkeypatch.setattr(market, "fetch", lambda s, tf="H1", bars=300: ohlc())
    monkeypatch.setattr(llm, "has_live_model", lambda: True)
    monkeypatch.setattr(llm, "provider_name", lambda: "gemini")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: '{"summary_ar":"ok","summary_en":"ok"}')
    out = e.quant_analyze("XAUUSD", "H1")
    assert out["ai_live"] is True
    assert out["explanation"]["summary_en"] == "ok"


def test_quant_analyze_survives_explain_failure(clean, monkeypatch):
    e, market, llm = clean["engine"], clean["market"], clean["llm"]
    monkeypatch.setattr(market, "fetch", lambda s, tf="H1", bars=300: ohlc())
    monkeypatch.setattr(llm, "has_live_model", lambda: True)
    def boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(llm, "complete", boom)
    out = e.quant_analyze("XAUUSD", "H1")
    assert out["ai_live"] is False
    assert "explain_error" in out


def test_quant_scan_ranks_and_reports_errors(clean, monkeypatch):
    e, market = clean["engine"], clean["market"]

    def fake_fetch(s, tf="H1", bars=300):
        if s == "BAD":
            raise market.MarketError("nope")
        return ohlc()

    monkeypatch.setattr(market, "fetch", fake_fetch)
    out = e.quant_scan("H1", symbols=["XAUUSD", "EURUSD", "BAD"])
    assert out["timeframe"] == "H1"
    assert len(out["results"]) == 2
    assert len(out["errors"]) == 1
    assert out["errors"][0]["symbol"] == "BAD"


def test_round_helper_handles_none(clean):
    assert clean["engine"]._round(None, 2) is None
    assert clean["engine"]._round(1.2345, 2) == 1.23
