# -*- coding: utf-8 -*-
"""
📈 Antigravity Technical Analysis Indicators
"""
import math

def calc_ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [None] * (period - 1) + ema

def calc_sma(closes: list, period: int) -> list:
    if len(closes) < period:
        return [None] * len(closes)
    sma = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        sma.append(sum(closes[i - period + 1 : i + 1]) / period)
    return sma

def calc_std_dev(closes: list, period: int, sma: list) -> list:
    if len(closes) < period:
        return [None] * len(closes)
    std = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        m = sma[i]
        variance = sum((x - m) ** 2 for x in closes[i - period + 1 : i + 1]) / period
        std.append(variance ** 0.5)
    return std

def calc_bb(closes: list, period: int = 20, num_std: int = 2) -> dict:
    sma = calc_sma(closes, period)
    std = calc_std_dev(closes, period, sma)
    upper = []
    lower = []
    for i in range(len(closes)):
        if sma[i] is None or std[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(sma[i] + num_std * std[i])
            lower.append(sma[i] - num_std * std[i])
    return {"mid": sma, "upper": upper, "lower": lower}

def calc_rsi(closes: list, period: int = 14) -> list:
    if len(closes) <= period:
        return [None] * len(closes)
    delta = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = [d if d > 0 else 0 for d in delta]
    loss = [-d if d < 0 else 0 for d in delta]
    avg_gain = sum(gain[:period]) / period
    avg_loss = sum(loss[:period]) / period
    rsi = [100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss != 0 else 100]
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rsi.append(100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss != 0 else 100)
    return [None] * period + rsi

def calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    if len(closes) <= period:
        return [None] * len(closes)
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr = [sum(tr[:period]) / period]
    for i in range(period, len(tr)):
        atr.append((atr[-1] * (period - 1) + tr[i]) / period)
    return [None] * period + atr

def find_swings(highs: list, lows: list, lb: int = 5) -> dict:
    sh, sl = [], []
    n = len(highs)
    for i in range(lb, n - lb):
        if all(highs[j] <= highs[i] for j in range(i - lb, i + lb + 1)):
            sh.append({"i": i, "v": highs[i]})
        if all(lows[j] >= lows[i] for j in range(i - lb, i + lb + 1)):
            sl.append({"i": i, "v": lows[i]})
    return {"sh": sh, "sl": sl}

def detect_fvgs(opens: list, highs: list, lows: list, closes: list, limit=40) -> list:
    fvgs = []
    n = len(closes)
    for i in range(max(2, n - limit), n):
        # Bullish FVG
        if closes[i-1] > opens[i-1] and (closes[i-1] - opens[i-1]) > (closes[i-1] * 0.005):
            gap = lows[i] - highs[i-2]
            if gap > 0:
                fvgs.append({"type": "long", "bottom": highs[i-2], "top": lows[i], "i": i-1})
        # Bearish FVG
        elif closes[i-1] < opens[i-1] and (opens[i-1] - closes[i-1]) > (opens[i-1] * 0.005):
            gap = lows[i-2] - highs[i]
            if gap > 0:
                fvgs.append({"type": "short", "bottom": highs[i], "top": lows[i-2], "i": i-1})
    return fvgs

def pct_diff(a: float, b: float) -> float:
    return abs(a - b) / (abs(b) or 1) * 100

def _dist_to_zone(price: float, zone) -> float:
    if zone is None:
        return float("inf")
    if zone["lo"] <= price <= zone["hi"]:
        return 0.0
    edge = zone["lo"] if price < zone["lo"] else zone["hi"]
    return pct_diff(price, edge)

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 條件掃描核心判斷邏輯（整合價格反轉燭台與 OTE）
# ═══════════════════════════════════════════════════════════════════════════════
def _cond(met, near, score, hint, detail):
    return {"met": met, "near": near, "score": score, "hint": hint, "detail": detail}

