from datetime import datetime, timezone, timedelta
from config import log, _normalize, fetch_klines, TZ_OFFSET
from analyzers.indicators import (
    calc_ema, calc_rsi, calc_atr, find_swings, calc_bb,
    detect_fvgs, pct_diff, _dist_to_zone, _cond
)


def check_vegas(price, opens, highs, lows, closes, ema144, ema169) -> dict:
    if ema144 is None or ema169 is None:
        return _cond(False, False, 0, "neutral", "EMA 資料不足")
    top = max(ema144, ema169)
    bot = min(ema144, ema169)
    inside = bot <= price <= top
    dist = min(pct_diff(price, ema144), pct_diff(price, ema169))
    slope = "long" if ema144 > ema169 else "short"
    
    rev_long = closes[-1] > opens[-1] and closes[-1] > closes[-2]
    rev_short = closes[-1] < opens[-1] and closes[-1] < closes[-2]
    
    met_long = (inside or dist <= 0.8) and slope == "long" and rev_long
    met_short = (inside or dist <= 0.8) and slope == "short" and rev_short
    
    if met_long:
        return _cond(True, False, 2, "long", f"Tunnel 支撐 + 反彈確認 ({bot:.4g}~{top:.4g})")
    if met_short:
        return _cond(True, False, 2, "short", f"Tunnel 阻力 + 回落確認 ({bot:.4g}~{top:.4g})")
        
    near = (inside or dist <= 1.8)
    if near:
        return _cond(False, True, 1, slope, f"接近 Vegas Tunnel ({dist:.2f}%)，待反轉確認")
    return _cond(False, False, 0, "neutral", f"距 Vegas Tunnel {dist:.2f}%")

def check_fib(price, opens, highs, lows, closes, swings) -> dict:
    sh, sl = swings.get("sh", []), swings.get("sl", [])
    if not sh or not sl:
        return _cond(False, False, 0, "neutral", "Swing 資料不足")
        
    last_high = sh[-1]
    last_low  = sl[-1]
    trend = "long" if last_low["i"] < last_high["i"] else "short"
    
    # 收集最近的 Swing 點位以計算重合度 (從 8 個減至 4 個以排除雜訊)
    recent_sh = sh[-4:]
    recent_sl = sl[-4:]
    all_swings = []
    for h in recent_sh:
        all_swings.append({"i": h["i"], "val": h["v"], "type": "high"})
    for l in recent_sl:
        all_swings.append({"i": l["i"], "val": l["v"], "type": "low"})
    all_swings.sort(key=lambda x: x["i"])
    
    # 1. 2點式回撤點位
    fib_retracements = []
    for l in recent_sl:
        for h in recent_sh:
            if l["i"] < h["i"]:
                diff = h["v"] - l["v"]
                fib_retracements.append({"name": "0.786", "val": h["v"] - diff * 0.786, "trend": "long"})
                fib_retracements.append({"name": "0.618", "val": h["v"] - diff * 0.618, "trend": "long"})
            elif h["i"] < l["i"]:
                diff = h["v"] - l["v"]
                fib_retracements.append({"name": "0.786", "val": l["v"] + diff * 0.786, "trend": "short"})
                fib_retracements.append({"name": "0.618", "val": l["v"] + diff * 0.618, "trend": "short"})

    # 2. 3點式 Trend-Based 擴展點位
    fib_extensions = []
    for idx in range(len(all_swings) - 2):
        A = all_swings[idx]
        B = all_swings[idx+1]
        C = all_swings[idx+2]
        if A["type"] == "low" and B["type"] == "high" and C["type"] == "low":
            val = C["val"] + (B["val"] - A["val"]) * 1.618
            fib_extensions.append({"name": "1.618 Ext", "val": val, "trend": "long"})
        elif A["type"] == "high" and B["type"] == "low" and C["type"] == "high":
            val = C["val"] - (A["val"] - B["val"]) * 1.618
            fib_extensions.append({"name": "1.618 Ext", "val": val, "trend": "short"})
            
    # 3. 聚類分析重合區
    all_levels = fib_retracements + fib_extensions
    confluence_zones = []
    checked = set()
    for i in range(len(all_levels)):
        if i in checked:
            continue
        cluster = [all_levels[i]]
        checked.add(i)
        for j in range(i + 1, len(all_levels)):
            if j in checked:
                continue
            diff_pct = abs(all_levels[i]["val"] - all_levels[j]["val"]) / all_levels[i]["val"] * 100
            if diff_pct <= 0.8:  # 容差收緊至 0.8% 以提高精準度
                cluster.append(all_levels[j])
                checked.add(j)
        if len(cluster) >= 2:
            avg_val = sum(c["val"] for c in cluster) / len(cluster)
            long_count = sum(1 for c in cluster if c["trend"] == "long")
            short_count = sum(1 for c in cluster if c["trend"] == "short")
            cluster_trend = "long" if long_count >= short_count else "short"
            confluence_zones.append({
                "avg_val": avg_val,
                "trend": cluster_trend,
                "level_count": len(cluster)
            })
            
    # 尋找距離當前價格最近的重合區
    closest_zone = None
    min_dist = 9999.0
    for cz in confluence_zones:
        dist = abs(price - cz["avg_val"]) / price * 100
        if dist < min_dist:
            min_dist = dist
            closest_zone = cz
            
    # 強力反轉 K 線確認 (吞噬或實體收盤高/低於前一 K 線)
    rev_long = closes[-1] > opens[-1] and closes[-1] > highs[-2]
    rev_short = closes[-1] < opens[-1] and closes[-1] < lows[-2]
    
    # 優先匹配重合支撐/阻力區
    if closest_zone and min_dist <= 1.0: # 偏差收緊至 1.0%
        cz_trend = closest_zone["trend"]
        met = (cz_trend == "long" and rev_long) or (cz_trend == "short" and rev_short)
        near = (not met) and min_dist <= 2.0
        
        detail = f"斐波重合區 {closest_zone['avg_val']:.4g} (重合 {closest_zone['level_count']} 個點位)，偏差 {min_dist:.2f}%"
        if met:
            return _cond(True, False, 2, cz_trend, detail + " + 反彈確認")
        if near:
            return _cond(False, True, 1, cz_trend, detail + " (待確認)")
            
    # 4. 備用回退：單一 Swing 的最近 OTE 點位檢測
    hi, lo = last_high["v"], last_low["v"]
    r = hi - lo
    levels = {
        "f618": lo + r * 0.618 if trend == "long" else hi - r * 0.618,
        "f702": lo + r * 0.702 if trend == "long" else hi - r * 0.702,
        "f786": lo + r * 0.786 if trend == "long" else hi - r * 0.786,
    }
    
    dists = {k: pct_diff(price, v) for k, v in levels.items()}
    closest_level = min(dists, key=dists.get)
    dist = dists[closest_level]
    target_val = levels[closest_level]
    level_name = {"f618": "0.618", "f702": "0.702", "f786": "0.786"}[closest_level]
    
    met = dist <= 0.8 and ((trend == "long" and rev_long) or (trend == "short" and rev_short))
    near = (not met) and dist <= 1.5
    
    detail = f"{'上升' if trend == 'long' else '下降'}區間 {level_name}={target_val:.4g}，偏差 {dist:.2f}%"
    if met:
        return _cond(True, False, 2, trend, detail + " + 反彈確認")
    if near:
        return _cond(False, True, 1, trend, detail + " (待確認)")
    return _cond(False, False, 0, "neutral", detail)

