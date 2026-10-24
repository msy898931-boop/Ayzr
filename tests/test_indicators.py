"""Indicators: the maths has to be right, so we check it against known values."""
from __future__ import annotations


def test_sma_basic(mods):
    sma = mods["market"].sma
    assert sma([1, 2, 3, 4, 5], 5) == 3
    assert sma([2, 4, 6], 3) == 4


def test_sma_needs_enough_history(mods):
    # Not enough bars must return None, never a wrong number.
    assert mods["market"].sma([1, 2], 5) is None


def test_ema_matches_manual_recurrence(mods):
    # EMA(3) over a constant series is that constant, for any seeding.
    ema = mods["market"].ema
    assert ema([5, 5, 5, 5, 5], 3) == 5


def test_ema_series_length_and_first_value(mods):
    series = mods["market"].ema_series([1, 2, 3, 4, 5], 3)
    assert len(series) == 5
    assert series[2] == 2  # seeded with the SMA of the first three bars


def test_rsi_all_gains_is_100(mods):
    closes = list(range(1, 30))
    assert mods["market"].rsi(closes, 14) == 100


def test_rsi_all_losses_is_zero(mods):
    closes = list(range(30, 1, -1))
    assert mods["market"].rsi(closes, 14) == 0


def test_rsi_flat_series_is_neutral(mods):
    # No movement means no relative strength: we define that as 50, not a
    # division by zero.
    assert mods["market"].rsi([10] * 30, 14) == 50


def test_macd_shapes(mods):
    # A curved uptrend, not a straight line: a perfectly linear series gives
    # MACD == signal, and floating-point noise then decides the histogram sign.
    closes = [100 + (i ** 1.5) for i in range(80)]
    m = mods["market"].macd(closes)
    assert set(m) == {"macd", "signal", "hist"}
    # In an accelerating uptrend MACD sits above its signal.
    assert m["hist"] > 0


def test_bollinger_bands_bracket_the_middle(mods):
    closes = [10, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18, 17, 19, 18, 20, 19, 21]
    b = mods["market"].bollinger(closes, 20, 2.0)
    assert b["lower"] < b["mid"] < b["upper"]


def test_atr_is_positive(mods):
    highs = [10 + i for i in range(30)]
    lows = [9 + i for i in range(30)]
    closes = [9.5 + i for i in range(30)]
    a = mods["market"].atr(highs, lows, closes, 14)
    assert a is not None and a > 0


def test_adx_in_range(mods):
    highs = [10 + i for i in range(60)]
    lows = [9 + i for i in range(60)]
    closes = [9.5 + i for i in range(60)]
    a = mods["market"].adx(highs, lows, closes, 14)
    assert a is None or 0 <= a <= 100


def test_stochastic_in_range(mods):
    highs = [10 + i for i in range(40)]
    lows = [9 + i for i in range(40)]
    closes = [9.5 + i for i in range(40)]
    s = mods["market"].stochastic(highs, lows, closes)
    assert s is None or 0 <= s["k"] <= 100


def test_swing_levels_filters_wrong_side(mods):
    """Levels on the wrong side of price are dropped, not mislabelled."""
    # A repeating pattern so pivots are unambiguous: highs 14, lows 2.
    highs = [10, 14, 10, 10] * 8
    lows = [6, 6, 2, 6] * 8
    # Price below every pivot: pivots are resistance, nothing is support.
    res, sup = mods["market"].swing_levels(highs, lows, 1.0)
    assert res and all(r > 1.0 for r in res)
    assert sup == []
    # Price above every pivot: pivots are support, nothing is resistance.
    res2, sup2 = mods["market"].swing_levels(highs, lows, 99.0)
    assert res2 == []
    assert sup2 and all(s < 99.0 for s in sup2)


def test_resample_aggregates_ohlc(mods):
    ohlc = {
        "time": [1, 2, 3, 4],
        "open": [10, 11, 12, 13],
        "high": [11, 12, 13, 14],
        "low": [9, 10, 11, 12],
        "close": [10.5, 11.5, 12.5, 13.5],
        "volume": [1, 2, 3, 4],
    }
    out = mods["market"]._resample(ohlc, 2)
    assert out["high"] == [12, 14]
    assert out["low"] == [9, 11]
    assert out["close"] == [11.5, 13.5]
    assert out["volume"] == [3, 7]
