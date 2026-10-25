"""
LLM provider layer.
===================

Products like this are, at their core, a thin wrapper around a vision
model. This module keeps that wrapper pluggable so the same app works with
OpenAI, Anthropic or Google Gemini -- whichever key the operator has.

If NO key is configured the app stays fully usable in DEMO mode: the engine
generates a deterministic, clearly-labelled analysis from the image itself.
Nothing is faked as if it came from a model; every payload carries
``provider`` and ``live`` so the UI can label it honestly.
"""
from __future__ import annotations

import json
import os
import re
import time

import config

try:
    import requests
except ImportError:  # pragma: no cover - requests is in requirements.txt
    requests = None

TIMEOUT = 90

# (provider, env var) in priority order. The first key found wins, so a user
# who has several configured gets the cheapest/fastest by ordering these.
PROVIDER_ENV = [
    ("gemini", "GEMINI_API_KEY"),
    ("gemini", "GOOGLE_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
]

DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-3.5-flash",          # free tier, sees images
    "groq": "qwen/qwen3.8-27b",            # free, fast, the vision model Groq keeps
    "openrouter": "meta-llama/llama-4-maverick:free",
}

# Gemini retires old model names and occasionally returns 503 under load, so we
# walk this list until one answers. Override with GEMINI_MODEL / LLM_MODEL.
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]

# Groq rotates its catalogue often and most of its models are text-only
# (passing an image to those returns 400 "content must be a string"). Keep only
# vision-capable models here; override with GROQ_MODEL / LLM_MODEL.
GROQ_MODELS = [
    "qwen/qwen3.8-27b",
]

# Per-provider ceiling on requested output tokens. Groq's free tier enforces a
# hard output-tokens-per-minute limit (currently 1000) and rejects the whole
# request if max_tokens exceeds it, so we ask for less than the cap.
MAX_OUT = {
    "groq": 900,
}


def _clamp(provider: str, max_tokens: int) -> int:
    cap = MAX_OUT.get(provider)
    return min(max_tokens, cap) if cap else max_tokens

# HTTP codes that mean "this model/provider is unavailable right now" and are
# worth retrying elsewhere, rather than a bug in our request.
RETRYABLE = {404, 408, 429, 500, 502, 503, 504}

# OpenAI-compatible providers: base URL + the env var holding the key.
COMPATIBLE = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


def provider_name() -> str:
    for name, env in PROVIDER_ENV:
        if os.environ.get(env):
            return name
    return "demo"


def has_live_model() -> bool:
    return provider_name() != "demo" and requests is not None


def configured_providers() -> list[str]:
    """Every provider that has a key set, in failover order."""
    out: list[str] = []
    for name, env in PROVIDER_ENV:
        if os.environ.get(env) and name not in out:
            out.append(name)
    return out


def probe(provider: str) -> dict:
    """Send one tiny request to a provider and report whether it answers.

    Walks the same candidate models that `complete()` would use, so a single
    busy model does not make a healthy provider look dead. Never raises --
    the error is part of the payload.
    """
    if provider not in {n for n, _ in PROVIDER_ENV}:
        return {"provider": provider, "ok": False, "error": "unknown provider"}
    if not any(os.environ.get(e) for n, e in PROVIDER_ENV if n == provider):
        return {"provider": provider, "ok": False, "error": "no key configured"}
    if requests is None:
        return {"provider": provider, "ok": False, "error": "requests not installed"}
    models = models_for(provider)
    last = "no model tried"
    for model in models:
        try:
            text = _invoke(provider, model, "You are a health check.",
                           "Reply with the single word OK.", None, "image/jpeg", 20)
            return {"provider": provider, "model": model, "ok": True,
                    "sample": (text or "").strip()[:40]}
        except _Retryable as exc:
            # Report the *first* failure: it is the primary model's reason and
            # the one the operator cares about, not a fallback's stale 404.
            if last == "no model tried":
                last = f"{model}: unavailable ({exc})"
            continue
        except Exception as exc:  # noqa: BLE001
            return {"provider": provider, "model": model, "ok": False, "error": str(exc)[:160]}
    return {"provider": provider, "model": models[0], "ok": False, "error": last}


_probe_cache: dict = {"at": 0.0, "rows": []}
_PROBE_TTL = 30.0


def probe_all() -> list[dict]:
    """Probe every configured provider, cached for a short window.

    Each call spends a real request against the provider's quota, so the result
    is cached: a public caller cannot turn this into a quota-drain or an
    outbound-request amplifier.
    """
    now = time.time()
    if _probe_cache["rows"] and now - _probe_cache["at"] < _PROBE_TTL:
        return _probe_cache["rows"]
    rows = [probe(p) for p in configured_providers()]
    _probe_cache["at"] = now
    _probe_cache["rows"] = rows
    return rows


def model_name() -> str:
    p = provider_name()
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(p, "demo")


def gemini_models() -> list[str]:
    """Model candidates for Gemini, best first, honouring overrides."""
    override = os.environ.get("GEMINI_MODEL") or os.environ.get("LLM_MODEL")
    if override:
        return [override]
    return list(GEMINI_MODELS)


def groq_models() -> list[str]:
    """Model candidates for Groq (vision-capable only)."""
    override = os.environ.get("GROQ_MODEL") or os.environ.get("LLM_MODEL")
    if override:
        return [override]
    return list(GROQ_MODELS)


