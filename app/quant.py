"""
Quant decision engine.
======================

Turns the indicator snapshot into a transparent, rules-based trade plan:

    verdict  - BUY / SELL / WAIT with a confluence score and the reasons
    plan     - entry, stop (ATR-based), three targets, risk:reward
    size     - position size for the account, in lots
    costs    - round-turn cost in pips and in money

Everything is deterministic and every reason is printed, so a user can audit
*why* the engine said what it said. The AI layer sits on top of this; it never
invents numbers -- it explains these.
"""
from __future__ import annotations

# round-turn cost in pips by symbol kind (spread in+out + commission),
# rough Exness-Standard-like averages. Override per-symbol if needed.
COST_PIPS = {
    "metal": 2.0, "fx": 1.4, "crypto": 30.0, "index": 1.5, "energy": 3.0,
}


def _score_side(s: dict, side: str) -> tuple[int, list[dict]]:
    """Confluence score for one direction, plus the reasons behind it."""
    score = 0
    reasons: list[dict] = []
    price = s["price"]

    def add(points: int, ar: str, en: str, ok: bool):
        nonlocal score
        if ok:
            score += points
        reasons.append({"points": points if ok else 0, "max": points,
                        "ar": ar, "en": en, "met": bool(ok)})

    up = side == "BUY"

    # trend alignment via EMAs
    if s["ema50"] and s["ema200"]:
        aligned = (price > s["ema50"] > s["ema200"]) if up else (price < s["ema50"] < s["ema200"])
        add(25, "السعر مع اتجاه المتوسطات (50 و200)", "Price aligned with EMA50/EMA200 trend", bool(aligned))
    if s["ema20"] and s["ema50"]:
        short = (s["ema20"] > s["ema50"]) if up else (s["ema20"] < s["ema50"])
        add(15, "تقاطع المتوسطين القصيرين يدعم الاتجاه", "Short EMAs crossing in your favour", bool(short))

    # RSI
    r = s["rsi14"]
    if r is not None:
        if up:
            add(15, "RSI فوق 50 (زخم صاعد) بدون تشبّع", "RSI above 50, not overbought", 50 < r <= 68)
        else:
            add(15, "RSI تحت 50 (زخم هابط) بدون تشبّع", "RSI below 50, not oversold", 32 <= r < 50)

    # MACD
    m = s["macd"]
    if m:
        hist = m["hist"]
        # A zero histogram is indecision; it must not count as bearish.
        add(15, "هستوغرام MACD يدعم الاتجاه", "MACD histogram agrees",
            (hist > 0) if up else (hist < 0))

    # ADX strength
    if s["adx14"] is not None:
        add(10, "اتجاه قوي (ADX>25)", "Trending (ADX>25)", s["adx14"] > 25)

    # Bollinger position
    b = s["bollinger"]
    if b:
        pos_ok = (price > b["mid"]) if up else (price < b["mid"])
        add(10, "السعر في النصف الصحيح من بولنجر", "Price on the right side of the Bollinger mid", bool(pos_ok))

    # volume confirmation
    if s["volume_ratio"] is not None:
        add(10, "الفوليوم فوق المتوسط (تأكيد)", "Above-average volume (confirmation)", s["volume_ratio"] > 1.05)

    return score, reasons


def decide(s: dict) -> dict:
    """Pick the higher-scoring side, or WAIT when neither is convincing."""
    buy_score, buy_reasons = _score_side(s, "BUY")
    sell_score, sell_reasons = _score_side(s, "SELL")

    if buy_score >= sell_score:
        side, score, reasons = "BUY", buy_score, buy_reasons
    else:
        side, score, reasons = "SELL", sell_score, sell_reasons

    max_score = sum(r["max"] for r in reasons) or 100
    confidence = round(100 * score / max_score)
    verdict = side if (score >= 55 and confidence >= 50) else "WAIT"

    return {
        "side": side,
        "verdict": verdict,
        "score": score,
        "max_score": max_score,
        "confidence": confidence,
        "reasons": reasons,
        "buy_score": buy_score,
        "sell_score": sell_score,
    }


