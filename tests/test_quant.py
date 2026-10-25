"""
Quant engine: scoring, planning, sizing and cost.
=================================================

These are the numbers a trader acts on, so each one gets an explicit check.
The tests build a *fully specified* snapshot so a single flipped indicator
cannot silently change the verdict.
"""
from __future__ import annotations


def snapshot(**over) -> dict:
    """A neutral snapshot that scores 0 on both sides, overridable per test."""
    base = {
        "price": 100.0, "ema20": 100.0, "ema50": 100.0, "ema200": 100.0,
        "rsi14": 50.0, "macd": {"macd": 0, "signal": 0, "hist": 0},
        "atr14": 1.0, "atr_pct": 1.0, "bollinger": {"mid": 100, "upper": 110, "lower": 90},
        "adx14": 10.0, "stoch": {"k": 50, "d": 50},
        "resistance": [105.0], "support": [95.0],
        "volume": 100, "volume_avg20": 100, "volume_ratio": 1.0,
        "bars": 300, "last_time": 1,
    }
    base.update(over)
    return base


META = {"pip": 0.1, "pip_value": 10.0, "digits": 2, "kind": "metal"}


# ------------------------------------------------------------------ scoring
def test_neutral_snapshot_scores_zero(mods):
    d = mods["quant"].decide(snapshot())
    assert d["buy_score"] == 0 and d["sell_score"] == 0
    assert d["verdict"] == "WAIT"
    assert d["confidence"] == 0


def test_bullish_snapshot_goes_buy(mods):
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100,
                 rsi14=60, macd={"macd": 1, "signal": 0.5, "hist": 0.5},
                 adx14=30, bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    d = mods["quant"].decide(s)
    assert d["side"] == "BUY"
    assert d["verdict"] == "BUY"
    assert d["score"] == d["max_score"]  # every confluence met
    assert d["confidence"] == 100


def test_bearish_snapshot_goes_sell(mods):
    s = snapshot(price=90, ema20=92, ema50=95, ema200=100,
                 rsi14=40, macd={"macd": -1, "signal": -0.5, "hist": -0.5},
                 adx14=30, bollinger={"mid": 95, "upper": 110, "lower": 80},
                 volume=130, volume_ratio=1.3)
    d = mods["quant"].decide(s)
    assert d["side"] == "SELL"
    assert d["verdict"] == "SELL"


def test_weak_edge_stays_wait(mods):
    # Only one minor confluence: not enough to trade.
    s = snapshot(rsi14=55)
    d = mods["quant"].decide(s)
    assert d["verdict"] == "WAIT"


def test_confidence_is_bounded(mods):
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    d = mods["quant"].decide(s)
    assert 0 <= d["confidence"] <= 100


def test_reasons_are_auditable(mods):
    d = mods["quant"].decide(snapshot(price=110, ema20=108, ema50=105, ema200=100))
    assert d["reasons"]
    for r in d["reasons"]:
        assert {"points", "max", "ar", "en", "met"} <= set(r)
        assert r["points"] in (0, r["max"])


def test_missing_indicators_do_not_crash(mods):
    s = snapshot(ema50=None, ema200=None, rsi14=None, macd=None,
                 adx14=None, bollinger=None, volume_ratio=None)
    d = mods["quant"].decide(s)
    assert d["verdict"] in {"BUY", "SELL", "WAIT"}


# -------------------------------------------------------------------- plan
def test_plan_wait_has_no_levels(mods):
    q = mods["quant"]
    d = q.decide(snapshot())
    p = q.plan(snapshot(), d, META)
    assert p["entry"] is None and p["stop"] is None and p["targets"] is None


def test_plan_buy_levels_are_ordered(mods):
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    p = q.plan(s, q.decide(s), META)
    assert p["stop"] < p["entry"] < p["targets"][0] < p["targets"][1] < p["targets"][2]