def check_ob(price, opens, highs, lows, closes) -> dict:
    def detect_strong_ob(opens_l, highs_l, lows_l, closes_l, direction: str):
        n = len(closes_l)
        last = min(40, n - 4)
        if direction == "long":
            for i in range(n - 3, max(n - last, 1), -1):
                if closes_l[i] < opens_l[i]:
                    if closes_l[i+1] > highs_l[i] or closes_l[i+2] > highs_l[i]:
                        ob_zone = {"hi": opens_l[i], "lo": lows_l[i]}
                        # 增加 Mitigation 檢測：在此之後是否有任何 K 線收盤跌破 OB 低點
                        mitigated = False
                        for j in range(i+1, n):
                            if closes_l[j] < ob_zone["lo"]:
                                mitigated = True
                                break
                        if not mitigated:
                            return ob_zone
        else:
            for i in range(n - 3, max(n - last, 1), -1):
                if closes_l[i] > opens_l[i]:
                    if closes_l[i+1] < lows_l[i] or closes_l[i+2] < lows_l[i]:
                        ob_zone = {"hi": highs_l[i], "lo": opens_l[i]}
                        # 增加 Mitigation 檢測：在此之後是否有任何 K 線收盤突破 OB 高點
                        mitigated = False
                        for j in range(i+1, n):
                            if closes_l[j] > ob_zone["hi"]:
                                mitigated = True
                                break
                        if not mitigated:
                            return ob_zone
        return None

    bull = detect_strong_ob(opens, highs, lows, closes, "long")
    bear = detect_strong_ob(opens, highs, lows, closes, "short")
    
    bd = _dist_to_zone(price, bull)
    sd = _dist_to_zone(price, bear)
    
    if bd == float("inf") and sd == float("inf"):
        res = _cond(False, False, 0, "neutral", "無明確 OB 區間")
        res["dist"] = float("inf")
        return res
        
    direction = "long" if bd <= sd else "short"
    zone = bull if direction == "long" else bear
    dist = bd if direction == "long" else sd
    
    # 升級為強反轉 K 線型態確認
    if direction == "long":
        is_engulfing = closes[-1] > opens[-1] and closes[-2] < opens[-2] and closes[-1] > highs[-2]
        is_hammer = (closes[-1] > opens[-1]) and ((opens[-1] - lows[-1]) > (closes[-1] - opens[-1]) * 1.5) and ((highs[-1] - closes[-1]) < (closes[-1] - opens[-1]) * 0.5)
        rev_long = is_engulfing or is_hammer or (closes[-1] > highs[-2])
    else:
        is_engulfing = closes[-1] < opens[-1] and closes[-2] > opens[-2] and closes[-1] < lows[-2]
        is_star = (closes[-1] < opens[-1]) and ((highs[-1] - opens[-1]) > (opens[-1] - closes[-1]) * 1.5) and ((closes[-1] - lows[-1]) < (opens[-1] - closes[-1]) * 0.5)
        rev_short = is_engulfing or is_star or (closes[-1] < lows[-2])
        
    met = dist <= 0.8 and ((direction == "long" and rev_long) or (direction == "short" and rev_short))
    near = (not met) and dist <= 1.8
    
    lbl = "Bullish" if direction == "long" else "Bearish"
    detail = f"{lbl} OB [{zone['lo']:.4g}~{zone['hi']:.4g}]，距離 {dist:.2f}%"
    
    if met:
        res = _cond(True, False, 2, direction, detail + " + 反轉確認")
    elif near:
        res = _cond(False, True, 1, direction, detail + " (待確認)")
    else:
        res = _cond(False, False, 0, "neutral", detail)
    res["dist"] = dist
    return res

