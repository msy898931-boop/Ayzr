"""
Deterministic DEMO engine.
==========================

When no vision-model key is configured the product must still demonstrate its
whole flow without ever pretending a model spoke. This module derives a
stable, plausible-looking analysis from the image fingerprint: the same chart
always yields the same answer, and every payload is tagged ``live=False`` so
the UI can show a clear "DEMO" badge.

Nothing here is a prediction. It is a structured placeholder.
"""
from __future__ import annotations

import hashlib

from agents import AGENTS, CHIEF

DIRECTIONS = ["BUY", "SELL", "WAIT"]


def _rng(seed: str, salt: str = "") -> int:
    h = hashlib.sha256((seed + "|" + salt).encode()).digest()
    return int.from_bytes(h[:4], "big")


def _pick(seed: str, salt: str, options):
    return options[_rng(seed, salt) % len(options)]


def _pct(seed: str, salt: str, lo: int, hi: int) -> int:
    return lo + _rng(seed, salt) % (hi - lo + 1)


def _price_like(seed: str, salt: str) -> float:
    return round(1.0 + (_rng(seed, salt) % 9000) / 100.0, 2)


def analyze(seed: str) -> dict:
    direction = _pick(seed, "dir", ["BUY", "SELL", "WAIT", "BUY", "SELL"])
    confidence = _pct(seed, "conf", 38, 82)
    trend = {"BUY": "up", "SELL": "down", "WAIT": "range"}[direction]
    entry = _price_like(seed, "entry")
    atr = round(entry * _pct(seed, "atr", 20, 90) / 10000, 4)
    sign = 1 if direction == "BUY" else -1

    if direction == "WAIT":
        stop = targets = None
        summary_ar = "السوق غير حاسم — لا توجد أفضليّة واضحة، الأفضل الانتظار."
        summary_en = "No clear edge here — the market is undecided, best to wait."
    else:
        stop = round(entry - sign * atr * 1.2, 4)
        targets = [round(entry + sign * atr * m, 4) for m in (1.0, 2.0, 3.0)]
        word_ar = "شراء" if direction == "BUY" else "بيع"
        summary_ar = (f"اتجاه {word_ar} مدعوم بالهيكل، مع دخول قريب من "
                      f"{entry} وستوب عند {stop}.")
        summary_en = (f"A {direction.lower()} setup backed by structure, entry "
                      f"near {entry} with a stop at {stop}.")

    agents = []
    for a in AGENTS:
        bias = direction if _rng(seed, a["key"] + "b") % 4 else "WAIT"
        agents.append({
            "key": a["key"], "en": a["en"], "ar": a["ar"],
            "bias": bias,
            "confidence": _pct(seed, a["key"] + "c", 35, 85),
            "note_ar": _note_ar(a["key"], bias),
            "note_en": _note_en(a["key"], bias),
        })

    agree = sum(1 for x in agents if x["bias"] == direction)
    return {
        "direction": direction,
        "trend": trend,
        "confidence": confidence,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "summary_ar": summary_ar,
        "summary_en": summary_en,
        "agents": agents,
        "agreement": {"of": len(agents), "with": agree},
        "chief": {
            "key": CHIEF["key"], "en": CHIEF["en"], "ar": CHIEF["ar"],
            "bias": direction, "confidence": confidence,
            "note_ar": summary_ar, "note_en": summary_en,
        },
        "live": False,
        "provider": "demo",
    }


def verify(seed: str) -> dict:
    verdict = _pick(seed, "verdict",
                    ["genuine", "genuine", "suspicious", "fake"])
    score = {"genuine": _pct(seed, "g", 68, 92),
             "suspicious": _pct(seed, "s", 40, 60),
             "fake": _pct(seed, "f", 8, 30)}[verdict]
    reasons_ar = {
        "genuine": ["الأرقام متسقة مع حركة السعر", "التوقيت منطقي مقارنة بالشموع",
                    "لا يوجد تضارب بين الصورة والنص"],
        "suspicious": ["الأسعار تبدو مقتطعة من سياق مختلف",
                       "التوقيت غامض أو غير مكتمل", "لا يمكن التحقق من الشمعة الداخلة"],
        "fake": ["الأسعار لا تطابق أي حركة سعرية معقولة",
                 "النتيجة تُعرض بدون شمعة دخول واضحة",
                 "نمط معروف لصور معدّلة أو مركّبة"],
    }[verdict]
    reasons_en = {
        "genuine": ["Figures match the visible price action",
                    "Timing is consistent with the candles",
                    "No conflict between the image and the claim"],
        "suspicious": ["Prices look cropped from a different context",
                       "Timing is vague or incomplete",
                       "The entry candle cannot be verified"],
        "fake": ["Prices do not match any plausible price action",
                 "The result is shown without a clear entry candle",
                 "Matches a known pattern of edited or composited images"],
    }[verdict]
    return {"verdict": verdict, "score": score,
            "reasons_ar": reasons_ar, "reasons_en": reasons_en,
            "live": False, "provider": "demo"}


def advise(seed: str) -> dict:
    action = _pick(seed, "act", ["hold", "reduce", "close", "hedge"])
    labels = {
        "hold": ("استمر بالصفقة مع رفع الستوب لنقطة التعادل.",
                 "Hold the position and move the stop to break-even."),
        "reduce": ("قلّل حجم الصفقة — الخطر أعلى من المكافأة حالياً.",
                   "Reduce size — risk now outweighs the reward."),
        "close": ("الأفضل إغلاق الصفقة الآن لحماية رأس المال.",
                  "Best to close now to protect capital."),
        "hedge": ("فكّر بتحوّط جزئي إذا استمر السعر بالعكس.",
                  "Consider a partial hedge if price keeps going against you."),
    }[action]
    return {"action": action, "advice_ar": labels[0], "advice_en": labels[1],
            "live": False, "provider": "demo"}


def zones(seed: str) -> dict:
    base = _price_like(seed, "zbase")
    return {
        "supply": [round(base * (1 + _pct(seed, f"s{i}", 20, 300) / 10000), 4)
                   for i in range(2)],
        "demand": [round(base * (1 - _pct(seed, f"d{i}", 20, 300) / 10000), 4)
                   for i in range(2)],
        "live": False, "provider": "demo",
    }


def _note_ar(key: str, bias: str) -> str:
    b = {"BUY": "ميل شرائي", "SELL": "ميل بيعي", "WAIT": "محايد"}[bias]
    return f"{b}: القراءة من زاوية {key} لا تدعم دخولاً قوياً بمفردها."


def _note_en(key: str, bias: str) -> str:
    label = key.replace("_", " ")
    return f"{bias.lower()} bias from the {label} lens; not a standalone trigger."