def models_for(provider: str) -> list[str]:
    if provider == "gemini":
        return gemini_models()
    if provider == "groq":
        return groq_models()
    return [os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(provider, "?")]


def complete(system: str, user: str, image_b64: str | None = None,
             mime: str = "image/jpeg", max_tokens: int = 1400) -> str:
    """Run one completion, failing over between models/providers.

    A 404 (retired model) or 503 (overloaded) on one provider should not take
    the whole app down -- we try the next candidate and only give up when
    everything is exhausted, so the caller can fall back to DEMO.
    """
    if not has_live_model():
        raise RuntimeError("no live model configured")

    errors: list[str] = []
    for provider, model in _candidates():
        try:
            return _invoke(provider, model, system, user, image_b64, mime, max_tokens)
        except _Retryable as exc:
            errors.append(f"{provider}/{model}: {exc}")
            continue
    raise RuntimeError("all providers failed -> " + " | ".join(errors))


def _candidates():
    """(provider, model) pairs to try, in order."""
    seen = set()
    for name, env in PROVIDER_ENV:
        if not os.environ.get(env):
            continue
        for m in models_for(name):
            if (name, m) not in seen:
                seen.add((name, m))
                yield name, m


class _Retryable(RuntimeError):
    """A provider/model failure that a different candidate might survive."""


def _invoke(provider, model, system, user, image_b64, mime, max_tokens):
    try:
        if provider in COMPATIBLE:
            return _compatible(provider, model, system, user, image_b64, mime, max_tokens)
        if provider == "anthropic":
            return _anthropic(model, system, user, image_b64, mime, max_tokens)
        return _gemini(model, system, user, image_b64, mime, max_tokens)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code in RETRYABLE:
            raise _Retryable(f"HTTP {code}") from exc
        raise


def _compatible(provider, model, system, user, image_b64, mime, max_tokens):
    """Any OpenAI-compatible chat endpoint (OpenAI, Groq, OpenRouter, ...)."""
    default_base, env = COMPATIBLE[provider]
    key = os.environ[env]
    base = os.environ.get(f"{provider.upper()}_BASE_URL", default_base).rstrip("/")
    content: list = [{"type": "text", "text": user}]
    if image_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"}})
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        # OpenRouter attributes traffic via these optional headers.
        headers["HTTP-Referer"] = os.environ.get("OPENROUTER_SITE", "https://example.com")
        headers["X-Title"] = config.BRAND_NAME
    r = requests.post(
        f"{base}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": _clamp(provider, max_tokens),
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _anthropic(model, system, user, image_b64, mime, max_tokens):
    key = os.environ["ANTHROPIC_API_KEY"]
    content: list = []
    if image_b64:
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": image_b64}})
    content.append({"type": "text", "text": user})
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": content}]},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def _gemini(model, system, user, image_b64, mime, max_tokens):
    key = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    parts: list = [{"text": user}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": mime, "data": image_b64}})
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    gen = {"temperature": 0.2, "maxOutputTokens": max_tokens}
    # Newer Gemini models "think" before answering; cap it so the visible answer
    # is not starved of tokens (which shows up as an empty part).
    if "2.5" in model or "3" in model:
        gen["thinkingConfig"] = {"thinkingBudget": int(os.environ.get("GEMINI_THINKING", "0"))}
    r = requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"system_instruction": {"parts": [{"text": system}]},
              "contents": [{"parts": parts}],
              "generationConfig": gen},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    cands = data.get("candidates") or []
    parts_out = (cands[0].get("content") or {}).get("parts") if cands else None
    if not parts_out:
        raise RuntimeError(f"empty response from {model}: {str(data)[:200]}")
    return parts_out[0].get("text", "")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating prose/fences.

    Handles the common failure where a model runs out of output tokens mid-JSON:
    we salvage the largest balanced ``{...}`` prefix and close any dangling
    string/bracket so a truncated reply still yields the fields it managed to
    emit, instead of throwing the whole analysis away.
    """
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # First *balanced* object, so a valid answer followed by a chatty remark
    # (which may itself contain braces) still parses.
    obj = _first_object(text)
    if obj:
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start == -1:
        return {}
    salvaged = _salvage(text[start:])
    if salvaged:
        try:
            return json.loads(salvaged)
        except json.JSONDecodeError:
            pass
    return {}


def _first_object(text: str) -> str | None:
    """The substring of the first brace-balanced object, or None."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _salvage(fragment: str) -> str:
    """Best-effort repair of a JSON object truncated mid-stream."""
    out: list[str] = []
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in fragment:
        out.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:
        out.append('"')
    # Drop a trailing partial token (e.g. `"key":` or `"key": "val`) so the
    # repaired object still parses.
    text = "".join(out).rstrip()
    while text and text[-1] in ",:":
        text = text[:-1].rstrip()
    # A key with no value yet (`...,"confidence"`) is not valid JSON: drop it.
    while True:
        m = re.search(r'[{,]\s*"(?:[^"\\]|\\.)*"\s*$', text)
        if not m:
            break
        text = text[:m.start()].rstrip()
        while text and text[-1] in ",:":
            text = text[:-1].rstrip()
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text
