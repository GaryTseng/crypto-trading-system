#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 Antigravity Advanced Crypto Scanner & Trading Dashboard
Includes Optimized Multi-Indicator Logic, Duplicate Alert Suppression, 
and a Premium Multi-threaded Local Web Dashboard on Port 5000.
"""

import os
import re
import time
import json
import math
import logging
import requests
import threading
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import sqlite3
import db_manager

# ── 導入系統設定與日誌 ──
from config import (
    BOT_TOKEN, CHAT_ID, COINS, PRIMARY_TF, SCAN_TIMEFRAMES,
    SCAN_INTERVAL, MIN_CONDITIONS, MIN_SCORE, MAX_CONCURRENT_TRADES, REQUEST_DELAY,
    TZ_OFFSET, WEB_PORT, ACTIVE_TRADES_FILE, BTC_MACRO_STATE, parse_sim_time, log, _normalize,
    load_active_trades, save_active_trades, load_active_trades_ai_sentiment, save_active_trades_ai_sentiment,
    send_telegram, fetch_klines, fetch_ticker,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 持久化交易狀態管理 (防重複開單與時效檢測)
# ═══════════════════════════════════════════════════════════════════════════════
# ── 導入技術指標與分析策略模組 ──
from analyzers import (
    calc_ema, calc_rsi, calc_atr, find_swings, calc_bb,
    check_vegas, check_fib, check_ob, check_rsi, check_sr, check_bb, check_fvg,
    decide_direction, gen_smart_tpsl, analyze_symbol, get_higher_tf, calc_signal_expiry,
    analyze_btc_macro
)

# 12 小時止損冷卻時效字典
COOLDOWN_UNTIL = {}
SIGNAL_DUPLICATE_COOLDOWN_MINUTES = 90
SIGNAL_DUPLICATE_REPRICE_PCT = 0.6


def should_skip_duplicate_notification(symbol: str, direction: str, tf: str, price: float,
                                       cooldown_minutes: int = SIGNAL_DUPLICATE_COOLDOWN_MINUTES,
                                       reprice_pct: float = SIGNAL_DUPLICATE_REPRICE_PCT):
    """防止短時間內同幣種同方向同週期重複推播。"""
    try:
        with db_manager.get_db() as conn:
            row = conn.execute(
                """
                SELECT timestamp, price
                FROM notifications
                WHERE symbol = ? AND direction = ? AND COALESCE(tf, '') = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol.upper(), direction.lower(), tf.lower()),
            ).fetchone()
        if not row:
            return False, ""

        last_ts_str = row["timestamp"]
        last_price = float(row["price"]) if row["price"] is not None else None
        if not last_ts_str or last_price is None or last_price <= 0:
            return False, ""

        try:
            last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False, ""

        now_dt = datetime.now()
        minutes_since = (now_dt - last_dt).total_seconds() / 60.0
        price_diff_pct = abs(float(price) - last_price) / last_price * 100.0

        if minutes_since <= cooldown_minutes and price_diff_pct <= reprice_pct:
            reason = f"{minutes_since:.1f} 分鐘內且價差僅 {price_diff_pct:.3f}%"
            return True, reason
        return False, ""
    except Exception as e:
        log.error(f"[Dedup] 檢查重複通知失敗: {e}")
        return False, ""

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 訊息格式化與 Telegram 推送
# ═══════════════════════════════════════════════════════════════════════════════
def fmt_price(v: float) -> str:
    if v >= 10000: return f"{v:,.2f}"
    if v >= 1: return f"{v:.4f}"
    if v >= 0.01: return f"{v:.5f}"
    return f"{v:.7f}"

def build_telegram_message(result: dict, tf: str, higher_result: dict = None, higher_tf: str = None) -> str:
    tz     = timezone(timedelta(hours=TZ_OFFSET))
    now    = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    sym    = result["symbol"]
    dir_   = result["direction"]
    entry  = result["price"]
    sl     = result["sl"]
    tps    = result["tps"]
    conds  = result["conditions"]

    dir_label = {"long": "📈 多 (LONG)", "short": "📉 空 (SHORT)", "neutral": "⚖️ 中性"}.get(dir_, "❓")
    strength = result["strength"]
    conf_label = "🔥 極強" if strength >= 70 else ("💪 強" if strength >= 50 else ("⚡ 中等" if strength >= 30 else "🌡 偏弱"))

    # MTF
    mtf_line = ""
    if higher_result and higher_tf:
        htf_dir = higher_result["direction"]
        agree   = htf_dir != "neutral" and dir_ != "neutral" and htf_dir == dir_
        if agree:
            mtf_line = f"🔗 MTF ({higher_tf.upper()}): ✅ 同向確認 ({htf_dir.upper()})"
        elif htf_dir == "neutral":
            mtf_line = f"🔗 MTF ({higher_tf.upper()}): ⚠️ 高週期中性，建議觀察"
        else:
            mtf_line = f"🔗 MTF ({higher_tf.upper()}): ❌ 方向相反 ({htf_dir.upper()})，降倉謹慎"

    # 條件詳情
    cond_names = {
        "vegas": "Vegas Tunnel", "fib": "Fib 0.618/OTE", "ob": "Order Block",
        "rsi": "RSI Reversal", "sr": "S/R Level", "bb": "Bollinger Bands", "fvg": "Fair Value Gap"
    }
    cond_lines = []
    for k, name in cond_names.items():
        c = conds[k]
        icon = "✅" if c["met"] else ("🔶" if c["near"] else "⬜")
        cond_lines.append(f"  {icon} {name}: {c['detail']}")

    # TP/SL
    sl_pct = abs(sl - entry) / entry * 100
    tp_section = ""
    for i, tp in enumerate(tps, 1):
        tp_pct = abs(tp - entry) / entry * 100
        arrow  = "▲" if tp > entry else "▼"
        tp_section += f"\n  🎯 TP{i}: {fmt_price(tp)} ({arrow}{tp_pct:.2f}%)"

    expiry = calc_signal_expiry(tf, result["count_active"])

    # 週線大級別水平支撐/阻力
    weekly_line = ""
    weekly_sup = result.get("weekly_support", {})
    if weekly_sup.get("near_support"):
        weekly_line = f"💎 週線結構: <b>✅ 貼近週線支撐 ({weekly_sup['support_level']} | 偏離 {weekly_sup['diff_pct']}%)</b>"
    elif weekly_sup.get("near_resistance"):
        weekly_line = f"💎 週線結構: <b>⚠️ 貼近週線阻力 ({weekly_sup['resistance_level']} | 偏離 {weekly_sup['diff_pct']}%)</b>"

    lines = [
        f"🚨 <b>優化開單信號 | {sym}</b>",
        f"{'─' * 28}",
        f"📅 時間:    {now} (UTC+8)",
        f"💱 幣別:    {sym}",
        f"方向:      <b>{dir_label}</b>",
        f"⏰ 週期:    {tf.upper()}" + (f" + {higher_tf.upper()}" if higher_tf else ""),
        "",
        f"💯 信心:    <b>{strength}% {conf_label}</b>",
        f"✅ 條件數:  {result['count_active']}/7  (滿足 {result['count_met']} 項)",
        f"📊 分數:    {result['score']}/14",
    ]
    if weekly_line:
        lines.append(weekly_line)
    if mtf_line:
        lines.append(mtf_line)
    lines += [
        "",
        f"{'─' * 28}",
        f"💰 進場參考: <b>{fmt_price(entry)}</b>",
        f"🛑 止損:     <b>{fmt_price(sl)}</b> (-{sl_pct:.2f}%)",
        f"🎯 停利:",
        tp_section,
        "",
        f"💡 <b>減倉建議：TP1 觸及時平倉 50% 鎖定利潤，其餘 50% 保本止損至進場價。</b>",
        f"{'─' * 28}",
        "📋 條件詳情:",
        *cond_lines,
        "",
        f"⏱  撤單時效: {expiry}",
        f"{'─' * 28}",
    ]
    return "\n".join(str(l) for l in lines)

# BTC 4H 短期趨勢過濾 (防止大盤強拉/急砸時山寨幣逆勢開單)
def check_btc_4h_trend_aligned(direction: str) -> bool:
    try:
        klines = fetch_klines("BTCUSDT", "4h", 180)
        if not klines or len(klines) < 170:
            return True
        closes = [float(k[4]) for k in klines]
        ema144 = calc_ema(closes, 144)[-1]
        ema169 = calc_ema(closes, 169)[-1]
        price = closes[-1]
        if not ema144 or not ema169:
            return True
            
        max_ema = max(ema144, ema169)
        min_ema = min(ema144, ema169)
        
        if direction == "long" and price < min_ema:
            return False
        elif direction == "short" and price > max_ema:
            return False
    except Exception as e:
        log.error(f"  [BTC 4H Filter] 檢查失敗: {e}")
    return True

# BTC 1H 價格動能百分比過濾器 (防止大盤強反彈時逆勢開空，強砸盤時逆勢開多)
def check_btc_1h_momentum_aligned(direction: str) -> bool:
    try:
        klines = fetch_klines("BTCUSDT", "1h", 10)
        if not klines or len(klines) < 4:
            return True
        recent_closes = [float(k[4]) for k in klines[-4:]]
        recent_lows = [float(k[3]) for k in klines[-4:]]
        recent_highs = [float(k[2]) for k in klines[-4:]]
        
        current_p = recent_closes[-1]
        min_low = min(recent_lows)
        max_high = max(recent_highs)
        
        rebound_pct = (current_p - min_low) / min_low * 100
        dump_pct = (max_high - current_p) / max_high * 100
        
        if direction == "short" and rebound_pct > 1.5:
            log.info(f"  [BTC 1H Filter] SHORT 被攔截：大盤處於 1H 強反彈中 (反彈幅度 {rebound_pct:.2f}% > 1.5%)")
            return False
        if direction == "long" and dump_pct > 1.5:
            log.info(f"  [BTC 1H Filter] LONG 被攔截：大盤處於 1H 強砸盤中 (下跌幅度 {dump_pct:.2f}% > 1.5%)")
            return False
    except Exception as e:
        log.error(f"  [BTC 1H Filter] 檢查失敗: {e}")
    return True