def check_rsi(rsi_value, closes, opens) -> dict:
    if rsi_value is None:
        return _cond(False, False, 0, "neutral", "RSI 資料不足")
    v = rsi_value
    rev_long = closes[-1] > opens[-1] and closes[-1] > closes[-2]
    rev_short = closes[-1] < opens[-1] and closes[-1] < closes[-2]
    
    if v < 35:
        detail = f"RSI {v:.1f} 超賣區"
        return _cond(rev_long, not rev_long, 2 if rev_long else 1, "long", detail + (" (反彈確認)" if rev_long else ""))
    if v <= 42:
        return _cond(False, True, 1, "long", f"RSI {v:.1f} 偏低，等待反轉")
        
    if v > 65:
        detail = f"RSI {v:.1f} 超買區"
        return _cond(rev_short, not rev_short, 2 if rev_short else 1, "short", detail + (" (回落確認)" if rev_short else ""))
    if v >= 58:
        return _cond(False, True, 1, "short", f"RSI {v:.1f} 偏高，等待確認")
        
    return _cond(False, False, 0, "neutral", f"RSI {v:.1f} 中性區")

def check_sr(price, opens, highs, lows, closes, swings) -> dict:
    points = (
        [{"v": p["v"], "t": "short"} for p in swings["sh"]] +
        [{"v": p["v"], "t": "long"}  for p in swings["sl"]]
    )
    if not points:
        return _cond(False, False, 0, "neutral", "S/R 資料不足")
    nearest = min(points, key=lambda p: pct_diff(price, p["v"]))
    dist = pct_diff(price, nearest["v"])
    
    rev_long = closes[-1] > opens[-1] and closes[-1] > closes[-2]
    rev_short = closes[-1] < opens[-1] and closes[-1] < closes[-2]
    
    met = dist <= 0.6 and ((nearest["t"] == "long" and rev_long) or (nearest["t"] == "short" and rev_short))
    near = (not met) and dist <= 1.5
    
    lbl  = "支撐" if nearest["t"] == "long" else "阻力"
    detail = f"最近{lbl} {nearest['v']:.4g}，距離 {dist:.2f}%"
    
    if met:
        res = _cond(True, False, 2, nearest["t"], detail + " + 反彈確認")
    elif near:
        res = _cond(False, True, 1, nearest["t"], detail + " (待確認)")
    else:
        res = _cond(False, False, 0, "neutral", detail)
    res["dist"] = dist
    return res

def check_bb(price, opens, highs, lows, closes) -> dict:
    bb = calc_bb(closes, 20, 2)
    mid = bb["mid"][-1]
    upper = bb["upper"][-1]
    lower = bb["lower"][-1]
    if mid is None or upper is None or lower is None:
        return _cond(False, False, 0, "neutral", "BB 資料不足")
        
    touched_lower = any(lows[i] <= bb["lower"][i] for i in range(-3, 0))
    bull_rev = closes[-1] > opens[-1] and closes[-1] > closes[-2]
    met_long = touched_lower and bull_rev
    
    touched_upper = any(highs[i] >= bb["upper"][i] for i in range(-3, 0))
    bear_rev = closes[-1] < opens[-1] and closes[-1] < closes[-2]
    met_short = touched_upper and bear_rev
    
    if met_long:
        return _cond(True, False, 2, "long", f"觸碰布林下軌 {lower:.4g} 反彈")
    if met_short:
        return _cond(True, False, 2, "short", f"觸碰布林上軌 {upper:.4g} 回落")
        
    dist_lower = pct_diff(price, lower)
    dist_upper = pct_diff(price, upper)
    if dist_lower <= 1.2:
        return _cond(False, True, 1, "long", f"接近布林下軌 {lower:.4g}，待反轉")
    if dist_upper <= 1.2:
        return _cond(False, True, 1, "short", f"接近布林上軌 {upper:.4g}，待反轉")
        
    return _cond(False, False, 0, "neutral", f"處於布林帶中軌 {mid:.4g} 附近")

