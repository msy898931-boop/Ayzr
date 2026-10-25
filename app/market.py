"""
Live market data + technical indicators (pure standard library).
================================================================

This is the layer a pure vision model cannot have: real OHLC candles pulled
from a public feed, and the classic indicators computed from them. Every number
here is reproducible and verifiable -- not a model's guess.

No numpy/pandas: the arithmetic is simple enough that keeping the app
dependency-light is worth more than the speed.
"""
from __future__ import annotations

import time

try:
    import config
except ImportError:  # pragma: no cover - config lives beside this module
    config = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketQuant/1.0)"}
TIMEOUT = 20
CACHE_TTL = 60  # seconds

# Spot feed for metals. Yahoo's XAUUSD proxy is the GC=F *futures* contract,
# which can sit several dollars away from the spot price a trader sees on their
# broker. When a spot quote is available we anchor the candle series to it, so
# the displayed price (and every level derived from it) matches the broker.
SPOT_SYMBOLS = {"XAUUSD": "XAU", "XAGUSD": "XAG"}

# friendly name -> Yahoo symbol + contract metadata for position sizing
SYMBOLS: dict[str, dict] = {
    "XAUUSD": {"yahoo": "GC=F", "label": "Gold", "pip": 0.1, "pip_value": 10.0, "digits": 2, "kind": "metal"},
    "XAGUSD": {"yahoo": "SI=F", "label": "Silver", "pip": 0.01, "pip_value": 5.0, "digits": 3, "kind": "metal"},
    "EURUSD": {"yahoo": "EURUSD=X", "label": "Euro", "pip": 0.0001, "pip_value": 10.0, "digits": 5, "kind": "fx"},
    "GBPUSD": {"yahoo": "GBPUSD=X", "label": "Cable", "pip": 0.0001, "pip_value": 10.0, "digits": 5, "kind": "fx"},
    "USDJPY": {"yahoo": "USDJPY=X", "label": "Dollar-Yen", "pip": 0.01, "pip_value": 6.4, "digits": 3, "kind": "fx"},
    "AUDUSD": {"yahoo": "AUDUSD=X", "label": "Aussie", "pip": 0.0001, "pip_value": 10.0, "digits": 5, "kind": "fx"},
    "USDCAD": {"yahoo": "USDCAD=X", "label": "Loonie", "pip": 0.0001, "pip_value": 7.4, "digits": 5, "kind": "fx"},
    "NZDUSD": {"yahoo": "NZDUSD=X", "label": "Kiwi", "pip": 0.0001, "pip_value": 10.0, "digits": 5, "kind": "fx"},
    "BTCUSD": {"yahoo": "BTC-USD", "label": "Bitcoin", "pip": 1.0, "pip_value": 1.0, "digits": 1, "kind": "crypto"},
    "ETHUSD": {"yahoo": "ETH-USD", "label": "Ethereum", "pip": 0.1, "pip_value": 0.1, "digits": 2, "kind": "crypto"},
    "US30": {"yahoo": "^DJI", "label": "Dow 30", "pip": 1.0, "pip_value": 1.0, "digits": 1, "kind": "index"},
    "NAS100": {"yahoo": "^NDX", "label": "Nasdaq 100", "pip": 1.0, "pip_value": 1.0, "digits": 1, "kind": "index"},
    "SPX500": {"yahoo": "^GSPC", "label": "S&P 500", "pip": 1.0, "pip_value": 1.0, "digits": 1, "kind": "index"},
    "GER40": {"yahoo": "^GDAXI", "label": "DAX", "pip": 1.0, "pip_value": 1.0, "digits": 1, "kind": "index"},
    "USOIL": {"yahoo": "CL=F", "label": "WTI Oil", "pip": 0.01, "pip_value": 10.0, "digits": 2, "kind": "energy"},
}

# timeframe -> (yahoo interval, range, optional resample factor)
TIMEFRAMES: dict[str, tuple] = {
    "M5": ("5m", "5d", 1),
    "M15": ("15m", "1mo", 1),
    "M30": ("30m", "1mo", 1),
    "H1": ("60m", "3mo", 1),
    "H4": ("60m", "3mo", 4),
    "D1": ("1d", "2y", 1),
}