def plan(s: dict, decision: dict, meta: dict, rr: tuple = (1.0, 2.0, 3.0)) -> dict:
    """Entry, ATR-based stop, three targets, risk:reward."""
    price = s["price"]
    atr = s["atr14"] or price * 0.002
    side = decision["side"]
    if decision["verdict"] == "WAIT":
        return {"entry": None, "stop": None, "targets": None, "rr": None,
                "atr": atr, "stop_pips": None}

    sign = 1 if side == "BUY" else -1
    stop = price - sign * 1.5 * atr
    targets = [price + sign * m * 1.5 * atr for m in rr]
    risk = abs(price - stop)
    reward = abs(targets[1] - price)
    pip = meta["pip"]
    digits = meta["digits"]
    return {
        "entry": round(price, digits),
        "stop": round(stop, digits),
        "targets": [round(t, digits) for t in targets],
        "stop_pips": round(risk / pip, 1),
        "rr": round(reward / risk, 2) if risk else None,
        "atr": atr,
        "digits": digits,
    }


def size_position(account: float, risk_pct: float, trade: dict, meta: dict) -> dict:
    """Lots for a given account balance and risk percentage.

    Brokers have a minimum lot (usually 0.01). When that floor forces more risk
    than the user asked for, we say so plainly instead of hiding it -- on a
    small account a wide stop can make a "1%" trade impossible.
    """
    risk_money = account * risk_pct / 100
    stop_pips = trade.get("stop_pips")
    pip_value = meta["pip_value"]  # $ per pip per 1.0 lot
    if not stop_pips or stop_pips <= 0:
        return {"lots": None, "risk_money": round(risk_money, 2),
                "stop_pips": stop_pips, "warning": None}

    raw = risk_money / (stop_pips * pip_value)
    min_lot = 0.01
    lots = max(min_lot, round(raw, 2))
    actual_risk = round(lots * stop_pips * pip_value, 2)
    actual_pct = round(100 * actual_risk / account, 2) if account else None

    warning = None
    if raw < min_lot:
        warning = {
            "level": "high",
            "ar": (f"الستوب واسع ({stop_pips:.0f} نقطة) — أصغر حجم (0.01 لوت) "
                   f"يخاطر بـ{actual_pct}% من حسابك، أكثر من الحد المطلوب {risk_pct}%."),
            "en": (f"Wide stop ({stop_pips:.0f} pips) — the minimum 0.01 lot risks "
                   f"{actual_pct}% of your account, above your {risk_pct}% limit."),
        }
    elif actual_pct and actual_pct > risk_pct * 1.5:
        warning = {
            "level": "medium",
            "ar": f"الخطر الفعلي {actual_pct}% أعلى من هدفك {risk_pct}% بسبب تقريب الحجم.",
            "en": f"Actual risk {actual_pct}% exceeds your {risk_pct}% target due to lot rounding.",
        }

    return {
        "lots": lots,
        "risk_money": round(risk_money, 2),
        "actual_risk": actual_risk,
        "actual_pct": actual_pct,
        "stop_pips": stop_pips,
        "pip_value_per_lot": pip_value,
        "min_lot_forced": raw < min_lot,
        "warning": warning,
    }


def cost_estimate(trade: dict, meta: dict, lots: float) -> dict:
    """Round-turn cost in pips and money for the planned size."""
    if not trade.get("stop_pips"):
        return {"pips": None, "money": None}
    pips = COST_PIPS.get(meta["kind"], 1.5)
    money = round(pips * meta["pip_value"] * lots, 2)
    # cost as a fraction of the risk taken
    risk = trade["stop_pips"] * meta["pip_value"] * lots
    return {
        "pips": pips,
        "money": money,
        "pct_of_risk": round(100 * money / risk, 1) if risk else None,
    }


def full_plan(symbol: str, snapshot: dict, meta: dict,
              account: float = 200.0, risk_pct: float = 1.0) -> dict:
    """One call: decide, plan, size, cost."""
    d = decide(snapshot)
    p = plan(snapshot, d, meta)
    sz = size_position(account, risk_pct, p, meta) if p["entry"] else {
        "lots": None, "risk_money": account * risk_pct / 100, "stop_pips": None}
    c = cost_estimate(p, meta, sz["lots"]) if sz["lots"] else {"pips": None, "money": None}
    return {"decision": d, "plan": p, "size": sz, "cost": c,
            "account": account, "risk_pct": risk_pct}
