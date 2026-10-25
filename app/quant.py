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

# The broker's smallest tradable size and the volatility multiple for the stop.
MIN_LOT = 0.01
ATR_STOP_MULT = 1.5      # structural (volatility) stop
MIN_STOP_ATR = 1.0       # never place a stop tighter than this: it is noise
STRUCTURE_BUFFER_ATR = 0.25  # pad a level so normal wick noise cannot tag it


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


def _nearest_level(levels, price: float, side: str) -> float | None:
    """The closest swing level on the protective side of `price`.

    For a long the stop goes *below* support, for a short *above* resistance,
    so we pick the nearest level strictly on that side and ignore anything that
    is not (a "support" above price is really resistance -- `swing_levels`
    already filters this, but a caller could pass anything).
    """
    if not levels:
        return None
    if side == "BUY":
        below = [x for x in levels if x < price]
        return max(below) if below else None
    above = [x for x in levels if x > price]
    return min(above) if above else None


def plan(s: dict, decision: dict, meta: dict, rr: tuple = (1.0, 2.0, 3.0),
         account: float = 200.0, risk_pct: float = 1.0) -> dict:
    """Entry, stop and three targets.

    The stop sits *beyond a structural level* when one is available: under the
    nearest support for a long, over the nearest resistance for a short, padded
    by a small ATR buffer so an ordinary wick cannot tag it. That is where the
    market itself says the idea is wrong, and it is more honest than a fixed
    multiple of ATR. Two floors still apply: 1x ATR (beyond noise) and, when the
    account is small, the widest distance that keeps a 0.01-lot trade inside
    ``risk_pct`` of ``account``. If the level lies further than the risk budget
    allows, the stop is pulled to the budget (``stop_mode="risk"``) and the
    level-based distance is still reported in ``structure_stop`` /
    ``structure_stop_pips`` so the caller can say what was given up.

    Targets stay anchored to volatility, so the risk:reward stays honest.
    When the account is too small to fund a sane stop at 0.01 lots, the stop is
    floored and the trade is flagged ``feasible=False`` with the balance that
    *would* fit -- never a silent 20%-risk trade.
    """
    price = s["price"]
    atr = s["atr14"] or price * 0.002
    digits = meta["digits"]
    pip = meta["pip"]
    pip_value = meta["pip_value"]            # $ per pip per 1.0 lot
    per_price = pip_value / pip              # $ per 1.0 price unit per 1.0 lot
    side = decision["side"]

    vol_stop = ATR_STOP_MULT * atr
    floor_stop = MIN_STOP_ATR * atr
    risk_money = max(0.0, account) * max(0.0, risk_pct) / 100
    budget_stop = (risk_money / (MIN_LOT * per_price)) if per_price > 0 else vol_stop

    # Structural stop: just past the level that would prove the idea wrong.
    levels = s.get("support") if side == "BUY" else s.get("resistance")
    level = _nearest_level(levels, price, side)
    buffer = STRUCTURE_BUFFER_ATR * atr
    if level is not None:
        structure_dist = abs(price - level) + buffer
    else:
        structure_dist = vol_stop
    # Structure is preferred over a bare ATR multiple, but never tighter than
    # the noise floor and never wider than the risk budget will fund.
    stop_dist = min(structure_dist, max(floor_stop, budget_stop))
    feasible = budget_stop >= floor_stop

    # Balance needed to fund each stop at 0.01 lots within the risk budget.
    _lot_pip = MIN_LOT * pip_value
    min_account = (round(floor_stop / pip * _lot_pip / (risk_pct / 100))
                   if risk_pct else None)
    min_account_vol = (round(vol_stop / pip * _lot_pip / (risk_pct / 100))
                       if risk_pct else None)

    base = {
        "atr": atr,
        "digits": digits,
        "atr_stop": round(vol_stop, digits),
        "atr_stop_pips": round(vol_stop / pip, 1),
        "vol_stop_pips": round(vol_stop / pip, 1),
        "floor_stop_pips": round(floor_stop / pip, 1),
        "max_risk_money": round(risk_money, 2),
        "min_account_for_floor_stop": min_account,
        "min_account_for_vol_stop": min_account_vol,
    }
    if decision["verdict"] == "WAIT":
        return {**base, "entry": None, "stop": None, "targets": None, "rr": None,
                "stop_pips": None, "risk_capped": False, "stop_mode": None,
                "structure_stop": None, "structure_stop_pips": None,
                "feasible": feasible}

    sign = 1 if side == "BUY" else -1
    stop = price - sign * stop_dist
    targets = [price + sign * m * vol_stop for m in rr]
    risk = abs(price - stop)
    reward = abs(targets[1] - price)
    risk_capped = stop_dist < structure_dist
    # "structure" when the level set the stop, "volatility" when ATR did (no
    # level), "risk" when the risk budget pulled it in.
    if risk_capped:
        stop_mode = "risk"
    elif level is not None:
        stop_mode = "structure"
    else:
        stop_mode = "volatility"

    return {
        **base,
        "entry": round(price, digits),
        "stop": round(stop, digits),
        "targets": [round(t, digits) for t in targets],
        "stop_pips": round(risk / pip, 1),
        "rr": round(reward / risk, 2) if risk else None,
        "risk_capped": bool(risk_capped),
        "stop_mode": stop_mode,
        "structure_stop": round(price - sign * structure_dist, digits),
        "structure_stop_pips": round(structure_dist / pip, 1),
        "feasible": feasible,
    }