def check_fvg(price, opens, highs, lows, closes) -> dict:
    # 升級後的 FVG 檢測，排除已被完全填補的 FVG
    n = len(closes)
    limit = 40
    
    # 重新檢測並過濾已補缺的 FVG
    raw_fvgs = detect_fvgs(opens, highs, lows, closes, limit)
    fvgs = []
    
    # 我們在 detect_fvgs 的基礎上，對每一筆進行 Mitigation 檢測
    for f in raw_fvgs:
        idx = f["i"]  # 形成的 FVG 蠟燭索引 (i-1)
        mitigated = False
        if f["type"] == "long":
            # 如果之後有任何 K 線最低價低於 FVG 底部，代表已補缺
            for j in range(idx + 1, n):
                if lows[j] <= f["bottom"]:
                    mitigated = True
                    break
        else:
            # 如果之後有任何 K 線最高價高於 FVG 頂部，代表已補缺
            for j in range(idx + 1, n):
                if highs[j] >= f["top"]:
                    mitigated = True
                    break
        if not mitigated:
            fvgs.append(f)
            
    if not fvgs:
        return _cond(False, False, 0, "neutral", "近 40 根無未補 FVG")
        
    bullish_fvgs = [f for f in fvgs if f["type"] == "long"]
    bearish_fvgs = [f for f in fvgs if f["type"] == "short"]
    
    active_bull = None
    for f in reversed(bullish_fvgs):
        if lows[-1] <= f["top"] and closes[-1] >= f["bottom"] * 0.999:
            active_bull = f
            break
            
    active_bear = None
    for f in reversed(bearish_fvgs):
        if highs[-1] >= f["bottom"] and closes[-1] <= f["top"] * 1.001:
            active_bear = f
            break
            
    # 採用更強的收盤突破來確認反轉
    rev_long = closes[-1] > opens[-1] and closes[-1] > highs[-2]
    rev_short = closes[-1] < opens[-1] and closes[-1] < lows[-2]
    
    if active_bull:
        return _cond(rev_long, not rev_long, 2 if rev_long else 1, "long", f"回踩多頭 FVG [{active_bull['bottom']:.4g}~{active_bull['top']:.4g}]" + (" + 確認" if rev_long else ""))
    if active_bear:
        return _cond(rev_short, not rev_short, 2 if rev_short else 1, "short", f"回測空頭 FVG [{active_bear['bottom']:.4g}~{active_bear['top']:.4g}]" + (" + 確認" if rev_short else ""))
        
    nearest = min(fvgs, key=lambda f: min(abs(price - f["top"]), abs(price - f["bottom"])))
    dist = min(pct_diff(price, nearest["top"]), pct_diff(price, nearest["bottom"]))
    
    if dist <= 1.5:
        return _cond(False, True, 1, nearest["type"], f"接近 FVG [{nearest['bottom']:.4g}~{nearest['top']:.4g}]，偏差 {dist:.2f}%")
    return _cond(False, False, 0, "neutral", f"最近 FVG [{nearest['bottom']:.4g}~{nearest['top']:.4g}]，距 {dist:.2f}%")

def decide_direction(conditions: dict) -> dict:
    hints = {"long": 0, "short": 0}
    score = 0
    for c in conditions.values():
        score += c.get("score", 0)
        h = c.get("hint", "neutral")
        if h == "long" and c.get("score", 0) > 0:
            hints["long"] += 1
        elif h == "short" and c.get("score", 0) > 0:
            hints["short"] += 1
            
    direction = "neutral"
    # 進場門檻更靈敏：2項強共振或3項中等共振
    if hints["long"] >= 3 and hints["long"] > hints["short"]:
        direction = "long"
    elif hints["short"] >= 3 and hints["short"] > hints["long"]:
        direction = "short"
    elif hints["long"] == 2 and hints["short"] == 0:
        direction = "long"
    elif hints["short"] == 2 and hints["long"] == 0:
        direction = "short"
        
    count_met    = sum(1 for c in conditions.values() if c.get("met"))
    count_active = sum(1 for c in conditions.values() if c.get("score", 0) > 0)
    
    # 嚴格進場防禦：若沒有任何實際滿足項 (count_met == 0)，強迫維持中性觀望
    if count_met < 1:
        direction = "neutral"
        
    strength     = min(100, round(score / 14 * 100)) # 7 個指標滿分 14
    return {"direction": direction, "strength": strength, "score": score,
            "count_met": count_met, "count_active": count_active}

