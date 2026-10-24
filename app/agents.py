"""
The 12-agent analysis roster.
=============================

Eleven specialists look at a chart from different angles; a twelfth ("chief")
weighs them into one verdict. Each agent is a prompt lens -- in FAST mode all
lenses are described in a single model call, in FULL mode each specialist is
called on its own and the chief then aggregates.

Keeping the roster as data (not code) means the UI, the prompts and the demo
engine all stay in sync from one source.
"""
from __future__ import annotations

AGENTS: list[dict] = [
    {
        "key": "price_action", "en": "Price Action", "ar": "حركة السعر",
        "lens": ("Read raw price action: structure of highs/lows, breakouts, "
                 "ranges, false breaks. Ignore indicators."),
    },
    {
        "key": "trend", "en": "Trend & Structure", "ar": "الاتجاه والهيكل",
        "lens": ("Classify the trend (up/down/range) using swings and moving "
                 "averages; note where structure would be invalidated."),
    },
    {
        "key": "momentum", "en": "Momentum", "ar": "الزخم",
        "lens": ("Judge momentum from RSI/MACD-style behaviour: divergence, "
                 "overbought/oversold, acceleration or exhaustion."),
    },
    {
        "key": "volume", "en": "Volume / VSA", "ar": "الفوليوم / VSA",
        "lens": ("Assess volume behaviour: climax, absorption, effort vs result, "
                 "whether moves are backed by participation."),
    },
    {
        "key": "smart_money", "en": "Smart Money", "ar": "أموال المؤسسات",
        "lens": ("Look for liquidity sweeps, stop hunts, order blocks and "
                 "imbalances that suggest institutional intent."),
    },
    {
        "key": "supply_demand", "en": "Supply & Demand", "ar": "العرض والطلب",
        "lens": ("Identify the nearest fresh supply and demand zones and how "
                 "price is likely to react at each."),
    },
    {
        "key": "candlestick", "en": "Candlestick Patterns", "ar": "أنماط الشموع",
        "lens": ("Name the relevant candlestick formations (engulfing, pin, "
                 "inside bar, doji) and their reliability in context."),
    },
    {
        "key": "volatility", "en": "Volatility", "ar": "التقلب",
        "lens": ("Assess ATR-style volatility: expansion or contraction, and "
                 "what it implies for stop distance and targets."),
    },
    {
        "key": "mtf", "en": "Multi-Timeframe", "ar": "الفريمات المتعددة",
        "lens": ("Reconcile the timeframes shown: is the higher timeframe "
                 "agreeing with the lower one or fighting it?"),
    },
    {
        "key": "risk", "en": "Risk & Position", "ar": "المخاطرة والحجم",
        "lens": ("Translate the setup into risk terms: logical stop, invalidation "
                 "level, reward-to-risk, and position-size caution."),
    },
    {
        "key": "sentiment", "en": "Sentiment & Context", "ar": "المزاج والسياق",
        "lens": ("Consider session timing, nearby news risk and overall market "
                 "sentiment that could distort the technical read."),
    },
]

CHIEF = {
    "key": "chief", "en": "Chief Analyst", "ar": "المحلّل الرئيسي",
    "lens": ("Weigh every specialist's view into a single decision: direction, "
             "entry zone, stop, targets, confidence, and what would change it."),
}

ALL_AGENTS = AGENTS + [CHIEF]


def by_key(key: str) -> dict | None:
    return next((a for a in ALL_AGENTS if a["key"] == key), None)


def roster() -> list[dict]:
    return [{"key": a["key"], "en": a["en"], "ar": a["ar"]} for a in ALL_AGENTS]


def lens_block() -> str:
    lines = [f"- {a['en']} ({a['ar']}): {a['lens']}" for a in AGENTS]
    return "\n".join(lines)
