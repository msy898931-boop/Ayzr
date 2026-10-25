"""
The analysis engine.
====================

Four capabilities, each one a thin orchestration over `llm.complete`:

    analyze  - chart analysis (11 specialist lenses + 1 chief)
    verify   - signal fraud detector
    advise   - position advisor (close / reduce / hedge / hold)
    zones    - supply & demand levels

Two modes:
    fast  - one model call that embodies all 12 agents (cheap, default)
    full  - one call per specialist, then a chief call that aggregates

When no model key is set, or a call fails, the deterministic DEMO engine
answers and the payload is tagged ``live=False``. We never label demo output
as a model's work.
"""
from __future__ import annotations

import json

import agents as roster
import config
import demo
import imaging
import llm

# The model is told the product's configured name, so a white-label deployment
# does not leak another brand into its own prompts.
def _brand(prompt: str) -> str:
    return prompt.replace("{brand}", config.BRAND_NAME)


SYSTEM_ANALYST = """You are {brand}, a professional multi-agent market analyst.
Eleven specialists examine a trading chart, then a chief analyst decides.
The specialists are:
{roster}

Rules:
- Base everything ONLY on what is visible in the chart. Never invent data.
- If the chart is unreadable or shows no clear setup, answer WAIT.
- Be explicit about uncertainty; confidence is 0-100.
- This is decision support, not financial advice.

Reply with ONE JSON object and nothing else, shaped exactly like:
{{
  "direction": "BUY" | "SELL" | "WAIT",
  "trend": "up" | "down" | "range",
  "confidence": 0-100,
  "entry": number|null,
  "stop": number|null,
  "targets": [number, number, number] | null,
  "summary_ar": "جملة عربية واحدة",
  "summary_en": "one English sentence",
  "agents": [
    {{"key": "<agent key>", "bias": "BUY"|"SELL"|"WAIT", "confidence": 0-100,
      "note_ar": "...", "note_en": "..."}}
  ]
}}
Include one entry in "agents" for every specialist key listed above."""

SYSTEM_VERIFY = """You are {brand}'s signal fraud detector. You receive an image of a
claimed trade result (a chart plus text). Decide whether the claim is credible.
Look for: cropped context, missing entry candle, prices that contradict the
candles, obviously edited pixels, screenshots of demo accounts passed as real.

Reply with ONE JSON object only:
{"verdict": "genuine"|"suspicious"|"fake", "score": 0-100,
 "reasons_ar": ["...", "..."], "reasons_en": ["...", "..."]}"""

SYSTEM_ADVISE = """You are {brand}'s position advisor. Given a chart and the user's
position details, give one clear instruction.
Reply with ONE JSON object only:
{"action": "hold"|"reduce"|"close"|"hedge",
 "advice_ar": "جملة عربية", "advice_en": "one English sentence",
 "reasoning_en": "short why"}"""

SYSTEM_ZONES = """You are {brand}'s supply & demand mapper. Read the chart and
return the two nearest supply zones (above price) and two nearest demand zones
(below price).
Reply with ONE JSON object only:
{"supply": [n, n], "demand": [n, n], "note_ar": "...", "note_en": "..."}"""


def _merge(base: dict, data: dict) -> dict:
    """Overlay a model's JSON onto a demo skeleton, keeping the shape stable."""
    out = dict(base)
    for k, v in (data or {}).items():
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out


def _agent_meta(key: str) -> dict:
    a = roster.by_key(key) or {}
    return {"key": key, "en": a.get("en", key), "ar": a.get("ar", key)}


def _attach_meta(payload: dict) -> dict:
    fixed = []
    for a in payload.get("agents", []):
        if not isinstance(a, dict) or "key" not in a:
            continue
        fixed.append({**_agent_meta(a["key"]), **a})
    if fixed:
        payload["agents"] = fixed
    if isinstance(payload.get("chief"), dict):
        payload["chief"] = {**_agent_meta("chief"), **payload["chief"]}
    return payload


def analyze(image: bytes, mime: str, symbol: str, timeframe: str,
            mode: str = "fast", extra: str = "") -> dict:
    meta = imaging.describe(image, mime)
    fallback = demo.analyze(meta["fingerprint"])

    if not llm.has_live_model():
        return {**fallback, "image": meta}

    try:
        user = (f"Symbol: {symbol or 'unknown'}\nTimeframe: {timeframe or 'unknown'}\n"
                f"Mode: {mode}\n{extra}\nAnalyze the attached chart.")
        if mode == "full":
            return _analyze_full(image, mime, meta, fallback, symbol, timeframe)
        text = llm.complete(_brand(SYSTEM_ANALYST).format(roster=roster.lens_block()),
                            user, imaging.b64(image), mime)
        data = llm.extract_json(text)
        if not data:
            raise ValueError("no JSON in reply")
        out = _merge(fallback, data)
        out["agents"] = data.get("agents") or fallback["agents"]
        out["live"] = True
        out["provider"] = llm.provider_name()
        out["model"] = llm.model_name()
        return {**_attach_meta(out), "image": meta}
    except Exception as exc:  # noqa: BLE001 - fall back, but say so
        return {**fallback, "image": meta, "fallback_reason": str(exc)}