def test_plan_sell_levels_are_ordered(mods):
    q = mods["quant"]
    s = snapshot(price=90, ema20=92, ema50=95, ema200=100, rsi14=40,
                 macd={"macd": -1, "signal": -0.5, "hist": -0.5}, adx14=30,
                 bollinger={"mid": 95, "upper": 110, "lower": 80},
                 volume=130, volume_ratio=1.3)
    p = q.plan(s, q.decide(s), META)
    # For a sell, targets run downward: [0] is nearest, [2] is furthest.
    assert p["stop"] > p["entry"] > p["targets"][0] > p["targets"][1] > p["targets"][2]


def test_plan_stop_uses_atr_when_account_can_afford_it(mods):
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=None, resistance=None)
    # A funded account with no level to lean on: the volatility stop wins.
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert abs((p["entry"] - p["stop"]) - 1.5 * 2.0) < 1e-6  # 1.5 * ATR
    assert p["stop_mode"] == "volatility" and p["risk_capped"] is False


def test_plan_stop_sits_beyond_support(mods):
    """A long's stop goes under the nearest support, not at a fixed 1.5x ATR."""
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=[105.0])
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert p["stop_mode"] == "structure"
    assert p["stop"] < 105.0                       # below the level
    assert abs(p["stop"] - (105.0 - 0.25 * 2.0)) < 1e-6  # level minus buffer
    assert abs(p["structure_stop"] - p["stop"]) < 1e-6   # == the stop here


def test_plan_stop_sits_beyond_resistance(mods):
    """A short's stop goes over the nearest resistance."""
    q = mods["quant"]
    s = snapshot(price=90, ema20=92, ema50=95, ema200=100, rsi14=40, atr14=2.0,
                 macd={"macd": -1, "signal": -0.5, "hist": -0.5}, adx14=30,
                 bollinger={"mid": 95, "upper": 110, "lower": 80},
                 volume=130, volume_ratio=1.3, resistance=[95.0])
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert p["stop_mode"] == "structure"
    assert p["stop"] > 95.0                        # above the level
    assert abs(p["stop"] - (95.0 + 0.25 * 2.0)) < 1e-6  # level plus buffer


def test_plan_falls_back_to_atr_without_a_level(mods):
    """No swing level on the protective side -> plain volatility stop."""
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=None)
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert p["stop_mode"] == "volatility"
    assert abs((p["entry"] - p["stop"]) - 1.5 * 2.0) < 1e-6


def test_plan_reports_the_structure_stop_it_gave_up(mods):
    """When the risk budget caps a far level, the level distance is reported."""
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=[80.0])  # far below
    p = q.plan(s, q.decide(s), META, account=250, risk_pct=1.0)
    assert p["stop_mode"] == "risk" and p["risk_capped"] is True
    assert p["structure_stop_pips"] > p["stop_pips"]   # the level was wider
    assert p["structure_stop"] < 80.0                  # level minus buffer
    assert p["stop"] > p["structure_stop"]             # capped short of it


def test_plan_caps_stop_to_the_risk_budget(mods):
    """A small account gets a tighter stop, never a 10%+ risk trade."""
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    p = q.plan(s, q.decide(s), META, account=250, risk_pct=1.0)
    dist = abs(p["entry"] - p["stop"])
    assert p["risk_capped"] is True and p["stop_mode"] == "risk"
    assert p["feasible"] is True
    assert 1.0 * 2.0 <= dist <= 1.5 * 2.0          # inside the noise floor
    assert p["rr"] >= 2.0                           # tighter stop, better RR


def test_plan_flags_an_account_too_small_for_the_noise_floor(mods):
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=2.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    p = q.plan(s, q.decide(s), META, account=50, risk_pct=1.0)
    assert p["feasible"] is False
    assert abs((p["entry"] - p["stop"]) - 2.0) < 1e-6   # floored at 1x ATR
    assert p["min_account_for_floor_stop"] > 50


