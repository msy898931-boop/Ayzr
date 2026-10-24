"""
LLM layer: provider selection, model lists, JSON extraction.
================================================================

No network calls: `requests` is never exercised here. The interesting logic is
pure and fragile -- choosing a model list, and pulling JSON out of a reply that
a model may have truncated mid-stream.
"""
from __future__ import annotations

import os


# ------------------------------------------------------------ provider state
def test_no_key_means_demo(mods, monkeypatch):
    llm = mods["llm"]
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.provider_name() == "demo"
    assert llm.has_live_model() is False
    assert llm.configured_providers() == []


def test_first_configured_provider_wins(mods, monkeypatch):
    llm = mods["llm"]
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert llm.provider_name() == "groq"
    assert llm.configured_providers() == ["groq"]


def test_gemini_has_priority_over_groq(mods, monkeypatch):
    llm = mods["llm"]
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert llm.provider_name() == "gemini"
    assert llm.configured_providers() == ["gemini", "groq"]


def test_complete_without_key_raises(mods, monkeypatch):
    import pytest
    llm = mods["llm"]
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        llm.complete("sys", "user")


# ------------------------------------------------------------- model lists
def test_models_for_groq_is_vision_only(mods):
    # Every Groq entry must be a vision model: text-only ones 400 on an image.
    models = mods["llm"].models_for("groq")
    assert models == ["qwen/qwen3.8-27b"]


def test_models_for_gemini_returns_the_walk_list(mods):
    assert mods["llm"].models_for("gemini") == mods["llm"].GEMINI_MODELS


def test_groq_max_tokens_is_clamped(mods):
    llm = mods["llm"]
    assert llm._clamp("groq", 1400) == llm.MAX_OUT["groq"]
    assert llm._clamp("groq", 100) == 100           # never raises the ask
    assert llm._clamp("gemini", 1400) == 1400       # untouched elsewhere


def test_candidates_are_unique(mods, monkeypatch):
    llm = mods["llm"]
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    cands = list(llm._candidates())
    assert len(cands) == len(set(cands))
    assert ("groq", "qwen/qwen3.8-27b") in cands


def test_candidates_skip_unconfigured(mods, monkeypatch):
    llm = mods["llm"]
    for env in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert [p for p, _ in llm._candidates()] == ["openai"]


# ---------------------------------------------------------- JSON extraction
def test_extract_plain_object(mods):
    assert mods["llm"].extract_json('{"a": 1}') == {"a": 1}


def test_extract_from_fenced_block(mods):
    text = 'Sure!\n```json\n{"direction": "BUY", "confidence": 80}\n```\n'
    assert mods["llm"].extract_json(text) == {"direction": "BUY", "confidence": 80}


def test_extract_ignores_surrounding_prose(mods):
    text = 'Here is my read. {"verdict": "fake"} Hope that helps.'
    assert mods["llm"].extract_json(text) == {"verdict": "fake"}


def test_extract_empty_is_empty_dict(mods):
    assert mods["llm"].extract_json("") == {}
    assert mods["llm"].extract_json("no json here") == {}


def test_extract_salvages_truncated_object(mods):
    # Output hit the token cap right after a complete key/value pair.
    text = '{"direction": "SELL", "confidence": 61, "agents": [{"key": "Price Action"'
    out = mods["llm"].extract_json(text)
    assert out["direction"] == "SELL"
    assert out["confidence"] == 61


def test_extract_salvages_dangling_string(mods):
    text = '{"direction":"SELL","summary_ar":"كسر دعم'
    out = mods["llm"].extract_json(text)
    assert out["direction"] == "SELL"
    assert out["summary_ar"].startswith("كسر")


def test_extract_salvages_dangling_key(mods):
    # Cut right after a colon: the half-written key must be dropped, not kept.
    text = '{"direction":"BUY","confidence":'
    out = mods["llm"].extract_json(text)
    assert out == {"direction": "BUY"}


def test_salvage_closes_nested_brackets(mods):
    # Three openers ({ { [) must be closed by three closers in reverse order.
    frag = '{"a": {"b": [1, 2'
    assert mods["llm"]._salvage(frag) == '{"a": {"b": [1, 2]}}'


def test_extract_prefers_valid_object_over_salvage(mods):
    # A complete object followed by junk must parse the complete object.
    text = '{"a": 1} trailing {"b": 2}'
    assert mods["llm"].extract_json(text) == {"a": 1}