def _analyze_full(image, mime, meta, fallback, symbol, timeframe) -> dict:
    b64 = imaging.b64(image)
    per_agent = []
    for a in roster.AGENTS:
        sys_p = (f"You are the {a['en']} specialist on a trading desk. "
                 f"Focus ONLY on this lens: {a['lens']} "
                 'Reply with ONE JSON object only: {"bias":"BUY"|"SELL"|"WAIT",'
                 '"confidence":0-100,"note_ar":"...","note_en":"..."}')
        try:
            txt = llm.complete(sys_p, f"Symbol: {symbol} {timeframe}. Analyse.",
                               b64, mime, max_tokens=400)
            d = llm.extract_json(txt) or {}
        except Exception:  # noqa: BLE001
            d = {}
        per_agent.append({**_agent_meta(a["key"]),
                          "bias": d.get("bias", "WAIT"),
                          "confidence": d.get("confidence", 50),
                          "note_ar": d.get("note_ar", ""),
                          "note_en": d.get("note_en", "")})
    digest = json.dumps([{k: x[k] for k in ("key", "bias", "confidence", "note_en")}
                         for x in per_agent], ensure_ascii=False)
    chief_sys = ("You are the chief analyst. Eleven specialists reported. "
                 "Weigh them into one decision. Reply with ONE JSON object: "
                 '{"direction":"BUY"|"SELL"|"WAIT","trend":"up"|"down"|"range",'
                 '"confidence":0-100,"entry":n|null,"stop":n|null,'
                 '"targets":[n,n,n]|null,"summary_ar":"...","summary_en":"..."}')
    try:
        txt = llm.complete(chief_sys, f"Specialist reports:\n{digest}", b64, mime)
        data = llm.extract_json(txt) or {}
    except Exception:  # noqa: BLE001
        data = {}
    out = _merge(fallback, data)
    out["agents"] = per_agent
    direction = out.get("direction", "WAIT")
    out["agreement"] = {"of": len(per_agent),
                        "with": sum(1 for x in per_agent if x["bias"] == direction)}
    out["chief"] = {**_agent_meta("chief"),
                    "bias": direction, "confidence": out.get("confidence", 50),
                    "note_ar": out.get("summary_ar", ""),
                    "note_en": out.get("summary_en", "")}
    out["live"] = True
    out["provider"] = llm.provider_name()
    out["model"] = llm.model_name()
    return {**out, "image": meta}


def verify(image: bytes, mime: str, claim: str = "") -> dict:
    meta = imaging.describe(image, mime)
    fallback = demo.verify(meta["fingerprint"])
    if not llm.has_live_model():
        return {**fallback, "image": meta}
    try:
        user = f"Claimed result / caption: {claim or '(none)'}\nVerify this image."
        text = llm.complete(_brand(SYSTEM_VERIFY), user, imaging.b64(image), mime)
        data = llm.extract_json(text)
        if not data:
            raise ValueError("no JSON in reply")
        out = _merge(fallback, data)
        out["live"] = True
        out["provider"] = llm.provider_name()
        return {**out, "image": meta}
    except Exception as exc:  # noqa: BLE001
        return {**fallback, "image": meta, "fallback_reason": str(exc)}


def advise(image: bytes, mime: str, position: str = "") -> dict:
    meta = imaging.describe(image, mime)
    fallback = demo.advise(meta["fingerprint"])
    if not llm.has_live_model():
        return {**fallback, "image": meta}
    try:
        user = f"My position: {position or '(not specified)'}\nAdvise."
        text = llm.complete(_brand(SYSTEM_ADVISE), user, imaging.b64(image), mime)
        data = llm.extract_json(text)
        if not data:
            raise ValueError("no JSON in reply")
        out = _merge(fallback, data)
        out["live"] = True
        out["provider"] = llm.provider_name()
        return {**out, "image": meta}
    except Exception as exc:  # noqa: BLE001
        return {**fallback, "image": meta, "fallback_reason": str(exc)}


def zones(image: bytes, mime: str) -> dict:
    meta = imaging.describe(image, mime)
    fallback = demo.zones(meta["fingerprint"])
    if not llm.has_live_model():
        return {**fallback, "image": meta}
    try:
        text = llm.complete(_brand(SYSTEM_ZONES), "Map the zones on this chart.",
                            imaging.b64(image), mime)
        data = llm.extract_json(text)
        if not data:
            raise ValueError("no JSON in reply")
        out = _merge(fallback, data)
        out["live"] = True
        out["provider"] = llm.provider_name()
        return {**out, "image": meta}
    except Exception as exc:  # noqa: BLE001
        return {**fallback, "image": meta, "fallback_reason": str(exc)}


# ======================================================================
# Quant layer: real market data + transparent rules, optionally explained
# by the model. The numbers come from `market`/`quant`; the model only
# narrates them. This is what a pure vision product cannot do.
# ======================================================================