def test_plan_risk_reward_is_two(mods):
    q = mods["quant"]
    # With no level to lean on and a funded account the stop is exactly the
    # volatility stop, so the second target (2x that distance) is 2R.
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=1.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=None, resistance=None)
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert p["stop_mode"] == "volatility"
    assert p["rr"] == 2.0  # second target is 2R


def test_plan_structural_stop_changes_rr(mods):
    """A wider structural stop lowers the RR honestly -- no target fiddling."""
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60, atr14=1.0,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3, support=[105.0])
    p = q.plan(s, q.decide(s), META, account=1_000_000, risk_pct=1.0)
    assert p["stop_mode"] == "structure"
    # stop is 5 + 0.25 = 5.25 wide, targets still 1x/2x/3x the 1.0 ATR vol stop
    assert p["rr"] < 2.0 and p["rr"] > 0


# -------------------------------------------------------------------- size
def test_size_hits_the_risk_budget(mods):
    q = mods["quant"]
    trade = {"stop_pips": 100.0}
    sz = q.size_position(1000.0, 1.0, trade, META)  # risk $10
    assert sz["lots"] == 0.01  # 10 / (100 * 10) = 0.01
    assert sz["actual_pct"] == 1.0


def test_size_scales_with_account(mods):
    q = mods["quant"]
    trade = {"stop_pips": 100.0}
    small = q.size_position(1000.0, 1.0, trade, META)
    big = q.size_position(100_000.0, 1.0, trade, META)
    assert big["lots"] > small["lots"]


def test_size_warns_when_min_lot_exceeds_risk(mods):
    q = mods["quant"]
    # A tiny account with a wide stop cannot honour 1% risk at 0.01 lots.
    sz = q.size_position(50.0, 1.0, {"stop_pips": 500.0}, META)
    assert sz["min_lot_forced"] is True
    assert sz["warning"]["level"] == "medium"
    assert sz["lots"] == 0.01


def test_size_stands_down_when_stop_is_infeasible(mods):
    q = mods["quant"]
    # plan() flags feasible=False; the size must say so loudly, not silently.
    sz = q.size_position(400.0, 1.0,
                         {"stop_pips": 300.0, "feasible": False,
                          "min_account_for_floor_stop": 3000}, META)
    assert sz["warning"]["level"] == "high"
    assert "3,000" in sz["warning"]["en"] and "3,000" in sz["warning"]["ar"]


def test_size_without_stop_is_safe(mods):
    sz = mods["quant"].size_position(1000.0, 1.0, {"stop_pips": None}, META)
    assert sz["lots"] is None and sz["warning"] is None


# -------------------------------------------------------------------- cost
def test_cost_uses_symbol_kind(mods):
    q = mods["quant"]
    fx = {"pip": 0.0001, "pip_value": 10.0, "digits": 5, "kind": "fx"}
    c = q.cost_estimate({"stop_pips": 20.0}, fx, 0.1)
    assert c["pips"] == q.COST_PIPS["fx"]
    assert c["money"] == round(c["pips"] * 10.0 * 0.1, 2)


def test_cost_none_without_plan(mods):
    c = mods["quant"].cost_estimate({"stop_pips": None}, META, 0.1)
    assert c["pips"] is None and c["money"] is None


# ---------------------------------------------------------------- full plan
def test_full_plan_is_consistent(mods):
    q = mods["quant"]
    s = snapshot(price=110, ema20=108, ema50=105, ema200=100, rsi14=60,
                 macd={"macd": 1, "signal": 0.5, "hist": 0.5}, adx14=30,
                 bollinger={"mid": 105, "upper": 120, "lower": 90},
                 volume=130, volume_ratio=1.3)
    out = q.full_plan("XAUUSD", s, META, account=1000, risk_pct=1.0)
    assert out["decision"]["verdict"] == "BUY"
    assert out["plan"]["entry"] is not None
    assert out["size"]["lots"] > 0
    assert out["account"] == 1000 and out["risk_pct"] == 1.0