def size_position(account: float, risk_pct: float, trade: dict, meta: dict) -> dict:
    """Lots for a given account balance and risk percentage.

    ``plan`` already keeps the stop sane and flags ``feasible``; here we report
    the size and, when the account cannot fund that stop at 0.01 lots, say
    plainly that the minimum lot would risk too much and how much balance the
    setup actually needs -- instead of hiding a 10%+ risk behind a normal-looking
    "0.01".
    """
    risk_money = account * risk_pct / 100
    stop_pips = trade.get("stop_pips")
    pip_value = meta["pip_value"]  # $ per pip per 1.0 lot
    if not stop_pips or stop_pips <= 0:
        return {"lots": None, "risk_money": round(risk_money, 2),
                "stop_pips": stop_pips, "warning": None}

    raw = risk_money / (stop_pips * pip_value)
    lots = max(MIN_LOT, round(raw, 2))
    actual_risk = round(lots * stop_pips * pip_value, 2)
    actual_pct = round(100 * actual_risk / account, 2) if account else None

    warning = None
    if not trade.get("feasible", True):
        need = trade.get("min_account_for_floor_stop")
        need_txt = f"${need:,.0f}" if need else "—"
        warning = {
            "level": "high",
            "ar": (f"حسابك صغير على هذا السوق: أضيق ستوب منطقي ({stop_pips:.0f} نقطة) "
                   f"يخاطر بـ{actual_pct}% بأصغر حجم. لا تدخل — هذا السوق يحتاج "
                   f"رصيداً ≈ {need_txt} لمخاطرة {risk_pct}%."),
            "en": (f"Account too small for this market: the tightest sane stop "
                   f"({stop_pips:.0f} pips) risks {actual_pct}% at the minimum lot. "
                   f"Stand down — this market needs ~{need_txt} to risk {risk_pct}%."),
        }
    elif actual_pct and actual_pct > risk_pct * 1.5:
        warning = {
            "level": "medium",
            "ar": f"الخطر الفعلي {actual_pct}% أعلى من هدفك {risk_pct}% بسبب أصغر حجم عقد مسموح.",
            "en": f"Actual risk {actual_pct}% exceeds your {risk_pct}% target due to the minimum lot size.",
        }

    return {
        "lots": lots,
        "risk_money": round(risk_money, 2),
        "actual_risk": actual_risk,
        "actual_pct": actual_pct,
        "stop_pips": stop_pips,
        "pip_value_per_lot": pip_value,
        "min_lot_forced": raw < MIN_LOT,
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
    p = plan(snapshot, d, meta, account=account, risk_pct=risk_pct)
    sz = size_position(account, risk_pct, p, meta) if p["entry"] else {
        "lots": None, "risk_money": account * risk_pct / 100, "stop_pips": None}
    c = cost_estimate(p, meta, sz["lots"]) if sz["lots"] else {"pips": None, "money": None}
    return {"decision": d, "plan": p, "size": sz, "cost": c,
            "account": account, "risk_pct": risk_pct}