SYSTEM_EXPLAIN = """You are {brand}'s analyst. You are given a rule-based trade
plan computed from REAL market data (indicators and levels). Your job is to
explain it clearly and flag anything that looks off -- NOT to invent numbers.

Use ONLY the numbers provided. Never add prices that are not in the input.
If the setup is weak or contradictory, say so plainly.
The plan says how the stop was chosen: "stop_mode":"structure" means the stop
sits just beyond the nearest support (for a buy) or resistance (for a sell) --
the level that would prove the idea wrong; "stop_mode":"volatility" means no
level was available, so a 1.5x ATR stop was used; "stop_mode":"risk" means the
structural/volatility stop was tightened so a 0.01-lot trade fits the requested
risk %. When "stop_mode" is "risk" and "structure_stop_pips" is larger than
"stop_pips", the level is wider than the risk budget allows -- say so. When
"feasible" is false (or size.warning is set), the account is too small for this
market at that risk -- say so and give the "min_account_for_floor_stop" figure
as the balance the market needs.

Reply with ONE JSON object only:
{"summary_ar": "جملتان بالعربية", "summary_en": "two sentences in English",
 "risk_notes_ar": ["..."], "risk_notes_en": ["..."],
 "invalidation_ar": "ما الذي يُبطل هذا السيناريو", "invalidation_en": "what invalidates it"}"""


def quant_analyze(symbol: str, timeframe: str, account: float = 200.0,
                  risk_pct: float = 1.0, explain: bool = True) -> dict:
    """Full quantitative read of a live instrument."""
    import market
    import quant

    meta = market.SYMBOLS.get(symbol.upper())
    if not meta:
        raise ValueError(f"unknown symbol: {symbol}")
    ohlc = market.fetch(symbol, timeframe)
    snap = market.snapshot(ohlc)
    plan = quant.full_plan(symbol, snap, meta, account, risk_pct)

    out = {
        "symbol": symbol.upper(),
        "label": meta["label"],
        "timeframe": timeframe.upper(),
        "kind": meta["kind"],
        "digits": meta["digits"],
        "price": snap["price"],
        "last_candle": snap["last_time"],
        "indicators": {
            "rsi14": _round(snap["rsi14"], 2),
            "adx14": _round(snap["adx14"], 2),
            "atr14": _round(snap["atr14"], 6),
            "atr_pct": _round(snap["atr_pct"], 3),
            "ema20": _round(snap["ema20"], meta["digits"]),
            "ema50": _round(snap["ema50"], meta["digits"]),
            "ema200": _round(snap["ema200"], meta["digits"]),
            "macd_hist": _round((snap["macd"] or {}).get("hist"), 6),
            "stoch_k": _round((snap["stoch"] or {}).get("k"), 2),
            "volume_ratio": _round(snap["volume_ratio"], 2),
            "support": [_round(x, meta["digits"]) for x in snap["support"]],
            "resistance": [_round(x, meta["digits"]) for x in snap["resistance"]],
        },
        "decision": plan["decision"],
        "plan": plan["plan"],
        "size": plan["size"],
        "cost": plan["cost"],
        "account": account,
        "risk_pct": risk_pct,
        "data_live": True,      # real market data, always
        "spot": bool(ohlc.get("spot")),   # price anchored to the spot feed
        "spot_offset": ohlc.get("spot_offset"),
        "ai_live": False,
        "provider": "rules",
    }

    if explain and llm.has_live_model():
        try:
            payload = json.dumps({
                "symbol": out["symbol"], "timeframe": out["timeframe"],
                "price": out["price"], "indicators": out["indicators"],
                "verdict": plan["decision"]["verdict"],
                "score": plan["decision"]["score"],
                "confidence": plan["decision"]["confidence"],
                "plan": plan["plan"], "size": plan["size"], "cost": plan["cost"],
                "reasons": [{"ar": r["ar"], "en": r["en"], "met": r["met"]}
                            for r in plan["decision"]["reasons"]],
            }, ensure_ascii=False)
            text = llm.complete(_brand(SYSTEM_EXPLAIN),
                                f"Trade plan (JSON):\n{payload}\nExplain it.",
                                max_tokens=800)
            data = llm.extract_json(text)
            if data:
                out["explanation"] = data
                out["ai_live"] = True
                out["provider"] = llm.provider_name()
        except Exception as exc:  # noqa: BLE001
            out["explain_error"] = str(exc)

    return out


def quant_scan(timeframe: str = "H1", account: float = 200.0,
               risk_pct: float = 1.0, symbols: list[str] | None = None) -> dict:
    """Scan many instruments and rank the cleanest setups."""
    import market

    symbols = symbols or list(market.SYMBOLS)
    rows = []
    for sym in symbols:
        try:
            r = quant_analyze(sym, timeframe, account, risk_pct, explain=False)
            rows.append(r)
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": sym, "error": str(exc)})
    good = [r for r in rows if "error" not in r]
    good.sort(key=lambda r: (r["decision"]["verdict"] != "WAIT",
                             r["decision"]["confidence"]), reverse=True)
    return {"timeframe": timeframe.upper(), "account": account,
            "risk_pct": risk_pct, "results": good,
            "errors": [r for r in rows if "error" in r]}


def _round(v, d: int):
    return round(v, d) if isinstance(v, (int, float)) else None