_cache: dict[str, tuple[float, dict]] = {}
_spot_cache: dict[str, tuple[float, float]] = {}


class MarketError(RuntimeError):
    pass


def spot(symbol: str) -> float | None:
    """The live spot price for a metal, or None if unavailable.

    Best-effort and never raises: if the feed is down we fall back to the Yahoo
    series, which is still real data -- just the futures contract rather than
    spot. Cached for ``GOLD_API_TTL`` seconds.
    """
    if requests is None or config is None:
        return None
    code = SPOT_SYMBOLS.get(symbol.upper())
    url_tpl = getattr(config, "GOLD_API_URL", "")
    if not code or not url_tpl:
        return None
    hit = _spot_cache.get(symbol.upper())
    ttl = getattr(config, "GOLD_API_TTL", 60)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        r = requests.get(url_tpl.format(sym=code), headers=HEADERS, timeout=8)
        r.raise_for_status()
        price = float(r.json()["price"])
    except Exception:  # noqa: BLE001 - a spot miss must never break the fetch
        return None
    if price <= 0:
        return None
    _spot_cache[symbol.upper()] = (time.time(), price)
    return price


def _shift_to_spot(ohlc: dict, target: float) -> dict:
    """Translate the candle series so its last close equals ``target``.

    Shifting every bar by the same offset preserves all candle *shape* -- gaps,
    ranges, highs/lows -- so the indicators stay exactly as valid as before,
    while the price the user sees matches the spot feed. Returns a new dict; the
    cached original is left untouched.
    """
    if not ohlc["close"] or not target:
        return ohlc
    delta = target - ohlc["close"][-1]
    if abs(delta) < 1e-12:
        return ohlc
    out = dict(ohlc)
    for k in ("open", "high", "low", "close"):
        out[k] = [round(x + delta, 6) for x in ohlc[k]]
    out["spot"] = True
    out["spot_offset"] = round(delta, 6)
    return out