def gen_smart_tpsl(entry: float, direction: str, swings: dict, atr: float) -> dict:
    valid_atr = atr if (atr and atr > 0) else abs(entry) * 0.01
    
    if direction == "long":
        struct_lo = swings["sl"][-1]["v"] if (swings and swings.get("sl")) else entry - 2.5 * valid_atr
        sl = min(max(struct_lo, entry - 3.5 * valid_atr), entry - 1.2 * valid_atr)
        risk = entry - sl
        
        # 取得最近的前高 (Swing High) 作為結構目標參考
        last_sh = swings["sh"][-1]["v"] if (swings and swings.get("sh") and swings["sh"][-1]["v"] > entry) else entry + risk * 1.5
        
        # 斐波那契 / 前高結構目標：
        # TP1: 前高前夕或 0.75 Risk (首波落袋)
        tp1 = min(entry + risk * 0.75, last_sh * 0.995) if last_sh > entry else entry + risk * 0.75
        # TP2: 精確前高壓力點或 1.4 Risk (結構破位點)
        tp2 = last_sh if last_sh > entry else entry + risk * 1.4
        # TP3: 斐波 1.272 延伸 (1.272 * Swing Range)
        tp3 = entry + risk * 2.2
        # TP4: 斐波 1.618 延伸 (1.618 * Swing Range)
        tp4 = entry + risk * 3.5
        
        tps = [round(tp1, 4), round(tp2, 4), round(tp3, 4), round(tp4, 4)]
    else:
        struct_hi = swings["sh"][-1]["v"] if (swings and swings.get("sh")) else entry + 2.5 * valid_atr
        sl = max(min(struct_hi, entry + 3.5 * valid_atr), entry + 1.2 * valid_atr)
        risk = sl - entry
        
        # 取得最近的前低 (Swing Low) 作為結構目標參考
        last_sl = swings["sl"][-1]["v"] if (swings and swings.get("sl") and swings["sl"][-1]["v"] < entry) else entry - risk * 1.5
        
        # 斐波那契 / 前低結構目標：
        tp1 = max(entry - risk * 0.75, last_sl * 1.005) if last_sl < entry else entry - risk * 0.75
        tp2 = last_sl if last_sl < entry else entry - risk * 1.4
        tp3 = entry - risk * 2.2
        tp4 = entry - risk * 3.5
        
        tps = [round(tp1, 4), round(tp2, 4), round(tp3, 4), round(tp4, 4)]
        
    return {"sl": sl, "tps": tps, "atr": valid_atr}

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 單幣種全方位掃描
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_symbol(symbol: str, klines: list, ticker: dict = None) -> dict:
    if not klines or len(klines) < 170:
        return None
        
    opens  = [float(k[1]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    ema144 = calc_ema(closes, 144)
    ema169 = calc_ema(closes, 169)
    rsi    = calc_rsi(closes, 14)
    atr    = calc_atr(highs, lows, closes, 14)
    swings = find_swings(highs, lows, 5)

    price    = closes[-1]
    last_atr = next((v for v in reversed(atr)  if v is not None), price * 0.01)
    last_e144= next((v for v in reversed(ema144) if v is not None), None)
    last_e169= next((v for v in reversed(ema169) if v is not None), None)
    last_rsi = next((v for v in reversed(rsi)   if v is not None), None)

    # 獲取日線 (1D) 大趨勢
    daily_trend = "neutral"
    try:
        klines_1d = fetch_klines(symbol, "1d", limit=200)
        if klines_1d and len(klines_1d) >= 170:
            closes_1d = [float(k[4]) for k in klines_1d]
            ema144_1d = calc_ema(closes_1d, 144)
            ema169_1d = calc_ema(closes_1d, 169)
            last_e144_1d = ema144_1d[-1]
            last_e169_1d = ema169_1d[-1]
            price_1d = closes_1d[-1]
            if last_e144_1d is not None and last_e169_1d is not None:
                if price_1d > max(last_e144_1d, last_e169_1d) and last_e144_1d > last_e169_1d:
                    daily_trend = "long"
                elif price_1d < min(last_e144_1d, last_e169_1d) and last_e144_1d < last_e169_1d:
                    daily_trend = "short"
    except Exception as e:
        log.warning(f"  🧬 [Daily Trend] 計算日線趨勢失敗 {symbol}: {e}")

    conditions = {
        "vegas": check_vegas(price, opens, highs, lows, closes, last_e144, last_e169),
        "fib":   check_fib(price, opens, highs, lows, closes, swings),
        "ob":    check_ob(price, opens, highs, lows, closes),
        "rsi":   check_rsi(last_rsi, closes, opens),
        "sr":    check_sr(price, opens, highs, lows, closes, swings),
        "bb":    check_bb(price, opens, highs, lows, closes),
        "fvg":   check_fvg(price, opens, highs, lows, closes),
    }
    summary = decide_direction(conditions)
    direction = summary["direction"]
    
    # ── 💡 戰略優化過濾器 ──
    # 1. 排除「純 Fib + FVG」信號 (這兩者共振勝率極低)
    logic_list = [k for k, v in conditions.items() if v.get("score", 0) > 0]
    if len(logic_list) == 2 and "fib" in logic_list and "fvg" in logic_list:
        direction = "neutral"
        summary["direction"] = "neutral"
        
    # 2. 趨勢跟隨過濾器 (Vegas 隧道大趨勢過濾)
    # 若為多單，但價格低於 Vegas 隧道下軌，強迫維持中性觀望
    if direction == "long" and last_e144 and last_e169:
        if price < min(last_e144, last_e169):
            direction = "neutral"
            summary["direction"] = "neutral"
            
    # 若為空單，但價格高於 Vegas 隧道上軌，強迫維持中性觀望
    elif direction == "short" and last_e144 and last_e169:
        if price > max(last_e144, last_e169):
            direction = "neutral"
            summary["direction"] = "neutral"

    # 4. 局部支撐阻力防守過濾器 (避免做空在支撐、做多在阻力；高分信號/突破單不進行防守過濾)
    if direction == "short":
        ob_is_support = (conditions["ob"].get("hint") == "long")
        ob_dist = conditions["ob"].get("dist", float("inf"))
        sr_is_support = (conditions["sr"].get("hint") == "long")
        sr_dist = conditions["sr"].get("dist", float("inf"))
        
        # 距離下方局部支撐低於 0.5% 且分數 < 8，強制轉為中性過濾
        if (ob_is_support and ob_dist < 0.5) or (sr_is_support and sr_dist < 0.5):
            if summary["score"] < 8:
                direction = "neutral"
                summary["direction"] = "neutral"
                log.info(f"🚫 [Filter] {symbol} 空單過濾：分數={summary['score']} 低於 8 且進場價距離下方支撐過近 (OB_dist={ob_dist:.2f}%, SR_dist={sr_dist:.2f}%)，暫停做空")
            
    elif direction == "long":
        ob_is_resistance = (conditions["ob"].get("hint") == "short")
        ob_dist = conditions["ob"].get("dist", float("inf"))
        sr_is_resistance = (conditions["sr"].get("hint") == "short")
        sr_dist = conditions["sr"].get("dist", float("inf"))
        
        # 距離上方局部阻力低於 0.5% 且分數 < 8，強制轉為中性過濾
        if (ob_is_resistance and ob_dist < 0.5) or (sr_is_resistance and sr_dist < 0.5):
            if summary["score"] < 8:
                direction = "neutral"
                summary["direction"] = "neutral"
                log.info(f"🚫 [Filter] {symbol} 多單過濾：分數={summary['score']} 低於 8 且進場價距離上方阻力過近 (OB_dist={ob_dist:.2f}%, SR_dist={sr_dist:.2f}%)，暫停做多")

    # 3. 週線水平結構支撐阻力檢測與過濾
    weekly_support_data = {
        "near_support": False,
        "near_resistance": False,
        "support_level": None,
        "resistance_level": None,
        "diff_pct": 0.0
    }
    try:
        # 獲取最近 15 週的週線 K 線
        klines_1w = fetch_klines(symbol, "1w", limit=15)
        if klines_1w and len(klines_1w) >= 3:
            lows_1w = [float(k[3]) for k in klines_1w]
            highs_1w = [float(k[2]) for k in klines_1w]
            closes_1w = [float(k[4]) for k in klines_1w]
            
            # 週線水平支撐區：近期最低的 2 個低點，以及近 4 週收盤平均價
            weekly_supports = sorted(lows_1w[-8:])[:2] + [sum(closes_1w[-4:]) / 4]
            # 週線水平阻力區：近期最高的 2 個高點
            weekly_resistances = sorted(highs_1w[-8:])[-2:]
            
            # 檢測是否貼近週線支撐 (2.2% 以內)
            for lvl in weekly_supports:
                diff_pct = abs(price - lvl) / lvl * 100
                if diff_pct <= 2.2 and price >= lvl * 0.98:
                    weekly_support_data["near_support"] = True
                    weekly_support_data["support_level"] = round(lvl, 4)
                    weekly_support_data["diff_pct"] = round(diff_pct, 2)
                    break
                    
            # 檢測是否貼近週線阻力
            for lvl in weekly_resistances:
                diff_pct = abs(price - lvl) / lvl * 100
                if diff_pct <= 2.2 and price <= lvl * 1.02:
                    weekly_support_data["near_resistance"] = True
                    weekly_support_data["resistance_level"] = round(lvl, 4)
                    weekly_support_data["diff_pct"] = round(diff_pct, 2)
                    break
    except Exception as e:
        log.warning(f"  🧬 [Weekly Level] 計算週線支撐失敗 {symbol}: {e}")

    # A. 阻礙過濾：避免在週線支撐附近追空
    if direction == "short" and weekly_support_data["near_support"]:
        direction = "neutral"
        summary["direction"] = "neutral"
        
    # B. 阻礙過濾：避免在週線阻力附近追多
    if direction == "long" and weekly_support_data["near_resistance"]:
        direction = "neutral"
        summary["direction"] = "neutral"

    # C. 共振加分：若為多單且在週線支撐上，提升強度得分
    if direction == "long" and weekly_support_data["near_support"]:
        summary["strength"] = min(100, summary["strength"] + 15)

    # Strictly calculate the higher timeframe trend bias
    trend = "neutral"
    if last_e144 is not None and last_e169 is not None:
        if price > max(last_e144, last_e169) and last_e144 > last_e169:
            trend = "long"
        elif price < min(last_e144, last_e169) and last_e144 < last_e169:
            trend = "short"

    # For neutral setups, we default the simulated trade direction to the trend bias
    sim_dir = direction if direction != "neutral" else (trend if trend != "neutral" else "long")
    tpsl = gen_smart_tpsl(price, sim_dir, swings, last_atr)
    change = float((ticker or {}).get("priceChangePercent", 0))

    return {
        "symbol":       _normalize(symbol),
        "price":        price,
        "change24h":    change,
        "conditions":   conditions,
        "direction":    direction,
        "sim_direction": sim_dir,
        "strength":     summary["strength"],
        "score":        summary["score"],
        "count_met":    summary["count_met"],
        "count_active": summary["count_active"],
        "atr":          tpsl["atr"],
        "sl":           tpsl["sl"],
        "tps":          tpsl["tps"],
        "trend":        trend,
        "last_e144":    last_e144,
        "last_e169":    last_e169,
        "last_rsi":     last_rsi,
        "weekly_support": weekly_support_data,
        "daily_trend":  daily_trend,
    }

def get_higher_tf(tf: str) -> str:
    return {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w", "1w": "1w"}.get(tf.lower(), "4h")

def calc_signal_expiry(tf: str, count_active: int) -> str:
    hours_map = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0, "1w": 168.0}
    h      = hours_map.get(tf.lower(), 4.0)
    factor = 3.5 if count_active >= 4 else (2.5 if count_active >= 2 else 1.5)
    expiry = h * factor
    if expiry < 1:
        text = f"約 {round(expiry * 60)} 分鐘內未動則撤單"
    elif expiry < 24:
        text = f"約 {expiry:.1f} 小時內未動則撤單"
    else:
        text = f"約 {expiry / 24:.1f} 天內未動則撤單"
    note = "·強共振時效長" if count_active >= 4 else ("·中等信號" if count_active >= 2 else "·時效短")
    return f"{text} {note}"


# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ BTC 大盤分析與聯動過濾 (斐波那契重合與今年1-4月盤整支撐)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_btc_macro(end_time_ms: int = None) -> dict:
    try:
        klines = fetch_klines("BTCUSDT", "1d", 250, end_time_ms)
        if not klines or len(klines) < 170:
            return {
                "price": 0.0,
                "change": 0.0,
                "state": "NEUTRAL",
                "filter_rule": "⚡ 允許雙向交易",
                "analysis_text": "無法獲取足夠的 BTC K 線數據，大盤過濾器暫時處於中性狀態。",
                "levels": [],
                "confluences": []
            }
        
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        
        current_price = closes[-1]
        change_pct = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0.0
        
        ema144 = calc_ema(closes, 144)
        ema169 = calc_ema(closes, 169)
        last_e144 = next((v for v in reversed(ema144) if v is not None), None)
        last_e169 = next((v for v in reversed(ema169) if v is not None), None)
        
        swings = find_swings(highs, lows, lb=5)
        sh_list = swings.get("sh", [])
        sl_list = swings.get("sl", [])
        
        n = len(closes)
        recent_sh = [s for s in sh_list if (n - s["i"]) < 180]
        recent_sl = [s for s in sl_list if (n - s["i"]) < 180]
        
        all_swings = []
        for h in recent_sh:
            all_swings.append({"i": h["i"], "val": h["v"], "type": "high"})
        for l in recent_sl:
            all_swings.append({"i": l["i"], "val": l["v"], "type": "low"})
        all_swings.sort(key=lambda x: x["i"])
        
        # Consolidation Zone (Jan-Apr 2026)
        jan_apr_closes = []
        for k in klines:
            ot = datetime.fromtimestamp(k[0]/1000, tz=timezone.utc)
            if ot.year == 2026 and ot.month in [1, 2, 3, 4]:
                jan_apr_closes.append(float(k[4]))
                
        con_low = 67300.0
        con_high = 90000.0
        if jan_apr_closes:
            sorted_closes = sorted(jan_apr_closes)
            con_low = sorted_closes[int(len(sorted_closes) * 0.15)]
            con_high = sorted_closes[int(len(sorted_closes) * 0.85)]
            
        fib_retracements = []
        for l in recent_sl:
            for h in recent_sh:
                if l["i"] < h["i"]:
                    r = h["v"] - l["v"]
                    f786 = h["v"] - r * 0.786
                    f618 = h["v"] - r * 0.618
                    fib_retracements.append({
                        "name": f"Fib 0.786 Retracement (L:{l['v']:.0f} -> H:{h['v']:.0f})",
                        "val": f786
                    })
                    fib_retracements.append({
                        "name": f"Fib 0.618 Retracement (L:{l['v']:.0f} -> H:{h['v']:.0f})",
                        "val": f618
                    })
                    
        fib_extensions = []
        for idx in range(len(all_swings) - 2):
            A = all_swings[idx]
            B = all_swings[idx+1]
            C = all_swings[idx+2]
            if A["type"] == "low" and B["type"] == "high" and C["type"] == "low":
                val = C["val"] + (B["val"] - A["val"]) * 1.618
                fib_extensions.append({
                    "name": f"Fib 1.618 Extension (Up: A:{A['val']:.0f}->B:{B['val']:.0f}->C:{C['val']:.0f})",
                    "val": val
                })
            elif A["type"] == "high" and B["type"] == "low" and C["type"] == "high":
                val = C["val"] - (A["val"] - B["val"]) * 1.618
                fib_extensions.append({
                    "name": f"Fib 1.618 Extension (Down: A:{A['val']:.0f}->B:{B['val']:.0f}->C:{C['val']:.0f})",
                    "val": val
                })
                
        all_levels = fib_retracements + fib_extensions
        all_levels.append({"name": "Consolidation Low (Jan-Apr)", "val": con_low})
        all_levels.append({"name": "Consolidation High (Jan-Apr)", "val": con_high})
        
        confluences = []
        checked = set()
        for i in range(len(all_levels)):
            if i in checked:
                continue
            cluster = [all_levels[i]]
            checked.add(i)
            for j in range(i + 1, len(all_levels)):
                if j in checked:
                    continue
                diff_pct = abs(all_levels[i]["val"] - all_levels[j]["val"]) / all_levels[i]["val"] * 100
                if diff_pct <= 1.8:
                    cluster.append(all_levels[j])
                    checked.add(j)
            if len(cluster) >= 2:
                avg_val = sum(c["val"] for c in cluster) / len(cluster)
                confluences.append({
                    "avg_val": avg_val,
                    "levels": cluster
                })
                
        confluences.sort(key=lambda x: -len(x["levels"]))
        
        btc_support_min = 68000.0
        btc_support_max = 68600.0
        for c in confluences:
            if 66000 <= c["avg_val"] <= 70000:
                btc_support_min = c["avg_val"] * 0.995
                btc_support_max = c["avg_val"] * 1.005
                break
                
        # Fetch 4H K-lines to check medium-term trend
        klines_4h = fetch_klines("BTCUSDT", "4h", 100, end_time_ms)
        btc_4h_bullish = False
        if klines_4h:
            closes_4h = [float(k[4]) for k in klines_4h]
            ema144_4h = calc_ema(closes_4h, 144)
            ema169_4h = calc_ema(closes_4h, 169)
            last_e144_4h = next((v for v in reversed(ema144_4h) if v is not None), None)
            last_e169_4h = next((v for v in reversed(ema169_4h) if v is not None), None)
            if last_e144_4h and last_e169_4h:
                if current_price > max(last_e144_4h, last_e169_4h):
                    btc_4h_bullish = True

        # Fetch 1H K-lines to check short-term reversal (V-shape recovery)
        klines_1h = fetch_klines("BTCUSDT", "1h", 100, end_time_ms)
        btc_1h_bullish = False
        if klines_1h:
            closes_1h = [float(k[4]) for k in klines_1h]
            ema12_1h = calc_ema(closes_1h, 12)
            ema26_1h = calc_ema(closes_1h, 26)
            last_e12_1h = next((v for v in reversed(ema12_1h) if v is not None), None)
            last_e26_1h = next((v for v in reversed(ema26_1h) if v is not None), None)
            if last_e12_1h and last_e26_1h:
                if last_e12_1h > last_e26_1h:
                    btc_1h_bullish = True

        broken_support = current_price < btc_support_min
        
        vegas_trend = "neutral"
        if last_e144 and last_e169:
            if current_price > max(last_e144, last_e169):
                vegas_trend = "bullish"
            elif current_price < min(last_e144, last_e169):
                vegas_trend = "bearish"
                
        btc_state = "NEUTRAL"
        filter_rule = "⚡ 允許雙向交易：大盤處於震盪區間"
        analysis_text = f"目前 BTC 價格為 {current_price:.1f}，處於日線 Vegas 通道附近或區間震盪，未觸及極端支撐或阻力位。系統允許雙向交易，但應嚴格控制倉位。"
        
        if not broken_support and vegas_trend == "bullish":
            btc_state = "BULLISH"
            filter_rule = "🚀 限制空單：大盤處於牛市單邊拉升趨勢，僅允許高勝率多單"
            analysis_text = f"目前 BTC 價格為 {current_price:.1f}，已站上日線 Vegas 通道之上，且大盤多頭排列。系統限制空單，全力跟隨多單波段。"
        
        if broken_support:
            if btc_4h_bullish or btc_1h_bullish:
                btc_state = "DIVERGENT"
                filter_rule = "🚫 暫停交易：大盤日線偏空但短線反彈，多空方向衝突"
                analysis_text = f"目前 BTC 價格為 {current_price:.1f}，雖然跌破日線支撐，但 1H/4H 級別出現反彈動能。由於大週期趨勢偏空與短線反彈動能衝突，市場極易出現洗盤，系統暫停所有多空開單信號。"
            else:
                btc_state = "BEARISH"
                filter_rule = "🚫 限制多單：大盤跌破關鍵支撐，僅允許高勝率空單"
                analysis_text = f"目前 BTC 價格為 {current_price:.1f}，已有效跌破今年 2-4 月盤整區間低點及 Fib 0.786 重合關鍵支撐區 ({btc_support_min:.1f}-{btc_support_max:.1f})。"
                if current_price < 67500:
                    analysis_text += " 反彈至 67500 無力，大盤極度偏空，直接下探 65000 支撐。系統限制多單。"
                else:
                    analysis_text += " 大盤偏弱，防範虛假反彈，系統已限制山寨幣多單信號。"

        closest_levels = sorted(all_levels, key=lambda x: abs(x["val"] - current_price))[:6]
        serialized_confluences = []
        for c in confluences[:4]:
            serialized_confluences.append({
                "avg_val": c["avg_val"],
                "level_count": len(c["levels"]),
                "names": [l["name"] for l in c["levels"][:3]]
            })
            
        return {
            "price": current_price,
            "change": change_pct,
            "state": btc_state,
            "filter_rule": filter_rule,
            "analysis_text": analysis_text,
            "levels": [{"name": l["name"], "val": l["val"]} for l in closest_levels],
            "confluences": serialized_confluences
        }
    except Exception as e:
        return {
            "price": 0.0,
            "change": 0.0,
            "state": "NEUTRAL",
            "filter_rule": "⚡ 允許雙向交易",
            "analysis_text": f"BTC 分析錯誤: {str(e)}",
            "levels": [],
            "confluences": []
        }