# BTC 大盤主力資金大戶持倉多空比過濾 (Plan F: 勝率飆升黃金指標)
def check_btc_smart_money_aligned(direction: str) -> bool:
    try:
        # 大戶持倉比為幣安公開接口，調用 db_manager.send_binance_request
        res = db_manager.send_binance_request("GET", "/futures/data/topLongShortPositionRatio", {"symbol": "BTCUSDT", "period": "1h", "limit": 1})
        if res.get("success") and res.get("data"):
            top_ratio = float(res["data"][0].get("longShortRatio", 1.20))
            # 💡 做空門檻放寬至 1.30（只有當大戶極度偏多 > 1.30 時才鎖死做空，解鎖正常陰跌/下跌行情的空單）
            if direction == "short" and top_ratio > 1.30:
                log.info(f"  [Smart Money Filter] SHORT 被攔截：BTC大戶持多偏向極度強烈 (多空比 {top_ratio:.2f} > 1.30)")
                return False
            if direction == "long" and top_ratio < 1.05:
                log.info(f"  [Smart Money Filter] LONG 被攔截：BTC大戶看空偏向強烈 (多空比 {top_ratio:.2f} < 1.05)")
                return False
    except Exception as e:
        log.error(f"  [Smart Money Filter] 檢查失敗: {e}")
    return True

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 即時一輪掃描 (防重複開單核心與大盤聯動過濾)
# ═══════════════════════════════════════════════════════════════════════════════
def scan_once() -> int:
    tz  = timezone(timedelta(hours=TZ_OFFSET))
    ts  = datetime.now(tz).strftime("%H:%M:%S")
    log.info(f"{'═' * 50}")
    log.info(f"  開始自動定期掃描 {len(COINS)} 個幣種  [{ts}]")
    log.info(f"{'═' * 50}")

    try:
        btc_res = analyze_btc_macro()
        if btc_res and btc_res.get("price", 0) > 0:
            BTC_MACRO_STATE.clear()
            BTC_MACRO_STATE.update(btc_res)
            log.info(f"  [BTC Filter] Price: {btc_res['price']:.1f} | State: {btc_res['state']}")
    except Exception as e:
        log.error(f"  [BTC Filter] Update failed: {e}")

    active_trades = load_active_trades()
    signals_sent = 0

    # 獲取當前所有幣種的價格以更新 active positions (使用 1H 價格作為最新即時價格即可)
    current_prices = {}
    for sym in COINS:
        try:
            kl = fetch_klines(sym, "1h", 5)
            if kl:
                current_prices[sym] = float(kl[-1][4])
        except Exception:
            pass
            
    # 更新持倉狀態
    active_trades = update_active_trades_state(active_trades, current_prices)
    save_active_trades(active_trades)

    for tf in SCAN_TIMEFRAMES:
        higher_tf = get_higher_tf(tf)
        for sym in COINS:
            try:
                time.sleep(REQUEST_DELAY)
                klines = fetch_klines(sym, tf, 200)
                ticker = fetch_ticker(sym)

                if not klines:
                    continue

                higher_klines = fetch_klines(sym, higher_tf, 200)
                higher_result = analyze_symbol(sym, higher_klines, ticker) if higher_klines else None
                result = analyze_symbol(sym, klines, ticker)

                if not result:
                    continue

                # MTF Trend 同向確認 (高週期大勢過濾)
                mtf_tag = ""
                mtf_conflict = False
                # ── 自適應開單門檻優化 ──
                dyn_min_conds = MIN_CONDITIONS  # 預設 3
                dyn_min_score = MIN_SCORE        # 預設 5
                sig_dir = result["direction"]
                
                if higher_result:
                    # 1. 基礎 Vegas 趨勢確認 (EMA交叉)
                    if higher_result["trend"] != "neutral":
                        agree = higher_result["trend"] == result["direction"]
                        mtf_tag = " MTF✓" if agree else " MTF❌"
                        if not agree:
                            mtf_conflict = True
                    else:
                        mtf_tag = " MTF?"
                        
                    # 2. 強勢價格過濾 (防逆勢摸頂/抄底)
                    h_price = higher_result.get("price", 0.0)
                    h_e144 = higher_result.get("last_e144")
                    h_e169 = higher_result.get("last_e169")
                    if h_price > 0 and h_e144 and h_e169:
                        h_max_ema = max(h_e144, h_e169)
                        h_min_ema = min(h_e144, h_e169)
                        price = result["price"]
                        
                        # 如果要開空，但高週期價格已經站上 Vegas 通道上方，視為逆勢，強制跳過
                        if result["direction"] == "short" and price > h_max_ema:
                            mtf_conflict = True
                            mtf_tag += " (Vegas上軌過濾空單)"
                        # 如果要開多，但高週期價格已經跌破 Vegas 通道下方，視為逆勢，強制跳過
                        elif result["direction"] == "long" and price < h_min_ema:
                            mtf_conflict = True
                            mtf_tag += " (Vegas下軌過濾多單)"

                log.info(
                    f"  [{tf.upper()}] {sym:<12} | 條件={result['count_active']}/7"
                    f"  分={result['score']:02d}"
                    f"  {result['direction'].upper():<7}"
                    f"  {result['strength']:3d}%{mtf_tag}"
                )

                # 記錄未達標的原因，方便使用者透明追蹤
                if sig_dir != "neutral" and not (result["count_met"] >= dyn_min_conds and result["score"] >= dyn_min_score):
                    log.info(f"  ⚡ {sym} [{tf.upper()}] {sig_dir.upper()} 門檻未達標 (需要 條件>={dyn_min_conds} 分>={dyn_min_score}，實際 條件={result['count_met']} 分={result['score']})，略過。")

                # 滿足自適應門檻、非中性、且高週期大趨勢無衝突
                if sig_dir != "neutral" and result["count_met"] >= dyn_min_conds and result["score"] >= dyn_min_score:

                    # 2. RSI 反彈動能過濾器：如果是空單且 RSI 反彈動能過強，跳過
                    if result["direction"] == "short" and result.get("last_rsi") is not None and result["last_rsi"] > 58:
                        log.info(f"  [RSI Filter] {sym} [{tf.upper()}] 反彈動能過強 (RSI={result['last_rsi']:.1f})，暫停做空。")
                        continue

                    if mtf_conflict:
                        log.info(f"  ⚠️ {sym} [{tf.upper()}] {result['direction'].upper()} 與高週期 {higher_tf.upper()} 大趨勢相反，跳過信號。")
                        continue
                        
                    direction = result["direction"]
                        
                    # BTC 4H 短期趨勢過濾 (防止大盤強拉/急砸時山寨幣逆勢開單)
                    if not check_btc_4h_trend_aligned(direction):
                        log.info(f"  [BTC 4H Filter] {sym} [{tf.upper()}] {direction.upper()} 被 BTC 4H 短期大勢過濾阻斷，略過。")
                        continue
                        
                    # BTC 1H 價格動能過濾 (防 V 轉與強單邊逆勢)
                    if not check_btc_1h_momentum_aligned(direction):
                        log.info(f"  [BTC 1H Filter] {sym} [{tf.upper()}] {direction.upper()} 被 BTC 1H 百分比動能過濾阻斷，略過。")
                        continue
                        
                    # BTC 1H 大戶主力資金多空比過濾 (Plan F: 大盤強勢背離防守)
                    if not check_btc_smart_money_aligned(direction):
                        log.info(f"  [Smart Money Filter] {sym} [{tf.upper()}] {direction.upper()} 被 BTC 大戶主力資金過濾阻斷，略過。")
                        continue
                        
                    # 防重複開單：檢查此幣種是否已有同方向 active trade (跨週期 1h/4h 統一防重)
                    trade_key = f"{sym}_{result['direction']}"
                    if any(k.startswith(f"{sym}_") and k.endswith(f"_{result['direction']}") for k in active_trades):
                        log.info(f"  ⚠️ {sym} [{tf.upper()}] {result['direction'].upper()} 開單信號已在 active_trades 中進行中，跳過重複通知與開單。")
                        continue
                        
                    # 防重複自動下單：檢查資料庫 active_positions 是否已有同幣種未平倉實體部位
                    try:
                        with db_manager.get_db() as conn:
                            exist_pos = conn.execute(
                                "SELECT COUNT(*) as cnt FROM active_positions WHERE symbol = ? AND status = 'OPEN'",
                                (sym,)
                            ).fetchone()
                            if exist_pos and exist_pos["cnt"] > 0:
                                log.info(f"  ⚠️ [Auto Trade Check] {sym} 資料庫中已有進行中之實體持倉，跳過短時間重複下單。")
                                continue
                    except Exception as db_pos_err:
                        log.error(f"檢查 {sym} 實體持倉異常: {db_pos_err}")
                        
                    # ⚡ [Plan B + Plan E 策略優化升級]
                    entry_val = float(result["price"])
                    initial_sl = float(result["sl"])
                    sl_diff_pct = abs(initial_sl - entry_val) / entry_val * 100
                    
                    # 1. Plan B：設置保底門檻 (BTC: 1.2% / 山寨幣: 2.5%)
                    threshold = 1.2 if sym == "BTCUSDT" else 2.5
                    if sl_diff_pct < threshold:
                        if result["direction"] == "long":
                            new_sl = entry_val * (1.0 - threshold / 100.0)
                        else:
                            new_sl = entry_val * (1.0 + threshold / 100.0)
                            
                        sl_str = str(initial_sl)
                        decimal_places = len(sl_str.split('.')[1]) if '.' in sl_str else 4
                        result["sl"] = round(new_sl, decimal_places)
                        log.info(f"🛡️ [Plan B] {sym} 初始止損空間為 {sl_diff_pct:.2f}% < {threshold}%，已強行保底拉寬至 {threshold}%，新 SL: {result['sl']}")


                        
                    # DB 層防重複推播：短時間同方向同週期且價格幾乎相同，不重複通知
                    skip_dup, dup_reason = should_skip_duplicate_notification(
                        sym, result["direction"], tf, float(result["price"])
                    )
                    if skip_dup:
                        log.info(f"  🧊 [Dedup Alert] {sym} [{tf.upper()}] {result['direction'].upper()} 重複訊號略過：{dup_reason}")
                        continue

                    # 發送 Telegram
                    msg  = build_telegram_message(result, tf, higher_result, higher_tf)
                    sent = send_telegram(msg)
                    
                    if sent:
                        signals_sent += 1
                        
                        # 儲存通知紀錄至 SQLite 數據庫
                        db_manager.insert_notification(
                            symbol=sym,
                            direction=result["direction"],
                            price=result["price"],
                            sl=result["sl"],
                            tps=result["tps"],
                            score=result["score"],
                            count_active=result["count_active"],
                            expiry=calc_signal_expiry(tf, result["count_active"]),
                            tf=tf
                        )
                        
                        # 同步寫入歷史分析表 (類別為 SIGNAL，狀態為 PENDING)
                        cond_names = {"vegas": "Vegas", "fib": "Fib", "ob": "OB", "rsi": "RSI", "sr": "S/R", "bb": "BB", "fvg": "FVG"}
                        met_conds = [name for k, name in cond_names.items() if result["conditions"].get(k, {}).get("met")]
                        logic_str = " + ".join(met_conds)
                        if not logic_str:
                            logic_str = "指標共振"
                        db_manager.insert_historical_trade(
                            symbol=sym,
                            direction=result["direction"],
                            entry=result["price"],
                            sl=result["sl"],
                            tps=result["tps"],
                            leverage=6,
                            logic=logic_str,
                            trade_type="SIGNAL",
                            pionex_order_id=None,
                            reach="PENDING",
                            tf=tf,
                            note="系統自動掃描開單建議"
                        )
                        
                        # 加入 Active Trade
                        active_trades[trade_key] = {
                            "symbol": sym,
                            "direction": result["direction"],
                            "entry": result["price"],
                            "sl": result["sl"],
                            "tps": result["tps"],
                            "reached_tp": 0,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        save_active_trades(active_trades)
                        
                        # 4. 自動託管開單 (如果有啟用自動開單且滿足自訂條件)
                        auto_enabled = db_manager.get_setting("auto_trade_enabled", "false") == "true"
                        if auto_enabled:
                            try:
                                # 實施最大併發持倉限制 (僅限自動下單)
                                max_concurrent = int(db_manager.get_setting("auto_trade_max_concurrent", "3"))
                                with db_manager.get_db() as conn:
                                    row = conn.execute("SELECT COUNT(*) as cnt FROM active_positions WHERE status = 'OPEN'").fetchone()
                                    active_positions_count = row["cnt"] if row else 0
                                    
                                if active_positions_count >= max_concurrent:
                                    log.info(f"⚡ [Auto Trade] 當前實體持倉 ({active_positions_count}) 已達併發上限 ({max_concurrent})，略過自動開單。")
                                else:
                                    auto_margin = float(db_manager.get_setting("auto_trade_margin", "20.0"))
                                    auto_leverage = int(db_manager.get_setting("auto_trade_leverage", "10"))
                                    
                                    # 1. 檢查是否啟用完全託管
                                    fully_managed = db_manager.get_setting("auto_trade_fully_managed", "false") == "true"
                                    auto_should_trigger = False
                                    
                                    if fully_managed:
                                        auto_should_trigger = True
                                        log.info(f"⚡ [Auto Trade] 信號 {sym} 啟用完全託管，無視自訂信心與共振指標限制，準備執行自動開單...")
                                    else:
                                        auto_conf = int(db_manager.get_setting("auto_trade_confidence", "70"))
                                        auto_min_conds = int(db_manager.get_setting("auto_trade_min_conditions", "4"))
                                        
                                        auto_req_inds = db_manager.get_setting("auto_trade_required_indicators", "")
                                        req_inds_list = [x.strip().lower() for x in auto_req_inds.split(",") if x.strip()]
                                        
                                        req_inds_met = True
                                        failed_ind = None
                                        for ind in req_inds_list:
                                            ind_met = result.get("conditions", {}).get(ind, {}).get("met", False)
                                            if not ind_met:
                                                req_inds_met = False
                                                failed_ind = ind
                                                break
                                        
                                        if not req_inds_met:
                                            log.info(f"⚡ [Auto Trade] 信號 {sym} 略過自動開單：必選指標條件 {failed_ind.upper()} 未符合。")
                                        elif result["strength"] >= auto_conf and result["count_met"] >= auto_min_conds:
                                            auto_should_trigger = True
                                            log.info(f"⚡ [Auto Trade] 信號 {sym} 滿足自訂自動開單門檻 (信心值: {result['strength']}% >= {auto_conf}%, 滿足數: {result['count_met']} >= {auto_min_conds})，準備執行自動開單...")
                                            
                                    if auto_should_trigger:
                                        # ── OI/CVD 數據過濾 ──
                                        use_oi_cvd = db_manager.get_setting("auto_trade_use_oi_cvd", "false") == "true"
                                        if use_oi_cvd:
                                            try:
                                                oi_cvd_res = db_manager.get_binance_oi_cvd(sym, period="5m", limit=30)
                                                if oi_cvd_res.get("success"):
                                                    oi_list = oi_cvd_res.get("oi", [])
                                                    vol_list = oi_cvd_res.get("vol", [])
                                                    if len(oi_list) >= 5 and len(vol_list) >= 5:
                                                        oi_values = [float(x["sumOpenInterest"]) for x in oi_list]
                                                        initial_oi = oi_values[-5]
                                                        latest_oi = oi_values[-1]
                                                        oi_change_pct = ((latest_oi - initial_oi) / initial_oi) * 100 if initial_oi > 0 else 0.0
                                                        
                                                        net_taker_volumes = []
                                                        for vol in vol_list[-5:]:
                                                            buy_vol = float(vol["buyVol"])
                                                            sell_vol = float(vol["sellVol"])
                                                            net_taker_volumes.append(buy_vol - sell_vol)
                                                        sum_net_taker = sum(net_taker_volumes)
                                                        
                                                        if result["direction"].upper() == "LONG" and oi_change_pct < -5.0 and sum_net_taker > 0:
                                                            auto_should_trigger = False
                                                            log.info(f"🚫 [Auto Trade] 信號 {sym} 觸發 Short Squeeze 過濾：OI 跌幅 {oi_change_pct:.1f}%，CVD 流入 {sum_net_taker:.2f}，略過自動做多。")
                                                            send_telegram(f"⚠️ <b>[OI/CVD 風控過濾] {sym}</b>\n原因: 偵測到 Short Squeeze (OI 暴跌 {oi_change_pct:.1f}%)，多頭力量源自空單止損，已略過自動開單。")
                                                        elif result["direction"].upper() == "SHORT" and oi_change_pct < -5.0 and sum_net_taker < 0:
                                                            auto_should_trigger = False
                                                            log.info(f"🚫 [Auto Trade] 信號 {sym} 觸發 Long Squeeze 過濾：OI 跌幅 {oi_change_pct:.1f}%，CVD 流出 {sum_net_taker:.2f}，略過自動做空。")
                                                            send_telegram(f"⚠️ <b>[OI/CVD 風控過濾] {sym}</b>\n原因: 偵測到 Long Squeeze (OI 暴跌 {oi_change_pct:.1f}%)，空頭力量源自多單踩踏，已略過自動開單。")
                                            except Exception as filter_err:
                                                log.error(f"❌ [Auto Trade] 執行 OI/CVD 數據過濾異常: {filter_err}")

                                    # ── 安全檢查：同幣種同方向已有 OPEN 持倉，拒絕開單（防止干擾手動倉）──
                                    if auto_should_trigger:
                                        bot_side = "BUY" if result["direction"].lower() == "long" else "SELL"
                                        with db_manager.get_db() as conn:
                                            conflict_row = conn.execute(
                                                "SELECT id FROM active_positions WHERE symbol = ? AND side = ? AND status = 'OPEN'",
                                                (sym.upper(), bot_side)
                                            ).fetchone()
                                        if conflict_row:
                                            auto_should_trigger = False
                                            log.info(f"🚧 [Auto Trade] {sym} {result['direction'].upper()} 已有 OPEN 持倉 (ID: {conflict_row['id']})，跳過自動開單，防止干擾手動倉。")

                                    if auto_should_trigger:
                                        # ── 動態 ATR 保證金計算 ──
                                        use_dynamic_atr = db_manager.get_setting("auto_trade_use_dynamic_atr", "false") == "true"
                                        if use_dynamic_atr:
                                            try:
                                                bal_res = db_manager.get_binance_futures_balance()
                                                if bal_res.get("success"):
                                                    avail_balance = float(bal_res.get("balance", 0.0))
                                                    risk_pct = float(db_manager.get_setting("auto_trade_risk_pct", "1.0"))
                                                    max_margin_pct = float(db_manager.get_setting("auto_trade_max_margin_pct", "10.0"))
                                                    
                                                    entry_price = float(result["price"])
                                                    sl_price = float(result["sl"])
                                                    
                                                    if entry_price > 0 and sl_price > 0 and entry_price != sl_price:
                                                        risk_amount = avail_balance * (risk_pct / 100.0)
                                                        sl_distance = abs(entry_price - sl_price) / entry_price
                                                        
                                                        # 名義開倉價值與保證金
                                                        nominal_value = risk_amount / sl_distance
                                                        calculated_margin = nominal_value / auto_leverage
                                                        
                                                        # 最大保證金限制
                                                        max_margin = avail_balance * (max_margin_pct / 100.0)
                                                        
                                                        final_margin = min(calculated_margin, max_margin)
                                                        if final_margin < 2.0:
                                                            final_margin = 2.0
                                                            
                                                        log.info(f"💎 [Auto Trade] 動態風控計算成功: "
                                                                 f"餘額: {avail_balance:.2f} USDT, "
                                                                 f"風險比: {risk_pct}%, 風險額: {risk_amount:.2f} USDT, "
                                                                 f"止損距離: {sl_distance*100:.2f}%, "
                                                                 f"保證金: {calculated_margin:.2f} USDT, "
                                                                 f"最終保證金: {final_margin:.2f} USDT")
                                                        auto_margin = round(final_margin, 2)
                                                    else:
                                                        log.warning(f"⚠️ [Auto Trade] 進場價 {entry_price} 或止損價 {sl_price} 無效，使用固定保證金 {auto_margin}")
                                                else:
                                                    log.warning(f"⚠️ [Auto Trade] 無法獲取餘額 ({bal_res.get('error')})，使用固定保證金 {auto_margin}")
                                            except Exception as calc_err:
                                                log.error(f"❌ [Auto Trade] 計算動態保證金時發生錯誤: {calc_err}，使用固定保證金 {auto_margin}")

                                        # ── 進攻加碼 (Smart Money Position Booster) ──
                                        # 注意：booster 觸發條件嚴格化，且需用戶在設定中主動啟用
                                        # LONG 門檻：多空比 >= 1.50（主力極度偏多），SHORT 門檻：多空比 <= 0.80（主力極度偏空）
                                        is_booster_active = 0
                                        booster_enabled = db_manager.get_setting("auto_trade_booster_enabled", "false") == "true"
                                        if booster_enabled:
                                            try:
                                                res_ratio = db_manager.send_binance_request("GET", "/futures/data/topLongShortPositionRatio", {"symbol": "BTCUSDT", "period": "1h", "limit": 1})
                                                if res_ratio.get("success") and res_ratio.get("data"):
                                                    top_ratio = float(res_ratio["data"][0].get("longShortRatio", 1.20))
                                                    dir_lower = result["direction"].lower()
                                                    log.info(f"  [Smart Money Booster] 當前 BTC 大戶多空比: {top_ratio:.3f}")
                                                    # 門檻嚴格化：做多須 >= 1.50（極度偏多），做空須 <= 0.80（極度偏空）
                                                    if dir_lower == "long" and top_ratio >= 1.50:
                                                        is_booster_active = 1
                                                    elif dir_lower == "short" and top_ratio <= 0.80:
                                                        is_booster_active = 1
                                                    else:
                                                        log.info(f"  [Smart Money Booster] 多空比 {top_ratio:.3f} 未達重倉門檻，維持標準保證金。")
                                            except Exception as booster_err:
                                                log.warning(f"判定 Booster 加碼失敗: {booster_err}")
                                        else:
                                            log.info(f"  [Smart Money Booster] Booster 未啟用（設定關閉），維持標準保證金。")
                                            
                                        if is_booster_active == 1:
                                            auto_margin = round(auto_margin * 2.5, 2)
                                            log.info(f"🔥 [Smart Money Booster] 檢測到強烈主力共振！保證金放大 2.5 倍為: {auto_margin} USDT")

                                        auto_res = db_manager.place_binance_futures_order(
                                            symbol=sym,
                                            direction=result["direction"],
                                            leverage=auto_leverage,
                                            margin=auto_margin,
                                            sl_price=result["sl"],
                                            tps=result["tps"],
                                            is_market=True,
                                            is_booster=is_booster_active
                                        )
                                        if auto_res.get("success"):
                                            log.info(f"✅ [Auto Trade] 信號 {sym} 自動開單成功！訂單 ID: {auto_res.get('order_id')}")
                                            booster_txt = "\n🔥 <b>[趨勢共振重倉加碼 2.5x 啟用]</b>" if is_booster_active == 1 else ""
                                            send_telegram(f"⚡ <b>[自動託管開單成功] {sym}</b>{booster_txt}\n方向: {result['direction'].upper()}\n槓桿: {auto_leverage}x\n金額: {auto_margin} USDT")
                                        else:
                                            log.error(f"❌ [Auto Trade] 信號 {sym} 自動開單失敗: {auto_res.get('error')}")
                            except Exception as auto_err:
                                log.error(f"❌ [Auto Trade] 執行自動開單發生異常: {auto_err}")
                                
                    time.sleep(1.0)

            except Exception as e:
                log.error(f"  {sym} [{tf.upper()}] 掃描錯誤: {e}")

    log.info(f"  定期掃描完成，新增發送 {signals_sent} 則通知。當前監控中活躍信號數: {len(active_trades)}")
    return signals_sent

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ AI 智能情緒策略並行監控邏輯
# ═══════════════════════════════════════════════════════════════════════════════
from analyzers.ai_sentiment import load_ai_sentiment_state

LAST_SENTIMENT_UPDATE_TIME = 0

def analyze_symbol_ai_sentiment(symbol, klines_1h, klines_15m):
    if not klines_1h or len(klines_1h) < 90 or not klines_15m or len(klines_15m) < 90:
        return None
        
    closes_1h = [float(x[4]) for x in klines_1h]
    highs_1h = [float(x[2]) for x in klines_1h]
    lows_1h = [float(x[3]) for x in klines_1h]
    price = closes_1h[-1]
    
    # 0. ATR 波動度健康區間過濾 (0.6% ~ 3.5%，過濾死水盤整與極端暴洗)
    atr_1h = calc_atr(highs_1h, lows_1h, closes_1h, 14)[-1]
    atr_ratio = (atr_1h / price * 100) if (atr_1h and price > 0) else 1.0
    if atr_ratio < 0.6 or atr_ratio > 3.5:
        return {
            "price": price,
            "direction": "neutral",
            "vegas_tunnel": f"ATR Regime Filter ({atr_ratio:.2f}%)",
            "sl": price,
            "tps": [price, price, price, price]
        }
    
    # 1. 優先判斷幣種 1H 市場結構突破 (BOS) 趨勢 (加上 0.5x ATR 突破緩衝區，杜絕假突破刺針)
    swings_1h = find_swings(highs_1h, lows_1h, 5)
    sh, sl_list = swings_1h.get("sh", []), swings_1h.get("sl", [])
    structural_trend = "neutral"
    
    if sh and sl_list:
        last_sh = sh[-1]["v"]
        last_sl = sl_list[-1]["v"]
        
        # 💡 方案 A 最佳優化：0.5x ATR 突破強度緩衝
        bos_long_threshold = last_sh + 0.5 * atr_1h
        bos_short_threshold = last_sl - 0.5 * atr_1h
        
        if price > bos_long_threshold:
            structural_trend = "long"
        elif price < bos_short_threshold:
            structural_trend = "short"
            
    if structural_trend == "neutral":
        return {
            "price": price,
            "direction": "neutral",
            "vegas_tunnel": "1H Structure Neutral",
            "sl": price,
            "tps": [price, price, price, price]
        }
        
    # 2. 判斷幣種 15m 短線回調觸發 (EMA21 觸碰 + K線反轉)
    closes_15m = [float(x[4]) for x in klines_15m]
    ema21_15m = calc_ema(closes_15m, 21)[-1]
    
    open_15m = float(klines_15m[-1][1])
    close_15m = float(klines_15m[-1][4])
    rev_long = close_15m > open_15m and close_15m > closes_15m[-2]
    rev_short = close_15m < open_15m and close_15m < closes_15m[-2]
    
    triggered = False
    if structural_trend == "long":
        dist = (close_15m - ema21_15m)/ema21_15m
        if -0.003 <= dist <= 0.003 and rev_long:
            triggered = True
    elif structural_trend == "short":
        dist = (ema21_15m - close_15m)/ema21_15m
        if -0.003 <= dist <= 0.003 and rev_short:
            triggered = True
            
    if not triggered:
        return {
            "price": price,
            "direction": "neutral",
            "vegas_tunnel": f"BOS: {structural_trend.upper()} | Pullback Pending",
            "sl": price,
            "tps": [price, price, price, price]
        }

    # 3. 技術候選成立後，先回傳純技術方向；AI 放行判斷在 scan_once_ai_sentiment 進行
    allowed_direction = structural_trend

    # 4. K 線動態結構止損 (Vegas 通道/波段高低點) 與 4H 斐波那契/前高強阻力動態止盈
    ema144_vals = [v for v in calc_ema(closes_1h, 144) if v is not None]
    ema169_vals = [v for v in calc_ema(closes_1h, 169) if v is not None]
    last_e144 = ema144_vals[-1] if ema144_vals else price * 0.96
    last_e169 = ema169_vals[-1] if ema169_vals else price * 0.96
    vegas_supp = min(last_e144, last_e169)
    vegas_resi = max(last_e144, last_e169)

    recent_low = min(lows_1h[-24:]) if len(lows_1h) >= 24 else price * 0.96
    recent_high = max(highs_1h[-24:]) if len(highs_1h) >= 24 else price * 1.04

    swing_high_60 = max(highs_1h[-60:]) if len(highs_1h) >= 60 else price * 1.05
    swing_low_60 = min(lows_1h[-60:]) if len(lows_1h) >= 60 else price * 0.95
    wave_diff = max(swing_high_60 - swing_low_60, price * 0.03)

    if allowed_direction == "long":
        sl = min(vegas_supp, recent_low) * 0.995 # 留給行情 Vegas / 前低 0.5% 回踩防守空間
        sl_dist = price - sl
        if sl_dist <= 0:
            sl_dist = price * 0.03
            sl = price - sl_dist

        # 4H 斐波那契與前高強阻力位
        fib_0618 = swing_low_60 + 0.618 * wave_diff
        fib_0786 = swing_low_60 + 0.786 * wave_diff
        fib_1000 = swing_high_60  # 前高強阻力位
        fib_1272 = swing_low_60 + 1.272 * wave_diff
        fib_1618 = swing_low_60 + 1.618 * wave_diff

        candidates = [lvl for lvl in [fib_0618, fib_0786, fib_1000, fib_1272, fib_1618] if lvl > price + 0.5 * sl_dist]
        
        tp1 = candidates[0] if len(candidates) > 0 else price + 1.5 * sl_dist
        tp2 = candidates[1] if len(candidates) > 1 else price + 2.3 * sl_dist
        tp3 = candidates[2] if len(candidates) > 2 else price + 3.2 * sl_dist
        tp4 = candidates[3] if len(candidates) > 3 else price + 4.5 * sl_dist

        tps = [tp1, tp2, tp3, tp4]
    else:
        sl = max(vegas_resi, recent_high) * 1.005 # 留給行情 Vegas / 前高 0.5% 防守空間
        sl_dist = sl - price
        if sl_dist <= 0:
            sl_dist = price * 0.03
            sl = price + sl_dist

        fib_0618 = swing_high_60 - 0.618 * wave_diff
        fib_0786 = swing_high_60 - 0.786 * wave_diff
        fib_1000 = swing_low_60  # 前低強支撐位
        fib_1272 = swing_high_60 - 1.272 * wave_diff
        fib_1618 = swing_high_60 - 1.618 * wave_diff

        candidates = [lvl for lvl in [fib_0618, fib_0786, fib_1000, fib_1272, fib_1618] if lvl < price - 0.5 * sl_dist]

        tp1 = candidates[0] if len(candidates) > 0 else price - 1.5 * sl_dist
        tp2 = candidates[1] if len(candidates) > 1 else price - 2.3 * sl_dist
        tp3 = candidates[2] if len(candidates) > 2 else price - 3.2 * sl_dist
        tp4 = candidates[3] if len(candidates) > 3 else price - 4.5 * sl_dist

        tps = [tp1, tp2, tp3, tp4]
    
    return {
        "price": price,
        "direction": allowed_direction,
        "vegas_tunnel": f"BOS {structural_trend.upper()} | Pullback Met",
        "sl": sl,
        "tps": tps
    }

# 多空人數比獲取函式 (修復 NameError)
def fetch_ls_ratio(symbol: str, period: str = "1h", limit: int = 30) -> list:
    base_url = "https://fapi.binance.com"
    sym = _normalize(symbol)
    try:
        r = requests.get(f"{base_url}/futures/data/globalLongShortAccountRatio", params={"symbol": sym, "period": period, "limit": limit}, timeout=5)
        if r.ok and r.json():
            return r.json()
    except Exception as e:
        log.error(f"獲取 {sym} 多空比失敗: {e}")
    return []

def scan_once_ai_sentiment():
    log.info("=" * 60)
    log.info("  🚀 開始 AI 智能情緒策略監控掃描...")
    log.info("=" * 60)
    
    from server.web_server import AI_SENTIMENT_SCAN_RESULTS, AI_SENTIMENT_FUNNEL
    
    global LAST_SENTIMENT_UPDATE_TIME
    now_ts = time.time()
    # 30分鐘 (1800秒) 自動更新一次 AI 輿情與期貨大戶數據
    if now_ts - LAST_SENTIMENT_UPDATE_TIME > 1800:
        log.info("  ⏳ [AI Sentiment] 已達 30 分鐘更新週期，自動執行 AI 輿情與期貨數據刷新...")
        try:
            from analyzers.ai_sentiment import update_ai_sentiment_state
            update_ai_sentiment_state()
            LAST_SENTIMENT_UPDATE_TIME = now_ts
        except Exception as e:
            log.error(f"  [AI Sentiment] 自動刷新輿情失敗: {e}")
            
    state = load_ai_sentiment_state()
    sentiment = state.get("sentiment", "NEUTRAL")
    ai_sentiment_enabled = str(state.get("auto_enabled", "false")).lower() == "true"
    log.info(f"  [AI Sentiment] 當前宏觀市場情緒: {sentiment} ({state.get('reason', '')})")
    max_same_direction = max(1, min(4, int(state.get("max_same_direction", "2"))))
    reentry_cooldown_minutes = max(0, int(state.get("reentry_cooldown_minutes", "90")))
    
    active_trades = load_active_trades_ai_sentiment()
    
    # Update active trades with 1h prices
    ai_coins = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "UNIUSDT", "JTOUSDT", "DOTUSDT"
    ]
    current_prices = {}
    for sym in ai_coins:
        try:
            kl = fetch_klines(sym, "1h", 5)
            if kl:
                current_prices[sym] = float(kl[-1][4])
        except Exception:
            pass
    active_trades = update_active_trades_state(active_trades, current_prices)
    save_active_trades_ai_sentiment(active_trades)
    
    signals_sent = 0
    funnel = {
        "updated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "symbols_scanned": 0,
        "insufficient_data": 0,
        "technical_candidates": 0,
        "ai_blocked": 0,
        "ls_blocked": 0,
        "short_guard_blocked": 0,
        "reentry_blocked": 0,
        "final_signals": 0
    }
    
    # We scan major assets BTC, ETH, SOL, BNB, XRP, and DOGE for AI Sentiment strategy
    for sym in ai_coins:
        try:
            time.sleep(REQUEST_DELAY)
            funnel["symbols_scanned"] += 1
            
            klines_1h = fetch_klines(sym, "1h", limit=100)
            klines_15m = fetch_klines(sym, "15m", limit=100)
            if not klines_1h or len(klines_1h) < 90 or not klines_15m or len(klines_15m) < 90:
                log.info(f"  [AI Sentiment] {sym} 無足夠 K 線歷史")
                funnel["insufficient_data"] += 1
                continue
                
            res = analyze_symbol_ai_sentiment(sym, klines_1h, klines_15m)
            if not res:
                continue
                
            sig_dir = res["direction"]
            price = res["price"]
            sl = res["sl"]
            tps = res["tps"]
            
            # ── 🧬 智能反向開單邏輯 (Reversal Flip)：超跌反彈做多 / 超買回調做空 ──
            flipped = False
            flip_reason = ""
            skip_filters = False
            
            if sig_dir != "neutral":
                try:
                    closes_1h = [float(x[4]) for x in klines_1h]
                    highs_1h = [float(x[2]) for x in klines_1h]
                    lows_1h = [float(x[3]) for x in klines_1h]
                    
                    # 1. 取得 1H RSI
                    rsi_vals = calc_rsi(closes_1h, 14)
                    last_rsi = rsi_vals[-1] if (rsi_vals and rsi_vals[-1] is not None) else 50.0
                    
                    # 2. 取得大盤 1H 反彈與砸盤幅度
                    btc_klines = fetch_klines("BTCUSDT", "1h", 10)
                    btc_closes = [float(k[4]) for k in btc_klines[-4:]] if btc_klines else []
                    btc_lows = [float(k[3]) for k in btc_klines[-4:]] if btc_klines else []
                    btc_highs = [float(k[2]) for k in btc_klines[-4:]] if btc_klines else []
                    
                    btc_rebound = (btc_closes[-1] - min(btc_lows)) / min(btc_lows) * 100 if (btc_closes and btc_lows and min(btc_lows) > 0) else 0.0
                    btc_dump = (max(btc_highs) - btc_closes[-1]) / max(btc_highs) * 100 if (btc_closes and btc_highs and max(btc_highs) > 0) else 0.0
                    
                    # 3. 取得大戶多空比
                    ls_res = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=1h&limit=5", timeout=5)
                    top_ratio = float(ls_res.json()[-1]["longShortRatio"]) if (ls_res.ok and isinstance(ls_res.json(), list) and len(ls_res.json()) > 0) else 1.18
                    
                    # 判斷反向開多條件：做空信號 + RSI超賣 + 大盤強力反彈 + 主力做多 ➔ 翻轉為做多
                    if sig_dir == "short" and (last_rsi < 38 or sym == "BTCUSDT") and btc_rebound > 1.0 and top_ratio > 1.25:
                        sig_dir = "long"
                        flipped = True
                        skip_filters = True
                        flip_reason = f"超跌反彈翻轉做多 (RSI={last_rsi:.1f}, BTC反彈={btc_rebound:.2f}%, 大戶={top_ratio:.2f})"
                        
                    # 判斷反向做空條件：做多信號 + RSI超買 + 大盤強力砸盤 + 主力做空 ➔ 翻轉為做空
                    elif sig_dir == "long" and (last_rsi > 65 or sym == "BTCUSDT") and btc_dump > 1.0 and top_ratio < 1.05:
                        sig_dir = "short"
                        flipped = True
                        skip_filters = True
                        flip_reason = f"超買回調翻轉做空 (RSI={last_rsi:.1f}, BTC下跌={btc_dump:.2f}%, 大戶={top_ratio:.2f})"
                        
                    if flipped:
                        log.info(f"  🔥 [Reversal Flip] {sym} 觸發信號反轉！原因: {flip_reason}")
                        atr_1h = calc_atr(highs_1h, lows_1h, closes_1h, 14)[-1]
                        sl_dist = 1.0 * atr_1h # 逆勢反彈單採用 1.0x ATR 超窄止損，鎖定極高盈虧比
                        sl = (price - sl_dist) if sig_dir == "long" else (price + sl_dist)
                        tps = [
                            price + 1.5 * atr_1h if sig_dir == "long" else price - 1.5 * atr_1h,
                            price + 3.0 * atr_1h if sig_dir == "long" else price - 3.0 * atr_1h,
                            price + 4.5 * atr_1h if sig_dir == "long" else price - 4.5 * atr_1h,
                            price + 6.0 * atr_1h if sig_dir == "long" else price - 6.0 * atr_1h,
                        ]
                except Exception as flip_err:
                    log.error(f"  [AI Sentiment] 翻轉邏輯計算出錯: {flip_err}")
            
            # 獲取多空人數比
            ls_data = fetch_ls_ratio(sym, "1h", limit=24)
            ls_ratio_str = "-"
            current_ratio = 1.0
            if ls_data:
                ratios = [float(x["longShortRatio"]) for x in ls_data if "longShortRatio" in x]
                if ratios:
                    current_ratio = ratios[-1]
                    ls_ratio_str = f"{current_ratio:.4f}"
                
            # Write results for frontend dashboard
            res_tunnel = res["vegas_tunnel"] if not flipped else flip_reason
            AI_SENTIMENT_SCAN_RESULTS[sym] = {
                "time": datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S"),
                "symbol": sym,
                "price": price,
                "score": 0,  # Legacy field, keep 0
                "direction": sig_dir,
                "vegas_tunnel": res_tunnel,
                "ls_ratio": ls_ratio_str
            }
            
            if sig_dir != "neutral":
                funnel["technical_candidates"] += 1
                log.info(f"  🔥 [AI Sentiment Trigger] {sym:<10} | 方向={sig_dir.upper():<7} | 狀態={res_tunnel} | 多空比={ls_ratio_str}")
            
            if sig_dir == "neutral":
                continue
                
            if not skip_filters:
                # 候選訊號成立後才進行 AI 放行判斷（先技術，再 AI，再後續驗證）
                if sym != "BTCUSDT" and ai_sentiment_enabled:
                    if sentiment == "BULLISH" and sig_dir == "short":
                        log.info(f"  [AI Gate] {sym} {sig_dir.upper()} 候選訊號被 AI 宏觀情緒 ({sentiment}) 阻斷。")
                        funnel["ai_blocked"] += 1
                        continue
                    if sentiment == "BEARISH" and sig_dir == "long":
                        log.info(f"  [AI Gate] {sym} {sig_dir.upper()} 候選訊號被 AI 宏觀情緒 ({sentiment}) 阻斷。")
                        funnel["ai_blocked"] += 1
                        continue

                # 絕對多空比過濾 (優化大幣專用多空人數比閥值)
                if not ls_data:
                    log.info(f"  [AI Sentiment] {sym} 無多空比，跳過")
                    continue
                    
                ls_ok = False
                if sig_dir == "long" and current_ratio < 1.85:
                    ls_ok = True
                elif sig_dir == "short" and current_ratio > 1.15:
                    ls_ok = True
                    
                if not ls_ok:
                    log.info(f"  [AI Sentiment] {sym} {sig_dir.upper()} 被大幣多空比過濾阻斷 (當前: {current_ratio:.4f})")
                    funnel["ls_blocked"] += 1
                    continue
                        
                # ── 🧬 新增：做空專用技術面守門人過濾，防止空在地板 ──
                if sig_dir == "short":
                    # 1. RSI 極值超賣過濾
                    try:
                        closes_1h = [float(x[4]) for x in klines_1h]
                        rsi_vals = calc_rsi(closes_1h, 14)
                        if rsi_vals and rsi_vals[-1] is not None:
                            last_rsi = rsi_vals[-1]
                            if last_rsi < 35:
                                log.info(f"  🛑 [AI Sentiment] {sym} 做空被 RSI={last_rsi:.1f} 極度超賣阻斷 (防止空在底部地板)")
                                funnel["short_guard_blocked"] += 1
                                continue
                    except Exception as rsi_err:
                        log.error(f"  [AI Sentiment] RSI 過濾出錯: {rsi_err}")

                    # 2. BTC 1H 短期動能反彈過濾
                    if not check_btc_1h_momentum_aligned("short"):
                        log.info(f"  🛑 [AI Sentiment] {sym} 做空被 BTC 1H 短期動能反彈阻斷。")
                        funnel["short_guard_blocked"] += 1
                        continue

                    # 3. BTC 大戶主力資金 (Smart Money) 做空比過濾
                    if not check_btc_smart_money_aligned("short"):
                        log.info(f"  🛑 [AI Sentiment] {sym} 做空被 BTC 大戶主力資金看多阻斷。")
                        funnel["short_guard_blocked"] += 1
                        continue
                
            # 同方向重複進場優化：允許分批，但限制同方向最大筆數 + 冷卻時間
            same_dir_trades = [
                t for t in active_trades.values()
                if t.get("symbol", "").upper() == sym.upper() and t.get("direction", "").lower() == sig_dir.lower()
            ]
            if len(same_dir_trades) >= max_same_direction:
                log.info(f"  🧊 [AI Re-entry Guard] {sym} {sig_dir.upper()} 已達同方向上限 ({len(same_dir_trades)}/{max_same_direction})，略過本次訊號。")
                funnel["reentry_blocked"] += 1
                continue

            if reentry_cooldown_minutes > 0 and same_dir_trades:
                latest_ts = None
                for t in same_dir_trades:
                    ts_raw = t.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                        if latest_ts is None or ts > latest_ts:
                            latest_ts = ts
                    except Exception:
                        continue
                if latest_ts is not None:
                    cooldown = timedelta(minutes=reentry_cooldown_minutes)
                    if datetime.now(timezone.utc) - latest_ts < cooldown:
                        remain = int((cooldown - (datetime.now(timezone.utc) - latest_ts)).total_seconds() // 60) + 1
                        log.info(f"  🧊 [AI Re-entry Guard] {sym} {sig_dir.upper()} 冷卻中，剩餘約 {max(0, remain)} 分鐘，略過本次訊號。")
                        funnel["reentry_blocked"] += 1
                        continue
                
            # 訊號成立
            signals_sent += 1
            funnel["final_signals"] += 1
            log.info(f"  🚨 [AI Sentiment Signal] {sym} {sig_dir.upper()} 訊號成立！價格: {price}, 止損: {sl}, 止盈: {tps}")
            
            # 插入資料庫
            trade_logic_name = f"AI({sentiment}) + BOS 共振" if not flipped else flip_reason
            db_id = db_manager.insert_historical_trade(
                symbol=sym, direction=sig_dir, entry=price, sl=sl, tps=tps,
                leverage=int(state.get("leverage", "10")),
                logic=trade_logic_name,
                trade_type="AI_SENTIMENT_SIGNAL",
                pionex_order_id=None,
                reach="PENDING",
                timeline=[{"time": "00:00", "event": f"AI 情緒訊號發出(反轉={flipped})，原因: {trade_logic_name}"}],
                tf="1h",
                note=f"AI輿情: {state.get('reason', '')}"
            )
            
            # 儲存到活躍持倉
            trade_key = f"{sym}_{sig_dir}_{int(time.time())}"
            active_trades[trade_key] = {
                "key": trade_key,
                "db_id": db_id,
                "symbol": sym,
                "direction": sig_dir,
                "entry": price,
                "sl": sl,
                "tps": tps,
                "logic": trade_logic_name,
                "reached_tp": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "entry_seq": len(same_dir_trades) + 1
            }
            save_active_trades_ai_sentiment(active_trades)
            
            # 自動開單
            auto_enabled = state.get("auto_enabled", "false") == "true"
            if auto_enabled:
                active_pos_count = len(active_trades)
                max_concurrent = int(state.get("max_concurrent", "3"))
                
                if active_pos_count > max_concurrent:
                    log.info(f"  [AI Sentiment Auto] 已達到最大持倉數限制 ({active_pos_count} >= {max_concurrent})，不自動開單。")
                    continue
                    
                # ── 安全檢查：同幣種同方向已有 OPEN 持倉，拒絕開單（防止干擾手動倉）──
                ai_bot_side = "BUY" if sig_dir.lower() == "long" else "SELL"
                with db_manager.get_db() as conn:
                    ai_conflict_row = conn.execute(
                        "SELECT id FROM active_positions WHERE symbol = ? AND side = ? AND status = 'OPEN'",
                        (sym.upper(), ai_bot_side)
                    ).fetchone()
                if ai_conflict_row:
                    log.info(f"  🚧 [AI Sentiment Auto] {sym} {sig_dir.upper()} 已有 OPEN 持倉 (ID: {ai_conflict_row['id']})，跳過自動開單，防止干擾手動倉。")
                    continue
                    
                margin = float(state.get("margin", "20.0"))
                leverage = int(state.get("leverage", "10"))
                
                # ── 進攻加碼 (Smart Money Position Booster for AI Sentiment, 需 AI 高信心 + 用戶啟用 Booster 設定) ──
                # 注意：門檻嚴格化，做多須多空比 >= 1.50，做空須 <= 0.80
                is_booster_active = 0
                ai_high_confidence = state.get("high_confidence", "false") == "true"
                booster_enabled = db_manager.get_setting("auto_trade_booster_enabled", "false") == "true"
                
                if not booster_enabled:
                    log.info(f"  [AI Sentiment Booster] Booster 未啟用（設定關閉），維持標準保證金。")
                elif ai_high_confidence:
                    try:
                        res_ratio = db_manager.send_binance_request("GET", "/futures/data/topLongShortPositionRatio", {"symbol": "BTCUSDT", "period": "1h", "limit": 1})
                        if res_ratio.get("success") and res_ratio.get("data"):
                            top_ratio = float(res_ratio["data"][0].get("longShortRatio", 1.20))
                            dir_lower = sig_dir.lower()
                            log.info(f"  [AI Sentiment Booster] 當前 BTC 大戶多空比: {top_ratio:.3f}")
                            # 門檻嚴格化：做多須 >= 1.50（極度偏多），做空須 <= 0.80（極度偏空）
                            if dir_lower == "long" and top_ratio >= 1.50:
                                is_booster_active = 1
                            elif dir_lower == "short" and top_ratio <= 0.80:
                                is_booster_active = 1
                            else:
                                log.info(f"  [AI Sentiment Booster] 多空比 {top_ratio:.3f} 未達重倉門檻，維持標準保證金。")
                    except Exception as booster_err:
                        log.warning(f"  [AI Sentiment Booster] 判定加碼失敗: {booster_err}")
                else:
                    log.info(f"  [AI Sentiment Booster] AI 評估當前訊號非極高信心 (high_confidence=false)，維持標準保證金。")
                    
                if is_booster_active == 1:
                    margin = round(margin * 2.5, 2)
                    log.info(f"  🔥 [AI Sentiment Booster] 經 AI 授權高信心且主力機構共振！AI 策略保證金放大 2.5 倍為: {margin} USDT")
                
                log.info(f"  [AI Sentiment Auto] 正在自動為 {sym} 開倉 (Margin: {margin} USDT, Booster: {is_booster_active})...")
                auto_res = db_manager.place_binance_futures_order(
                    symbol=sym, direction=sig_dir, leverage=leverage, margin=margin,
                    sl_price=sl, tps=tps, is_market=True, is_booster=is_booster_active
                )
                if auto_res.get("success"):
                    log.info(f"  [AI Sentiment Auto] 開倉成功！訂單 ID: {auto_res.get('order_id')}")
                    booster_txt = "\n🔥 <b>[AI 主力共振重倉加碼 2.5x 啟用]</b>" if is_booster_active == 1 else ""
                    send_telegram(f"🤖 <b>[AI 情緒策略 自動開單成功] {sym}</b>{booster_txt}\n方向: {sig_dir.upper()}\n策略邏輯: {trade_logic_name}\n保證金: {margin} USDT\n槓桿: {leverage}x\n輿情背景: {state.get('reason')}")
                else:
                    log.error(f"  [AI Sentiment Auto] 開倉失敗: {auto_res.get('error')}")
                    
        except Exception as err:
            log.error(f"  [AI Sentiment] 掃描 {sym} 發生錯誤: {err}")
            
    AI_SENTIMENT_FUNNEL.clear()
    AI_SENTIMENT_FUNNEL.update(funnel)
    log.info(f"  [AI Sentiment] 掃描完成。發送訊號數: {signals_sent}，活躍持倉數: {len(active_trades)}")

def update_active_trades_state(active_trades: dict, current_prices: dict, klines_dict: dict = None) -> dict:
    # ── 新增：主動與 SQLite 資料庫同步實體持倉狀態，解決本地 json 不同步的 Bug ──
    try:
        import sqlite3
        conn = sqlite3.connect("trading_system.db")
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, side FROM active_positions WHERE status IN ('OPEN', 'PENDING_ORDER')")
        db_positions = cursor.fetchall()
        conn.close()
        
        # 轉換為帶方向的對照組，如 {"BTCUSDT_short", "ETHUSDT_long"}
        active_db_keys = set()
        for sym, side in db_positions:
            direction = "long" if side.upper() == "BUY" else "short"
            active_db_keys.add(f"{sym.upper()}_{direction}")
    except Exception as e:
        log.error(f"  ❌ 同步 SQLite 實體持倉狀態失敗: {e}")
        active_db_keys = None

    updated = {}
    for key, trade in active_trades.items():
        trade_identity = f"{trade.get('symbol', '').upper()}_{trade.get('direction', '').lower()}"
        # 如果資料庫已無此實體持倉，說明已被 position_monitor 清除或平倉，主動停止監控
        # 只對「已綁定實體持倉」的條目做同步清理；純信號監控不可因此被誤刪，否則會反覆重複推播
        is_position_bound = bool(trade.get("bind_to_position", False)) or bool(trade.get("pionex_order_id"))
        if active_db_keys is not None and is_position_bound and trade_identity not in active_db_keys:
            log.info(f"  🔄 [Sync] 偵測到 {key} 已無實體持倉，自動結束本地信號監控。")
            continue

        sym = trade["symbol"]
        if sym not in current_prices:
            updated[key] = trade
            continue
            
        price = current_prices[sym]
        direction = trade["direction"]
        sl = trade["sl"]
        tps = trade["tps"]
        reached_tp = trade.get("reached_tp", 0)
        
        # 僅使用最新即時價格進行檢測，避免使用包含交易開單前歷史數據的 4H K線高低點
        sl_hit = False
        if direction == "long" and price <= sl:
            sl_hit = True
        elif direction == "short" and price >= sl:
            sl_hit = True
            
        if sl_hit:
            log.info(f"  🛑 {sym} 持倉觸及最新即時價格止損點 {sl:.4g}，平倉出局。")
            COOLDOWN_UNTIL[sym] = time.time() + 12 * 3600 # 12 小時止損冷卻
            continue
            
        new_reached_tp = reached_tp
        for idx, tp in enumerate(tps):
            if direction == "long" and price >= tp:
                new_reached_tp = max(new_reached_tp, idx + 1)
            elif direction == "short" and price <= tp:
                new_reached_tp = max(new_reached_tp, idx + 1)
                
        if new_reached_tp > reached_tp:
            log.info(f"  🎯 {sym} 觸及目標 TP{new_reached_tp}!")
            trade["reached_tp"] = new_reached_tp
            if new_reached_tp == 1:
                # 與 position_monitor 的 SIGNAL / AI_SENTIMENT_SIGNAL 監控規則保持一致：TP1 後移到保本價
                trade["sl"] = trade["entry"]
            elif new_reached_tp >= 2:
                trade["sl"] = tps[new_reached_tp - 2] # 追蹤止損
                
        # 24 小時時效撤單
        trigger_time = datetime.fromisoformat(trade["timestamp"].replace("Z", "+00:00"))
        time_elapsed = datetime.now(timezone.utc) - trigger_time
        if time_elapsed > timedelta(hours=24) and reached_tp == 0:
            log.info(f"  ⌛ {sym} 信號開單超時 24h 未觸及 TP，失效撤單。")
            continue
            
        # 48 小時無條件安全撤單，防止老信號一直殘留在 active_trades 佔用額度
        if time_elapsed > timedelta(hours=48):
            log.info(f"  ⌛ {sym} 信號監控已達 48 小時安全上限，自動移出監控佇列。")
            continue
            
        updated[key] = trade
    return updated

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 歷史 K 線生成器 (用於展示 14 筆成功案例，精美逼真)
# ═══════════════════════════════════════════════════════════════════════════════
def generate_mock_klines(entry, sl, tps, direction, reach, timestamp_str) -> list:
    # 產生成交前後 80 根 K 線，精確擬合進場、止損與停利軌跡
    dt = parse_sim_time(timestamp_str)
    base_ts = int(dt.timestamp() * 1000)
    interval_ms = 4 * 3600 * 1000 # 4h
    
    klines = []
    price = entry * (0.97 if direction == "long" else 1.03) # 初始價格
    
    # 1. 進場前的震盪 (第 0~39 根)
    for i in range(40):
        ts = base_ts - (40 - i) * interval_ms
        # 朝向 entry 漸進震盪
        target_p = entry - (entry - price) * 0.9
        open_p = price
        close_p = target_p + (entry * 0.003 * (1 if i % 2 == 0 else -1))
        high_p = max(open_p, close_p) + (entry * 0.002)
        low_p = min(open_p, close_p) - (entry * 0.002)
        
        # 確保進場前不破止損
        if direction == "long":
            low_p = max(low_p, sl * 1.005)
        else:
            high_p = min(high_p, sl * 0.995)
            
        klines.append([ts, open_p, high_p, low_p, close_p, 10000])
        price = close_p
        
    # 第 40 根 K 線：正好處於 Entry 的關鍵反轉 K 線
    ts = base_ts
    open_p = price
    close_p = entry
    high_p = max(open_p, close_p) + (entry * 0.005)
    low_p = min(open_p, close_p) - (entry * 0.005)
    if direction == "long":
        low_p = max(low_p, sl * 1.002)
    else:
        high_p = min(high_p, sl * 0.998)
    klines.append([ts, open_p, high_p, low_p, close_p, 20000])
    price = close_p
    
    # 2. 進場後的行情走勢 (第 41~79 根)：朝停利方向推進
    max_tp_level = int(reach.replace("TP", "")) if "TP" in reach else 1
    target_tp_price = tps[max_tp_level - 1]
    
    for i in range(1, 40):
        ts = base_ts + i * interval_ms
        # 漸進到達目標停利價
        progress = min(1.0, i / 20.0) # 前 20 根 K 線走到極限
        current_target = entry + (target_tp_price - entry) * progress
        
        open_p = price
        close_p = current_target + (entry * 0.004 * (1 if i % 3 == 0 else -1))
        
        if direction == "long":
            high_p = max(open_p, close_p) + (entry * 0.003)
            low_p = min(open_p, close_p) - (entry * 0.002)
            # 觸碰停利
            if i >= 20:
                high_p = max(high_p, target_tp_price)
        else:
            high_p = max(open_p, close_p) + (entry * 0.002)
            low_p = min(open_p, close_p) - (entry * 0.003)
            # 觸碰停利
            if i >= 20:
                low_p = min(low_p, target_tp_price)
                
        # 確保後期不打穿止損
        if direction == "long":
            low_p = max(low_p, sl * 1.005)
        else:
            high_p = min(high_p, sl * 0.995)
            
        klines.append([ts, open_p, high_p, low_p, close_p, 15000])
        price = close_p
        
    return klines

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 多線程極簡高階 Web 伺服器
# ═══════════════════════════════════════════════════════════════════════════════
# Load DASHBOARD_HTML dynamically from templates/dashboard.html

# ── 歷史回測交易模擬核心 ──
def run_simulation_backtest(signal, klines, allow_expiry=True, expiry_hours=3.5, margin=50.0, leverage=20.0):
    symbol = signal['symbol']
    direction = signal['direction'].upper()
    entry_price = signal['entry']
    sl = signal['sl']
    tps = [t for t in signal['tps'] if t is not None and t > 0]
    
    tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
    dt = parse_sim_time(signal['time_str'])
    dt_aware = dt.replace(tzinfo=tz_taipei)
    start_ts = int(dt_aware.timestamp() * 1000)
    
    if allow_expiry and expiry_hours is not None:
        expiry_ts = start_ts + int(expiry_hours * 3600 * 1000)
    else:
        expiry_ts = float('inf')
        
    reached_tp = 0
    hit_results = [False] * len(tps)
    sl_hit = False
    expired = False
    extreme_price = float(klines[0][1]) if klines else entry_price
    
    current_sl = sl
    exit_price = None
    exit_reason = "進行中"
    exit_time_str = "N/A"
    
    notional = margin * leverage
    qty = notional / entry_price
    
    last_close = entry_price
    last_time = start_ts
    
    timeline = []
    timeline.append({"time": "00:00", "event": f"開單引導建倉，進場參考價 {entry_price:.4f}"})
    
    for k in klines:
        timestamp = int(k[0])
        open_p, high, low, close = map(float, k[1:5])
        last_close = close
        last_time = timestamp
        
        if direction == 'LONG':
            if high > extreme_price: extreme_price = high
        else:
            if low < extreme_price or extreme_price == 0: extreme_price = low
            
        # Calculate elapsed time string
        total_seconds = int((timestamp - start_ts) / 1000)
        if total_seconds < 0:
            total_seconds = 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        elapsed_str = f"{hours:02d}:{minutes:02d}"
            
        # Check Expiration (with optimized logic: bypass if reached_tp >= 1 or currently in profit)
        if timestamp > expiry_ts:
            is_k_in_profit = (close > entry_price) if direction == 'LONG' else (close < entry_price)
            if reached_tp >= 1:
                # 已達標 TP1 以上，不執行逾期結算，繼續運行
                pass
            elif is_k_in_profit:
                # 當前處於獲利狀態，不執行逾期結算，繼續運行
                pass
            else:
                expired = True
                exit_reason = "失效撤單 (Expired)"
                exit_price = None
                ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                timeline.append({"time": elapsed_str, "event": "信號已逾期 24 小時未達標，失效結算"})
                break
            
        # Check Stop Loss (using current_sl which might have trailed)
        if direction == 'LONG':
            if low <= current_sl:
                sl_hit = True
                exit_price = current_sl
                ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                if reached_tp == 0:
                    exit_reason = "停損 (SL Hit)"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰止損 {current_sl:.4f}，平倉離場"})
                elif reached_tp == 1:
                    exit_reason = "保本平倉 (TP1後回撤)"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰保本價 {current_sl:.4f}，保本平倉"})
                else:
                    exit_reason = f"TP{reached_tp} 後追蹤停損 (TP{reached_tp-1})"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰移動止損 {current_sl:.4f}，追蹤平倉"})
                break
        else: # SHORT
            if high >= current_sl:
                sl_hit = True
                exit_price = current_sl
                ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                if reached_tp == 0:
                    exit_reason = "停損 (SL Hit)"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰止損 {current_sl:.4f}，平倉離場"})
                elif reached_tp == 1:
                    exit_reason = "保本平倉 (TP1後回撤)"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰保本價 {current_sl:.4f}，保本平倉"})
                else:
                    exit_reason = f"TP{reached_tp} 後追蹤停損 (TP{reached_tp-1})"
                    timeline.append({"time": elapsed_str, "event": f"價格觸碰移動止損 {current_sl:.4f}，追蹤平倉"})
                break
                
        # Check Take Profits sequentially
        is_break_final_tp = False
        new_tp_hit_this_kline = False
        for i in range(len(tps)):
            tp = tps[i]
            is_new_tp = False
            if direction == 'LONG':
                if high >= tp and not hit_results[i]:
                    is_new_tp = True
            else: # SHORT
                if low <= tp and not hit_results[i]:
                    is_new_tp = True
                    
            if is_new_tp:
                hit_results[i] = True
                reached_tp = max(reached_tp, i + 1)
                new_tp_hit_this_kline = True
                
                # Update Trailing SL
                if reached_tp == 1:
                    current_sl = entry_price
                elif reached_tp == 2:
                    current_sl = tps[0]
                elif reached_tp == 3:
                    current_sl = tps[1]
                elif reached_tp == 4:
                    current_sl = tps[2]
                
                timeline.append({"time": elapsed_str, "event": f"價格達標 TP{reached_tp} ({tp:.4f})，移動止損調整至 {current_sl:.4f}"})
                
                # If we reach the final TP, close position
                if reached_tp == len(tps):
                    is_break_final_tp = True
                    exit_price = tp
                    ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                    exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                    exit_reason = f"全盈達標 (TP{reached_tp}!)"
                    break
        
        if is_break_final_tp:
            break
            
        # 💡 動態止損收緊優化
        if not new_tp_hit_this_kline and reached_tp >= 1 and reached_tp < len(tps):
            next_tp = tps[reached_tp]
            if next_tp and next_tp > 0:
                base_price = entry_price if reached_tp == 1 else tps[reached_tp - 2]
                tighter_sl = current_sl
                
                denom = (next_tp - base_price) if direction == 'LONG' else (base_price - next_tp)
                if denom > 0:
                    # 1. Progressive Trailing (Progress >= 70%)
                    progress = (close - base_price) / denom if direction == 'LONG' else (base_price - close) / denom
                    if 0.70 <= progress < 1.0:
                        tighter_sl = base_price + 0.50 * (next_tp - base_price) if direction == 'LONG' else base_price - 0.50 * (base_price - next_tp)
                
                # 2. Stagnation-based Trailing (held > 12h)
                elapsed_hours = (timestamp - start_ts) / (3600 * 1000)
                if elapsed_hours > 12.0:
                    time_sl = base_price + 0.25 * (next_tp - base_price) if direction == 'LONG' else base_price - 0.25 * (base_price - next_tp)
                    if direction == 'LONG' and time_sl > tighter_sl:
                        tighter_sl = time_sl
                    elif direction == 'SHORT' and time_sl < tighter_sl:
                        tighter_sl = time_sl
                        
                if (direction == 'LONG' and tighter_sl > current_sl) or (direction == 'SHORT' and tighter_sl < current_sl):
                    current_sl = tighter_sl
            
    pnl = 0.0
    is_open = False
    
    if exit_reason == "進行中":
        is_open = True
        exit_price = last_close
        ts_utc = datetime.fromtimestamp(last_time / 1000, tz=timezone.utc)
        exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
        
        total_seconds = int((last_time - start_ts) / 1000)
        if total_seconds < 0:
            total_seconds = 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        elapsed_str = f"{hours:02d}:{minutes:02d}"
        timeline.append({"time": elapsed_str, "event": f"進行中，當前最新價格 {last_close:.4f}"})
        
        if direction == 'LONG':
            pnl = qty * (exit_price - entry_price)
        else:
            pnl = qty * (entry_price - exit_price)
    elif exit_reason == "失效撤單 (Expired)":
        pnl = 0.0
    else:
        if direction == 'LONG':
            pnl = qty * (exit_price - entry_price)
        else:
            pnl = qty * (entry_price - exit_price)
            
    fee = 0.0
    if exit_reason != "失效撤單 (Expired)":
        exit_val = qty * exit_price if exit_price else 0.0
        fee = (notional * 0.0005) + (exit_val * 0.0005)
        
    pnl_net = pnl - fee if exit_reason != "失效撤單 (Expired)" else 0.0
    
    return {
        'status': exit_reason,
        'exit_price': exit_price,
        'reached_tp': reached_tp,
        'pnl_raw': pnl,
        'pnl_net': pnl_net,
        'fee': fee,
        'is_open': is_open,
        'hit_results': hit_results,
        'extreme': extreme_price,
        'final_sl': current_sl,
        'exit_time': exit_time_str,
        'timeline': timeline
    }


# ── 導入 Dashboard 網頁伺服器服務模組 ──
from server import WebServerThread
# ── 導入持倉狀態監控與校正服務模組 ──
from monitor import PositionMonitorThread

# ═══════════════════════════════════════════════════════════════════════════════
#  ▶ 主程式
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║   🧬 Antigravity 智能合約交易掃描與量化儀表板     ║")
    log.info("╚══════════════════════════════════════════════════╝")
    log.info(f"  監控幣種: {', '.join(COINS)}")
    log.info(f"  掃描週期: {PRIMARY_TF}  →  高週期佐證: {get_higher_tf(PRIMARY_TF)}")
    log.info(f"  通知門檻: 滿足指標(✅) >= {MIN_CONDITIONS} 且分數 >= {MIN_SCORE}")
    log.info(f"  掃描間隔: {SCAN_INTERVAL} 秒（{SCAN_INTERVAL // 60} 分鐘）")
    log.info(f"  防重複開單與時效保險已就緒。")
    log.info("")

    # 初始化資料庫
    db_manager.init_db()

    # 初始化 BTC 大盤過濾數據
    log.info("  [BTC Filter] 正在初始化 BTC 大盤聯動過濾數據...")
    try:
        btc_res = analyze_btc_macro()
        if btc_res and btc_res.get("price", 0) > 0:
            BTC_MACRO_STATE.clear()
            BTC_MACRO_STATE.update(btc_res)
            log.info(f"  [BTC Filter] BTC 大盤數據初始化完成。價格: {btc_res['price']:.1f}, 狀態: {btc_res['state']}")
    except Exception as e:
        log.error(f"  [BTC Filter] 初始化失敗: {e}")

    # 啟動 Web 服務線程
    web_thread = WebServerThread("127.0.0.1", WEB_PORT)
    web_thread.start()

    # 啟動 幣安託管部位監控線程
    monitor_thread = PositionMonitorThread()
    monitor_thread.start()

    log.info("  按 Ctrl+C 停止")
    log.info("")

    # 執行首次掃描
    try:
        scan_once()
        # scan_once_felisa_ls() (Disabled by user request)
        scan_once_ai_sentiment()
    except Exception as e:
        log.error(f"初次掃描發生錯誤: {e}")

    tz = timezone(timedelta(hours=TZ_OFFSET))
    while True:
        try:
            # 等待到下一輪
            current_scan_interval = int(db_manager.get_setting("scan_interval", "300"))
            next_dt = datetime.now(tz) + timedelta(seconds=current_scan_interval)
            log.info(f"  下次定期掃描時間: {next_dt.strftime('%H:%M:%S')}")
            time.sleep(current_scan_interval)
            scan_once()
            # scan_once_felisa_ls() (Disabled by user request)
            scan_once_ai_sentiment()
        except KeyboardInterrupt:
            log.info("使用者中斷，自動終止程序。")
            break
        except Exception as e:
            log.error(f"掃描主循環異常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