def _resample(ohlc: dict, factor: int) -> dict:
    """Aggregate every `factor` candles (e.g. 1h -> 4h)."""
    if factor <= 1:
        return ohlc
    out = {"time": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    n = len(ohlc["close"])
    for start in range(0, n, factor):
        end = min(start + factor, n)
        highs = ohlc["high"][start:end]
        lows = ohlc["low"][start:end]
        out["time"].append(ohlc["time"][start])
        out["open"].append(ohlc["open"][start])
        out["high"].append(max(highs))
        out["low"].append(min(lows))
        out["close"].append(ohlc["close"][end - 1])
        out["volume"].append(sum(ohlc["volume"][start:end]))
    return out


def fetch(symbol: str, timeframe: str = "H1", bars: int = 300) -> dict:
    """Return OHLCV for a friendly symbol. Cached for CACHE_TTL seconds.

    For metals the raw Yahoo series (a futures contract) is cached, and the spot
    price is applied *on return* so a fresh spot quote is reflected immediately
    without refetching the candles.
    """
    key = f"{symbol.upper()}|{timeframe.upper()}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return _apply_spot(symbol, hit[1])

    meta = SYMBOLS.get(symbol.upper())
    if not meta:
        raise MarketError("unknown symbol")
    tf = TIMEFRAMES.get(timeframe.upper())
    if not tf:
        raise MarketError("unknown timeframe")
    if requests is None:
        raise MarketError("requests is not installed")

    interval, rng, factor = tf
    try:
        r = requests.get(YAHOO.format(sym=meta["yahoo"]),
                         params={"interval": interval, "range": rng},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
    except Exception as exc:  # noqa: BLE001
        raise MarketError(f"data fetch failed: {exc}") from exc

    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    ohlc = {"time": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None or q["high"][i] is None or q["low"][i] is None:
            continue  # skip incomplete candles
        ohlc["time"].append(t)
        ohlc["open"].append(q["open"][i] if q["open"][i] is not None else c)
        ohlc["high"].append(q["high"][i])
        ohlc["low"].append(q["low"][i])
        ohlc["close"].append(c)
        ohlc["volume"].append(q["volume"][i] or 0)

    if not ohlc["close"]:
        raise MarketError("no usable candles returned")
    ohlc = _resample(ohlc, factor)
    if bars:
        ohlc = {k: v[-bars:] for k, v in ohlc.items()}
    ohlc["symbol"] = symbol.upper()
    ohlc["timeframe"] = timeframe.upper()
    ohlc["label"] = meta["label"]
    _cache[key] = (time.time(), ohlc)
    return _apply_spot(symbol, ohlc)


def _apply_spot(symbol: str, ohlc: dict) -> dict:
    """Anchor a metal's series to the live spot price when one is available."""
    if symbol.upper() not in SPOT_SYMBOLS:
        return ohlc
    px = spot(symbol)
    return _shift_to_spot(ohlc, px) if px else ohlc


# ------------------------------------------------------------- indicators
def sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema_series(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def ema(vals: list[float], n: int) -> float | None:
    s = ema_series(vals, n)
    return s[-1] if s else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0 and ag == 0:
        return 50.0  # flat market: no momentum either way, not extreme
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    line = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(ef, es, strict=True)]
    clean = [v for v in line if v is not None]
    if len(clean) < signal:
        return None
    sig = [v for v in ema_series(clean, signal) if v is not None]
    if not sig:
        return None
    m, s = clean[-1], sig[-1]
    return {"macd": m, "signal": s, "hist": m - s}


def atr(highs, lows, closes, n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def bollinger(closes: list[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return {"mid": m, "upper": m + k * sd, "lower": m - k * sd,
            "width": (2 * k * sd / m) if m else 0.0}


def adx(highs, lows, closes, n: int = 14) -> float | None:
    if len(closes) < 2 * n:
        return None
    tr, pdm, ndm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))

    def wilder(vals):
        s = sum(vals[:n])
        out = [s]
        for v in vals[n:]:
            s = s - s / n + v
            out.append(s)
        return out

    trs, pds, nds = wilder(tr), wilder(pdm), wilder(ndm)
    dxs = []
    for i in range(len(trs)):
        if trs[i] == 0:
            continue
        pdi, ndi = 100 * pds[i] / trs[i], 100 * nds[i] / trs[i]
        tot = pdi + ndi
        if tot:
            dxs.append(100 * abs(pdi - ndi) / tot)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def stochastic(highs, lows, closes, k: int = 14, d: int = 3):
    if len(closes) < k + d:
        return None
    ks = []
    for i in range(k - 1, len(closes)):
        hh = max(highs[i - k + 1:i + 1])
        ll = min(lows[i - k + 1:i + 1])
        ks.append(100 * (closes[i] - ll) / (hh - ll) if hh > ll else 50.0)
    return {"k": ks[-1], "d": sum(ks[-d:]) / d}


def swing_levels(highs, lows, price: float, lookback: int = 5, count: int = 3):
    """Recent pivot highs (resistance) and pivot lows (support), nearest first.

    Levels on the wrong side of price are dropped: a "support" above the market
    is really resistance, and showing it as support would be misleading.
    """
    ph, pl = [], []
    for i in range(lookback, len(highs) - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h):
            ph.append(highs[i])
        if lows[i] == min(window_l):
            pl.append(lows[i])
    resistance = sorted([x for x in ph if x > price])[:count]      # nearest above
    support = sorted([x for x in pl if x < price], reverse=True)[:count]  # nearest below
    return resistance, support


def snapshot(ohlc: dict) -> dict:
    """Every indicator we use, at the latest bar."""
    c, h, low, v = ohlc["close"], ohlc["high"], ohlc["low"], ohlc["volume"]
    price = c[-1]
    a = atr(h, low, c, 14)
    resistance, support = swing_levels(h, low, price)
    vol_avg = sma(v, 20)
    return {
        "price": price,
        "ema20": ema(c, 20), "ema50": ema(c, 50), "ema200": ema(c, 200),
        "rsi14": rsi(c, 14),
        "macd": macd(c),
        "atr14": a,
        "atr_pct": (a / price * 100) if (a and price) else None,
        "bollinger": bollinger(c),
        "adx14": adx(h, low, c, 14),
        "stoch": stochastic(h, low, c),
        "resistance": resistance,
        "support": support,
        "volume": v[-1],
        "volume_avg20": vol_avg,
        "volume_ratio": (v[-1] / vol_avg) if vol_avg else None,
        "bars": len(c),
        "last_time": ohlc["time"][-1] if ohlc["time"] else None,
    }
