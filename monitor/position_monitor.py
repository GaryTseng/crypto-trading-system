# -*- coding: utf-8 -*-
"""
📈 Antigravity Position Monitor Thread
"""
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
import db_manager
import json
from config import log, parse_sim_time, TZ_OFFSET, send_telegram, load_active_trades, save_active_trades, BTC_MACRO_STATE, remove_active_trade_by_symbol_and_direction
from monitor.calibration import calibrate_pending_signals, sync_all_positions_with_binance_on_startup

def verify_expiry_with_ai(pos, last_price):
    from analyzers.ai_sentiment import get_latest_crypto_news, get_jin10_news, get_binance_futures_data_text
    symbol = pos.get("symbol")
    side = pos.get("side", "BUY")
    direction = "LONG" if side == "BUY" else "SHORT"
    entry_price = float(pos.get("entry_price", 0.0))
    sl = float(pos.get("sl", 0.0))
    tps_val = pos.get("tps")
    tps = []
    if tps_val:
        try:
            tps = json.loads(tps_val) if isinstance(tps_val, str) else tps_val
        except Exception:
            tps = [tps_val]
            
    # 1. Fetch latest news
    coindesk_news = get_latest_crypto_news()
    jin10_news = get_jin10_news()
    
    # 2. Fetch Binance Futures Trading Data for this symbol
    binance_data = get_binance_futures_data_text(symbol)
    
    # 3. Fetch K-lines (last 24 hours of 1H K-lines)
    klines_text = ""
    try:
        r = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval=1h&limit=24", timeout=5)
        if r.ok:
            kl = r.json()
            klines_text = "\n".join([f"Time: {datetime.fromtimestamp(x[0]/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')}, O: {x[1]}, H: {x[2]}, L: {x[3]}, C: {x[4]}, Vol: {float(x[5]):,.1f}" for x in kl])
    except Exception as e:
        klines_text = f"Error fetching klines: {e}"
        
    prompt = (
        "你是一個資深的加密貨幣期貨交易風控專家，目前正在評估是否要對一個已逾期的持倉執行『強制平倉割肉』或是『延長持單時間』。\n\n"
        "【當前持倉資訊】\n"
        f"- 交易對: {symbol}\n"
        f"- 方向: {direction}\n"
        f"- 入場價: {entry_price}\n"
        f"- 當前價: {last_price}\n"
        f"- 止損價: {sl}\n"
        f"- 止盈目標: {tps}\n\n"
        "【最新市場新聞輿情】\n"
        f"{coindesk_news}\n\n"
        f"{jin10_news}\n\n"
        "【幣安期貨大戶多空與持倉量數據】\n"
        f"{binance_data}\n\n"
        "【近 24 小時的 1H K線走勢】\n"
        f"{klines_text}\n\n"
        "請根據上述所有宏觀新聞、期貨持倉數據（Open Interest）、多空人數比（L/S Ratio）、以及 K 線趨勢，客觀評估：\n"
        f"目前市場動能與大局，是否仍然支持我們的 {direction} 持倉？\n"
        "1. 如果新聞利好/利空方向與持倉一致、或者 K 線有築底反彈/突破跡象、或者幣安大戶正大量加倉（OI上升且L/S比支持），認為還有機會獲利，請做成『HOLD』（延長持單時間，不割肉）。\n"
        "2. 如果趨勢已經嚴重逆轉、新聞出現重大反向利空、幣安大戶瘋狂反向建倉或爆倉，認為該倉位已無希望，繼續持有只會擴大虧損，請做成『CUT』（市價平倉割肉）。\n\n"
        "請嚴格以 JSON 格式回傳，格式如下：\n"
        "{\n"
        '  "decision": "HOLD" | "CUT",\n'
        '  "reason": "你的簡短繁體中文分析原因摘要（50字以內，結合新聞、K線與幣安多空數據）"\n'
        "}\n"
        "注意：請只回傳 JSON 字串，不要回傳額外的 ```json ``` 標記或任何其他說明文字。"
    )
    
    try:
        import subprocess
        res = subprocess.run(
            ["agy", "--model", "gemini-3.6-flash", "--effort", "low", "--print", prompt, "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60
        )
        if res.returncode == 0:
            clean_json = res.stdout.strip()
            if "```" in clean_json:
                clean_json = clean_json.split("```")[1]
                if clean_json.startswith("json"):
                    clean_json = clean_json[4:]
            parsed = json.loads(clean_json.strip())
            decision = parsed.get("decision", "CUT").upper()
            reason = parsed.get("reason", "AI分析判定")
            return decision, reason
        else:
            return "CUT", f"agy 執行錯誤: {res.stderr}"
    except Exception as e:
        return "CUT", f"AI分析異常: {e}"

class PositionMonitorThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.warned_positions = set()

    def run(self):
        log.info("  🚀 [Position Monitor] 託管持倉監控線程啟動。")
        # 啟動時先執行一次自動校正 PENDING 信號
        calibrate_pending_signals()
        
        # 啟動時執行一次完整的實體持倉與止損單校正同步
        try:
            sync_all_positions_with_binance_on_startup()
        except Exception as e:
            log.error(f"  [Position Monitor] 啟動校正幣安持倉失敗: {e}")
            
        last_calib_time = time.time()
        while True:
            try:
                active_pos_res = db_manager.get_active_positions()
                positions = []
                if active_pos_res.get("success"):
                    positions = [p for p in active_pos_res.get("data", []) if p["status"] in ["OPEN", "PENDING_ORDER"]]


                for pos in positions:
                    symbol = pos["symbol"]
                    pos_id = pos["id"]
                    side = pos["side"] # 'BUY' (Long) or 'SELL' (Short)
                    entry_price = pos["entry_price"]
                    sl = pos["sl"]
                    tps = pos["tps"] # [tp1, tp2, tp3, tp4]
                    current_tp_level = pos["current_tp_level"]
                    
                    # 獲取即時價格
                    last_price = None
                    try:
                        r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}", timeout=5)
                        if r.ok:
                            last_price = float(r.json().get("price", 0.0))
                    except Exception as e:
                        log.error(f"監控獲取價格失敗 {symbol}: {e}")
                            
                    if not last_price:
                        continue
                        
                    # 更新當前價格到數據庫
                    with db_manager.get_db() as conn:
                        conn.execute("UPDATE active_positions SET current_price = ? WHERE id = ?", (last_price, pos_id))
                        conn.commit()
                        
                    status = pos.get("status", "OPEN")
                    mock_mode = db_manager.get_setting("binance_mock_mode", "true") == "true"
                    
                    if status == "PENDING_ORDER":
                        # 檢查限價委託單超時失效 (24小時)
                        is_expired = False
                        try:
                            pos_time_str = pos.get("timestamp")
                            if pos_time_str:
                                created_dt = parse_sim_time(pos_time_str)
                                tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                                now_dt = datetime.now(tz_taipei)
                                created_dt = created_dt.replace(tzinfo=tz_taipei) if created_dt.tzinfo is None else created_dt
                                if now_dt - created_dt > timedelta(hours=24):
                                    log.info(f"⏰ 限價委託單 {symbol} (ID: {pos_id}) 已逾期 24 小時，自動撤銷委託...")
                                    cancel_res = db_manager.cancel_binance_limit_order(pos_id, event_txt="委託單已逾期 24 小時，自動撤單離場", reach_status="EXPIRED")
                                    if cancel_res.get("success"):
                                        msg = (
                                            f"⏰ <b>[幣安託管通知] {symbol} 限價委託單已逾期 24 小時，自動撤銷！</b>\n"
                                            f"────────────────────────────\n"
                                            f"方向: {'📈 多 (LONG)' if side == 'BUY' else '📉 空 (SHORT)'}\n"
                                            f"委託時間: {pos_time_str}\n"
                                            f"委託價格: {entry_price:.4f}\n"
                                            f"────────────────────────────\n"
                                            f"狀態: 已自動撤銷委託單，停止監控。"
                                        )
                                        send_telegram(msg)
                                        is_expired = True
                                    else:
                                        log.error(f"  [Position Monitor] 逾期撤單失敗 {symbol} (ID: {pos_id}): {cancel_res.get('error')}")
                        except Exception as exp_err:
                            log.error(f"檢查限價單過期失敗 {symbol} (ID: {pos_id}): {exp_err}")
                            
                        if is_expired:
                            continue
                            
                        # 檢測成交
                        if mock_mode:
                            is_filled = False
                            if side == "BUY" and last_price <= entry_price:
                                is_filled = True
                            elif side == "SELL" and last_price >= entry_price:
                                is_filled = True
                                
                            if is_filled:
                                with db_manager.get_db() as conn:
                                    conn.execute("UPDATE active_positions SET status = 'OPEN' WHERE id = ?", (pos_id,))
                                    conn.commit()
                                    
                                order_id = pos.get("pionex_order_id")
                                if order_id:
                                    db_manager.update_historical_trade_status(pionex_order_id=order_id, reach="OPEN")
                                    db_manager.append_historical_trade_event(order_id, "【模擬模式】限價單價格觸發，模擬成交。")
                                    
                                msg = (
                                    f"📈 <b>[幣安模擬限價單成交] {symbol} 已成功建倉！</b>\n"
                                    f"────────────────────────────\n"
                                    f"方向: {'📈 多 (LONG)' if side == 'BUY' else '📉 空 (SHORT)'}\n"
                                    f"成交價格: {entry_price:.4f}\n"
                                    f"數量: {pos['size']:.4f}\n"
                                    f"────────────────────────────\n"
                                    f"狀態: 已自動轉為進行中，開始追蹤持倉與移動止損。"
                                )
                                send_telegram(msg)
                                log.info(f"🎉 [Position Monitor] 模擬限價單 {symbol} (ID: {pos_id}) 觸發成交。")
                            continue
                        else:
                            # 實盤成交檢測
                            sync_res = db_manager.sync_position_with_binance(pos_id)
                            if sync_res.get("status") == "OPEN":
                                log.info(f"🎉 [Position Monitor] 限價單 {symbol} (ID: {pos_id}) 已成交，轉換為 OPEN 持倉。")
                            elif sync_res.get("status") == "CLOSED":
                                log.info(f"❌ [Position Monitor] 限價單 {symbol} (ID: {pos_id}) 在交易所被撤銷或已失效。")
                            continue
                            
                    # 實盤下，定期與幣安持倉同步以防漏檢 (status == "OPEN")
                    if not mock_mode:
                        try:
                            # 查詢幣安實體持倉
                            pos_risk_res = db_manager.send_binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()})
                            if pos_risk_res["success"]:
                                matched_risk = None
                                is_long = (side == "BUY")
                                for pr in pos_risk_res["data"]:
                                    amt = float(pr.get("positionAmt", 0.0))
                                    if (is_long and amt > 0) or (not is_long and amt < 0):
                                        matched_risk = pr
                                        break
                                        
                                if matched_risk is None or float(matched_risk.get("positionAmt", 0.0)) == 0.0:
                                    # 幣安上已無部位，先檢查限價開倉單是否還在掛單中
                                    is_limit_order_pending = False
                                    order_id = pos.get("pionex_order_id")
                                    if order_id and not str(order_id).startswith("MOCK_"):
                                        try:
                                            order_status_res = db_manager.send_binance_request("GET", "/fapi/v1/order", {"symbol": symbol.upper(), "orderId": int(order_id)})
                                            if order_status_res["success"]:
                                                order_state = order_status_res["data"].get("status")
                                                if order_state in ["NEW", "PARTIALLY_FILLED"]:
                                                    is_limit_order_pending = True
                                                    log.info(f"⏳ [Position Monitor] 限價開倉單 {symbol} (ID: {pos_id}, OrderID: {order_id}) 仍在掛單中/部分成交 (狀態: {order_state})，繼續監視中...")
                                        except Exception as order_err:
                                            log.error(f"查詢限價單狀態失敗 {symbol} (OrderID: {order_id}): {order_err}")
                                            
                                    if is_limit_order_pending:
                                        # 限價單尚未成交，跳過本次的止損/止盈判定
                                        continue
                                        
                                    # 真正已無持倉且無掛單，進行本地同步
                                    log.info(f"🔍 [Position Monitor] 檢測到幣安實體持倉 {symbol} (ID: {pos_id}) 已平倉，自動同步本地狀態...")
                                    
                                    # 判斷是否為止損（藉由最新價格是否接近止損價）
                                    hit_sl_real = False
                                    if last_price:
                                        if is_long and last_price <= sl * 1.005:
                                            hit_sl_real = True
                                        elif not is_long and last_price >= sl * 0.995:
                                            hit_sl_real = True
                                            
                                    reach_status = "SL" if hit_sl_real else "CLOSED"
                                    status_db = "SL_HIT" if hit_sl_real else "CLOSED"
                                    
                                    pnl = 0.0
                                    if last_price:
                                        pnl = ((last_price - entry_price) / entry_price * 100) if is_long else ((entry_price - last_price) / entry_price * 100)
                                    pnl_with_lev = pnl * pos["leverage"]
                                    
                                    with db_manager.get_db() as conn:
                                        conn.execute(
                                            "UPDATE active_positions SET status = ?, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
                                            (status_db, db_manager._now_taipei_str(), pos_id),
                                        )
                                        conn.commit()
                                    remove_active_trade_by_symbol_and_direction(symbol, 'long' if is_long else 'short')
                                        
                                    if pos.get("pionex_order_id"):
                                        acc_pnl = pos.get("accumulated_pnl", 0.0) or 0.0
                                        rem_pnl = float(pos["size"]) * ((last_price - entry_price) if is_long else (entry_price - last_price)) if last_price else 0.0
                                        total_pnl_usdt = round(acc_pnl + rem_pnl, 4)
                                        db_manager.update_historical_trade_status(pionex_order_id=pos["pionex_order_id"], reach=reach_status, pnl=total_pnl_usdt)
                                        db_manager.reconcile_trade_pnl(pos["pionex_order_id"], fallback_pnl=total_pnl_usdt)
                                        db_manager.append_historical_trade_event(
                                            pos["pionex_order_id"],
                                            f"自動狀態同步：幣安持倉已結束，判定為 {'止損離場' if hit_sl_real else '手動平倉或停利'}，盈虧 {pnl_with_lev:+.2f}%"
                                        )
                                        # 💡 同步更新關聯的前置 SIGNAL / AI_SENTIMENT_SIGNAL 狀態
                                        try:
                                            with db_manager.get_db() as conn:
                                                u_row = conn.execute("SELECT id FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pos["pionex_order_id"],)).fetchone()
                                                if u_row:
                                                    db_manager.sync_linked_signal_status_from_user_trade(
                                                        symbol=symbol,
                                                        direction="long" if is_long else "short",
                                                        user_trade_id=u_row["id"],
                                                        reach_status=reach_status,
                                                        event_text=f"持倉結束同步，狀態更變為 {reach_status}"
                                                    )
                                        except Exception as sync_err:
                                            log.warning(f"自動同步關聯信號狀態失敗 {symbol}: {sync_err}")
                                        
                                    msg = (
                                        f"🔄 <b>[幣安狀態同步] {symbol} 持倉已結束！</b>\n"
                                        f"────────────────────────────\n"
                                        f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                                        f"最終原因: {'🛑 觸發實體止損單' if hit_sl_real else '✅ 交易所手動平倉/停利'}\n"
                                        f"開倉價格: {entry_price:.4f}\n"
                                        f"最後價格: {last_price:.4f}\n"
                                        f"單筆盈虧: {pnl_with_lev:+.2f}%\n"
                                        f"────────────────────────────\n"
                                        f"狀態: 已自動同步為已結算，結束監控。"
                                    )
                                    send_telegram(msg)
                                    continue
                                else:
                                    # ── 新增：精確檢測交易所實體 TP1 限價單是否已成交 (防範多筆同向持倉合併干擾) ──
                                    real_amt = abs(float(matched_risk.get("positionAmt", 0.0)))
                                    db_size = float(pos["size"])
                                    tp1_filled_real = False
                                    tp1_order_id = pos.get("tp1_order_id")
                                    
                                    # 1. 優先級一：直接查詢 TP1 委託單狀態 (100% 精確)
                                    if tp1_order_id and not str(tp1_order_id).startswith("MOCK_"):
                                        try:
                                            tp1_status_res = db_manager.send_binance_request("GET", "/fapi/v1/order", {"symbol": symbol.upper(), "orderId": int(tp1_order_id)})
                                            if tp1_status_res["success"]:
                                                tp1_state = tp1_status_res["data"].get("status")
                                                if tp1_state == "FILLED":
                                                    tp1_filled_real = True
                                                    log.info(f"📊 [Position Monitor] 檢測到 {symbol} (ID: {pos_id}) 的 TP1 實體限價單 (ID: {tp1_order_id}) 已成交！")
                                        except Exception as order_err:
                                            log.warning(f"查詢 TP1 限價單狀態失敗 {symbol} (OrderID: {tp1_order_id}): {order_err}")
                                            
                                    # 2. 優先級二：Fallback (舊持倉兼容) ── 如果無訂單 ID，且該幣種只有單筆 OPEN 持倉，檢測數量是否減半
                                    if not tp1_filled_real and not tp1_order_id:
                                        with db_manager.get_db() as conn:
                                            same_coin_pos = conn.execute("SELECT COUNT(*) as cnt FROM active_positions WHERE symbol = ? AND status = 'OPEN'", (symbol.upper(),)).fetchone()
                                            same_coin_count = same_coin_pos["cnt"] if same_coin_pos else 1
                                            
                                        is_boost_val = (pos.get("is_booster") == 1)
                                        ratio_threshold = 0.85 if is_boost_val else 0.60
                                        if same_coin_count == 1 and 0 < real_amt <= db_size * ratio_threshold:
                                            tp1_filled_real = True
                                            log.info(f"📊 [Position Monitor] {symbol} (ID: {pos_id}) 無訂單 ID 且為單筆持倉，交易所數量已減半/減 25%，判定舊限價單成交！")
                                            
                                    # 3. 執行 TP1 狀態同步與移位防禦
                                    if current_tp_level == 0 and tp1_filled_real:
                                        # 計算開倉保本止損
                                        try:
                                            _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                                            new_sl = round(entry_price, price_prec)
                                        except Exception:
                                            new_sl = round(entry_price, 4)
                                            
                                        # 剩餘持倉更新：如果是多筆持倉合併，鎖定單筆持倉減倉後剩餘值；若是單筆，直接同步 real_amt
                                        tp1_ratio = 0.25 if pos.get("is_booster") == 1 else 0.50
                                        new_db_size = db_size * (1 - tp1_ratio)
                                        if real_amt < new_db_size * 1.2:
                                            new_db_size = real_amt
                                            
                                        # 更新資料庫中的 sl, size, current_tp_level
                                        with db_manager.get_db() as conn:
                                            conn.execute(
                                                "UPDATE active_positions SET sl = ?, size = ?, current_tp_level = 1 WHERE id = ?",
                                                (new_sl, new_db_size, pos_id)
                                            )
                                            conn.commit()
                                            
                                        # 同步更新幣安交易所上的實體止損委託單
                                        sl_order_side = "SELL" if is_long else "BUY"
                                        db_manager.update_binance_stop_loss(symbol, sl_order_side, new_sl, position_id=pos_id)
                                        
                                        # 同步更新歷史分析表與發送 Telegram 通知
                                        if pos.get("pionex_order_id"):
                                            db_manager.update_historical_trade_status(pionex_order_id=pos["pionex_order_id"], reach="TP1")
                                            db_manager.append_historical_trade_event(
                                                pos["pionex_order_id"],
                                                f"限價 TP1 ({tps[0]:.4f}) 已自動成交，移動止損調整至開倉保本價 {new_sl:.4f}"
                                            )
                                            
                                        msg = (
                                            f"🎯 <b>[幣安限價 TP1 成交] {symbol} 順利達標！</b>\n"
                                            f"────────────────────────────\n"
                                            f"當前價格: {last_price:.4f} (目標 TP1: {tps[0]:.4f})\n"
                                            f"平倉類型: <b>限價單 (LIMIT) 成交 - Maker 省下手續費！</b>\n"
                                            f"止損調整: 已自動將止損價修改為開倉保本價 <b>{new_sl:.4f}</b> (鎖定零風險持倉)！\n"
                                            f"────────────────────────────\n"
                                        )
                                        send_telegram(msg)
                                        
                                        sl = new_sl
                                        current_tp_level = 1
                        except Exception as risk_err:
                            log.error(f"監控檢查幣安持倉大小失敗 {symbol}: {risk_err}")
                        
                    # 判斷是多單 (BUY) 還是空單 (SELL)
                    is_long = (side == "BUY")
                    
                    # ── 💡 智能動能背離與 AI 雙重平倉保護 (RSI Momentum Decay + AGY AI Exit) ──
                    # 雙軌啟動條件 (方案 B)：1) 已達標 TP1，或 2) 持倉滿 30 分鐘且當前處於「浮盈狀態 (PnL > 0)」
                    allow_momentum_check = False
                    if current_tp_level >= 1:
                        allow_momentum_check = True
                    else:
                        pos_time_str = pos.get("timestamp")
                        if pos_time_str:
                            try:
                                created_dt = parse_sim_time(pos_time_str)
                                tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                                now_dt = datetime.now(tz_taipei)
                                created_dt = created_dt.replace(tzinfo=tz_taipei) if created_dt.tzinfo is None else created_dt
                                elapsed_mins = (now_dt - created_dt).total_seconds() / 60.0
                                # 方案 B：持倉滿 30 分鐘 + 必須處於浮盈狀態
                                is_in_profit = (last_price > entry_price) if is_long else (last_price < entry_price)
                                if elapsed_mins >= 30.0 and is_in_profit:
                                    allow_momentum_check = True
                            except Exception:
                                pass

                    if allow_momentum_check:
                        try:
                            # 獲取近 30 根 15m K線檢測動能背離
                            r_kl15 = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval=15m&limit=30", timeout=5)
                            if r_kl15.ok:
                                c_15m = [float(x[4]) for x in r_kl15.json()]
                                from analyzers.indicators import calc_rsi
                                rsi_15m_vals = [v for v in calc_rsi(c_15m, 14) if v is not None]
                                if rsi_15m_vals:
                                    last_rsi_15m = rsi_15m_vals[-1]
                                    decay_triggered = False
                                    decay_txt = ""
                                    if is_long and last_rsi_15m < 45:
                                        decay_triggered = True
                                        decay_txt = f"15m RSI 動能背離衰退 (RSI={last_rsi_15m:.1f} < 45)"
                                    elif not is_long and last_rsi_15m > 55:
                                        decay_triggered = True
                                        decay_txt = f"15m RSI 動能背離衰退 (RSI={last_rsi_15m:.1f} > 55)"
                                        
                                    if decay_triggered:
                                        # 💡 指標條件滿足後，詢問 AGY AI（Gemini 3.6 Flash）二次確認是否應該離場
                                        log.info(f"⚡ [Momentum Exit] {symbol} (ID: {pos_id}) 指標滿足 ({decay_txt})，調用 AGY AI (gemini-3.6-flash) 二次確認中...")
                                        ai_decision, ai_reason = verify_expiry_with_ai(pos, last_price)
                                        
                                        pnl = ((last_price - entry_price) / entry_price * 100) if is_long else ((entry_price - last_price) / entry_price * 100)
                                        pnl_lev = pnl * pos.get("leverage", 10)

                                        if ai_decision == "HOLD":
                                            log.info(f"🛡️ [AI 智能保護] {symbol} (ID: {pos_id}) 指標提示動能衰退，但 AGY AI 評估後決定 HOLD 抱單！原因：{ai_reason}")
                                            if pos.get("pionex_order_id"):
                                                db_manager.append_historical_trade_event(
                                                    pos["pionex_order_id"],
                                                    f"動能衰退指標觸發，但經 AGY AI 綜合評估決議 HOLD 抱單。原因：{ai_reason}"
                                                )
                                        else:
                                            log.info(f"⚡ [Momentum Decay Exit] {symbol} (ID: {pos_id}) 經 AGY AI 確認離場 ({decay_txt})！原因：{ai_reason}，盈虧: {pnl_lev:+.2f}%")
                                            close_reason_full = f"{decay_txt} | AI: {ai_reason}"
                                            close_res = db_manager.close_binance_position(
                                                pos_id,
                                                event_txt=close_reason_full,
                                                reach_status="CLOSED",
                                                pnl_percent=pnl_lev,
                                                close_reason_code="MOMENTUM_DECAY_EXIT"
                                            )
                                            if close_res.get("success"):
                                                decay_txt_safe = close_reason_full.replace("<", "&lt;").replace(">", "&gt;")
                                                sent_ok = send_telegram(
                                                    f"🚀 <b>[動能背離 + AI 離場通知] {symbol}</b>\n"
                                                    f"────────────────────────────\n"
                                                    f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                                                    f"開倉價: {entry_price:.4f} ➔ 離場價: {last_price:.4f}\n"
                                                    f"單筆盈虧: <b>{pnl_lev:+.2f}%</b>\n"
                                                    f"AI 決策原因: <i>{decay_txt_safe}</i>\n"
                                                    f"────────────────────────────\n"
                                                    f"狀態: 已經過 AGY AI 確認並執行市價平倉落袋為安。"
                                                )
                                                if not sent_ok:
                                                    log.warning(f"⚠️ [Telegram] 動能背離離場通知發送失敗: {symbol} (ID: {pos_id})")
                                                if pos.get("pionex_order_id"):
                                                    db_manager.update_historical_trade_audit(
                                                        pionex_order_id=pos["pionex_order_id"],
                                                        notify_sent=sent_ok,
                                                        notify_error=None if sent_ok else "telegram_send_failed"
                                                    )
                                                continue
                        except Exception as decay_err:
                            log.warning(f"檢查 {symbol} 動能背離離場失敗: {decay_err}")
                    
                    # 檢查持倉超時失效 (24小時優化)
                    try:
                        auto_close_enabled = pos.get("auto_close", 1) == 1
                        pos_time_str = pos.get("timestamp")
                        if pos_time_str and auto_close_enabled:
                            created_dt = parse_sim_time(pos_time_str)
                            tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                            now_dt = datetime.now(tz_taipei)
                            created_dt = created_dt.replace(tzinfo=tz_taipei) if created_dt.tzinfo is None else created_dt
                            if now_dt - created_dt > timedelta(hours=12):
                                # 💡 智能逾期平倉判定 (避免無腦砍在虧損最高點/原始止損臨界點)
                                is_in_profit = (last_price >= entry_price) if is_long else (last_price <= entry_price)
                                
                                # 計算當前價格在「開倉價」與「止損價」之間的百分比位置
                                denom = (entry_price - sl) if is_long else (sl - entry_price)
                                if denom > 0:
                                    sl_ratio = (entry_price - last_price) / denom if is_long else (last_price - entry_price) / denom
                                else:
                                    sl_ratio = 0.50
                                    
                                if current_tp_level >= 1:
                                    # 1. 若已達標 TP1 以上，因已移至保本價，無本金風險，不執行逾期平倉。
                                    pass
                                elif is_in_profit:
                                    # 2. 浮盈狀態下，不執行逾期平倉。
                                    pass
                                elif sl_ratio < 0.20:
                                    # 3. 浮虧極微 (低於止損距離的 20%)，視為健康回踩盤整，不割肉。
                                    pass
                                elif sl_ratio > 0.80:
                                    # 4. 浮虧極深 (大於止損距離的 80%)，此時主動割肉幾乎承受滿額虧損，不如留給原始止損發揮作用 (保留觸底反彈的生路)。
                                    pass
                                else:
                                    # ── 💡 使用全新 AI 綜合分析輿情、K線、與幣安多空數據決定是否割肉或展期 ──
                                    log.info(f"⏰ 持倉 {symbol} (ID: {pos_id}) 已逾期，正在調用 AI 進行智能風控評估...")
                                    ai_decision, ai_reason = verify_expiry_with_ai(pos, last_price)
                                    
                                    pnl = ((last_price - entry_price) / entry_price * 100) if is_long else ((entry_price - last_price) / entry_price * 100)
                                    pnl_with_lev = pnl * pos["leverage"]
                                    
                                    if ai_decision == "HOLD":
                                        log.info(f"🛡️ [AI 智能展期] 持倉 {symbol} (ID: {pos_id}) 暫緩割肉平倉，展期 3 小時。原因：{ai_reason}")
                                        new_time_str = datetime.now(timezone(timedelta(hours=TZ_OFFSET))).strftime("%Y-%m-%d %H:%M:%S")
                                        with db_manager.get_db() as conn:
                                            conn.execute("UPDATE active_positions SET timestamp = ? WHERE id = ?", (new_time_str, pos_id))
                                            conn.commit()
                                            
                                        if pos.get("pionex_order_id"):
                                            db_manager.append_historical_trade_event(
                                                pos["pionex_order_id"], 
                                                f"持倉逾期，經 AI 綜合分析新聞與期貨多空數據判定，暫緩平倉並展期 3 小時。原因：{ai_reason}"
                                            )
                                            
                                        msg = (
                                            f"🤖 <b>[AI 智能展期通知] {symbol}</b>\n"
                                            f"────────────────────────────\n"
                                            f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                                            f"原開倉時間: {pos_time_str}\n"
                                            f"當前價格: {last_price:.4f}\n"
                                            f"盈虧: {pnl_with_lev:+.2f}%\n"
                                            f"AI 決策: <b>🟢 展期繼續持單 3 小時</b>\n"
                                            f"展期原因: <i>{ai_reason}</i>\n"
                                            f"────────────────────────────\n"
                                            f"狀態: 已重置逾期計時器，繼續持倉監控。"
                                        )
                                        send_telegram(msg)
                                        continue
                                    else:
                                        log.info(f"⏰ 持倉 {symbol} (ID: {pos_id}) 經 AI 判定無起色，執行市價平倉割肉... 原因：{ai_reason}")
                                        close_event_txt = f"持倉已逾期且經 AI 判定執行智能割肉，原因：{ai_reason}，盈虧 {pnl_with_lev:+.2f}%"
                                        close_res = db_manager.close_binance_position(
                                            pos_id,
                                            event_txt=close_event_txt,
                                            reach_status="EXPIRED",
                                            pnl_percent=pnl_with_lev,
                                            close_reason_code="AI_TIMEOUT_CUT"
                                        )
                                        if close_res.get("success"):
                                            msg = (
                                                f"⏰ <b>[AI 智能割肉通知] {symbol} 持倉已逾期，自動平倉割肉！</b>\n"
                                                f"────────────────────────────\n"
                                                f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                                                f"開倉時間: {pos_time_str}\n"
                                                f"當前價格: {last_price:.4f}\n"
                                                f"單筆盈虧: {pnl_with_lev:+.2f}%\n"
                                                f"AI 決策: <b>🔴 執行市價平倉割肉</b>\n"
                                                f"平倉原因: <i>{ai_reason}</i>\n"
                                                f"────────────────────────────\n"
                                                f"狀態: 已自動執行市價平倉割肉，停止持倉監控。"
                                            )
                                            sent_ok = send_telegram(msg)
                                            if pos.get("pionex_order_id"):
                                                db_manager.update_historical_trade_audit(
                                                    pionex_order_id=pos["pionex_order_id"],
                                                    notify_sent=sent_ok,
                                                    notify_error=None if sent_ok else "telegram_send_failed"
                                                )
                                            continue
                                        else:
                                            log.error(f"  [Position Monitor] AI 逾期平倉失敗 {symbol} (ID: {pos_id}): {close_res.get('error')}")
                    except Exception as exp_err:
                        log.error(f"檢查持倉過期失敗 {symbol} (ID: {pos_id}): {exp_err}")
                    
                    # 檢查大盤聯動方向衝突
                    btc_state = BTC_MACRO_STATE.get("state", "NEUTRAL")
                    conflict = False
                    if btc_state == "BEARISH" and is_long:
                        conflict = True
                    elif btc_state == "BULLISH" and not is_long:
                        conflict = True
                        
                    if conflict:
                        if pos_id not in self.warned_positions:
                            self.warned_positions.add(pos_id)
                            direction_str = "多單 (LONG)" if is_long else "空單 (SHORT)"
                            msg = (
                                f"⚠️ <b>[大盤預警] {symbol} 持倉方向與大盤趨勢相反！</b>\n"
                                f"────────────────────────────\n"
                                f"您的持倉:  {direction_str}\n"
                                f"BTC 狀態:  {btc_state} ({BTC_MACRO_STATE.get('filter_rule', '')})\n"
                                f"進場價格:  {entry_price:.4f}\n"
                                f"目前價格:  {last_price:.4f}\n"
                                f"────────────────────────────\n"
                                f"💡 <b>警示:</b> 目前大盤大趨勢已轉為相反方向，逆勢持倉風險極高！建議立刻至儀表板手動平倉或採取防禦措施。"
                            )
                            sent_ok = send_telegram(msg)
                            if pos.get("pionex_order_id"):
                                db_manager.update_historical_trade_audit(
                                    pionex_order_id=pos["pionex_order_id"],
                                    close_reason_code="EXCHANGE_SYNC_SL" if hit_sl_real else "EXCHANGE_SYNC_CLOSE",
                                    notify_sent=sent_ok,
                                    notify_error=None if sent_ok else "telegram_send_failed"
                                )
                            log.info(f"  [BTC Filter] Sent macro conflict warning for position {pos_id} ({symbol})")
                    else:
                        if pos_id in self.warned_positions:
                            self.warned_positions.remove(pos_id)
                    
                    # 檢查是否觸發止損
                    sl_triggered = False
                    if is_long:
                        if last_price <= sl:
                            sl_triggered = True
                    else:
                        if last_price >= sl:
                            sl_triggered = True
                            
                    if sl_triggered:
                        # 觸發止損/追蹤保本止盈！
                        # 1. 執行平倉
                        mock_mode = db_manager.get_setting("binance_mock_mode", "true") == "true"
                        close_err = None
                        if not mock_mode:
                            close_res = db_manager.close_binance_position(pos_id, event_txt=None)
                            if not close_res["success"]:
                                close_err = close_res.get("error")
                                
                        # 2. 判斷是否為獲利追蹤止盈平倉 (TP後回踩保本)
                        is_profit_sl = (last_price > entry_price) if is_long else (last_price < entry_price)
                        current_tp_level = pos.get("current_tp_level", 0)
                        
                        acc_pnl = pos.get("accumulated_pnl", 0.0) or 0.0
                        rem_pnl = float(pos["size"]) * ((last_price - entry_price) if is_long else (entry_price - last_price))
                        total_pnl_usdt = round(acc_pnl + rem_pnl, 4)

                        is_win = (current_tp_level >= 1 or is_profit_sl or total_pnl_usdt > 0)
                        reach_tag = f"TP{max(1, current_tp_level)}" if is_win else "SL"
                        event_msg = f"價格觸碰追蹤保本止盈價 {sl:.4f}，獲利平倉離場，總盈虧 {total_pnl_usdt:+.2f} USDT" if is_win else f"價格觸碰止損 {sl:.4f}，合約自動平倉離場，總盈虧 {total_pnl_usdt:+.2f} USDT"
                        status_db = "CLOSED" if is_win else "SL_HIT"

                        with db_manager.get_db() as conn:
                            conn.execute(
                                "UPDATE active_positions SET status = ?, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
                                (status_db, db_manager._now_taipei_str(), pos_id),
                            )
                            conn.commit()
                        remove_active_trade_by_symbol_and_direction(symbol, 'long' if is_long else 'short')
                            
                        # 同步更新歷史分析表
                        if pos.get("pionex_order_id"):
                            db_manager.update_historical_trade_status(pionex_order_id=pos["pionex_order_id"], reach=reach_tag, pnl=total_pnl_usdt)
                            db_manager.reconcile_trade_pnl(pos["pionex_order_id"], fallback_pnl=total_pnl_usdt)
                            db_manager.append_historical_trade_event(pos["pionex_order_id"], event_msg)
                            
                        # 3. 發送通知
                        err_msg = f" (平倉失敗: {close_err})" if close_err else ""
                        msg_title = "🎯 <b>[幣安託管通知] " + symbol + (" 觸發追蹤保本止盈！</b>\n" if is_win else " 觸發止損！</b>\n")
                        msg = msg_title
                        msg += f"────────────────────────────\n"
                        msg += f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                        msg += f"槓桿: {pos['leverage']}x\n"
                        msg += f"開倉價格: {entry_price:.4f}\n"
                        msg += f"出場價格: {last_price:.4f}\n"
                        msg += f"總盈虧: {total_pnl_usdt:+.2f} USDT{err_msg}\n"
                        msg += f"────────────────────────────\n"
                        msg += f"狀態: 已自動執行委託平倉結算。"
                        
                        sent_ok = send_telegram(msg)
                        if pos.get("pionex_order_id"):
                            db_manager.update_historical_trade_audit(
                                pionex_order_id=pos["pionex_order_id"],
                                close_reason_code="TRAILING_TP_EXIT" if is_win else "SL_HIT",
                                notify_sent=sent_ok,
                                notify_error=None if sent_ok else "telegram_send_failed"
                            )
                        log.info(f"🚨 {symbol} 觸發{'追蹤止盈' if is_win else '止損'}平倉，已發送 Telegram 通知。")
                        continue
                        
                    # 檢查是否觸發停利 (移動止損)
                    new_tp_level = current_tp_level
                    new_sl = sl
                    
                    if is_long:
                        # 多單 TP1 ~ TP4
                        if current_tp_level < 1 and tps[0] > 0 and last_price >= tps[0]:
                            new_tp_level = 1
                            try:
                                _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                                new_sl = round((entry_price + sl) / 2, price_prec)
                            except Exception:
                                new_sl = round((entry_price + sl) / 2, 4)
                        if current_tp_level < 2 and tps[1] > 0 and last_price >= tps[1]:
                            new_tp_level = 2
                            new_sl = tps[0] # 價格到 TP2 止損改為 TP1
                        if current_tp_level < 3 and tps[2] > 0 and last_price >= tps[2]:
                            new_tp_level = 3
                            new_sl = tps[1] # 價格到 TP3 止損改為 TP2
                        if current_tp_level < 4 and tps[3] > 0 and last_price >= tps[3]:
                            new_tp_level = 4
                            try:
                                _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                                new_sl = round((tps[2] + tps[3]) / 2, price_prec)  # 止損改為 TP3~TP4 中間點，鎖更多利潤
                            except Exception:
                                new_sl = round((tps[2] + tps[3]) / 2, 4)
                    else:
                        # 空單 TP1 ~ TP4
                        if current_tp_level < 1 and tps[0] > 0 and last_price <= tps[0]:
                            new_tp_level = 1
                            try:
                                _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                                new_sl = round((entry_price + sl) / 2, price_prec)
                            except Exception:
                                new_sl = round((entry_price + sl) / 2, 4)
                        if current_tp_level < 2 and tps[1] > 0 and last_price <= tps[1]:
                            new_tp_level = 2
                            new_sl = tps[0]
                        if current_tp_level < 3 and tps[2] > 0 and last_price <= tps[2]:
                            new_tp_level = 3
                            new_sl = tps[1]
                        if current_tp_level < 4 and tps[3] > 0 and last_price <= tps[3]:
                            new_tp_level = 4
                            try:
                                _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                                new_sl = round((tps[2] + tps[3]) / 2, price_prec)  # 止損改為 TP3~TP4 中間點，鎖更多利潤
                            except Exception:
                                new_sl = round((tps[2] + tps[3]) / 2, 4)
                            
                    if new_tp_level > current_tp_level:
                        # 執行各階段的分批減倉或全平倉
                        close_completed = False
                        if new_tp_level == 1:
                            p_res = db_manager.partially_close_binance_position(pos_id, ratio=0.20, event_txt="價格抵達 TP1，自動執行 20% 分批平倉停利鎖定利潤")
                            if p_res["success"]:
                                log.info(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP1，20% 分批停利成功。")
                            else:
                                log.error(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP1，20% 分批停利失敗: {p_res.get('error')}")
                        elif new_tp_level == 2:
                            p_res = db_manager.partially_close_binance_position(pos_id, ratio=0.20, event_txt="價格抵達 TP2，自動再執行 20% 分批平倉停利")
                            if p_res["success"]:
                                log.info(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP2，20% 分批停利成功。")
                            else:
                                log.error(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP2，20% 分批停利失敗: {p_res.get('error')}")
                        elif new_tp_level == 3:
                            p_res = db_manager.partially_close_binance_position(pos_id, ratio=0.20, event_txt="價格抵達 TP3，自動再執行 20% 分批平倉停利")
                            if p_res["success"]:
                                log.info(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP3，20% 分批停利成功。")
                            else:
                                log.error(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP3，20% 分批停利失敗: {p_res.get('error')}")
                        elif new_tp_level == 4:
                            # TP4 不平倉 —— 啟動移動止損追蹤模式，剩餘 40% 倉位持續持有
                            # 止損鎖定在 TP3 價格（new_sl 已在上方邏輯中設為 tps[2]）
                            log.info(f"  [Position Monitor] {symbol} (ID: {pos_id}) 達標 TP4，不平倉，啟動移動止損追蹤模式，止損鎖定至 TP3 ({new_sl:.4f})。")
                            # close_completed 維持 False → 走下方 DB 更新流程

                        if not close_completed:
                            # 更新資料庫中的 sl 與 current_tp_level；TP4 額外設定移動止損基準價
                            with db_manager.get_db() as conn:
                                if new_tp_level == 4:
                                    # trailing_base_price 設為 TP4 價格，後續追蹤從此起算
                                    conn.execute(
                                        "UPDATE active_positions SET sl = ?, current_tp_level = ?, trailing_base_price = ? WHERE id = ?",
                                        (new_sl, new_tp_level, tps[3], pos_id)
                                    )
                                else:
                                    conn.execute("UPDATE active_positions SET sl = ?, current_tp_level = ? WHERE id = ?", (new_sl, new_tp_level, pos_id))
                                conn.commit()

                            # 同步更新幣安交易所上的實體止損委託單
                            if not mock_mode:
                                sl_order_side = "SELL" if is_long else "BUY"
                                db_manager.update_binance_stop_loss(symbol, sl_order_side, new_sl, position_id=pos_id)

                        # 同步更新歷史分析表
                        if pos.get("pionex_order_id"):
                            db_manager.update_historical_trade_status(
                                pionex_order_id=pos["pionex_order_id"], 
                                reach=f"TP{new_tp_level}"
                            )
                            if new_tp_level == 1:
                                event_desc = f"價格達標 TP1 ({tps[0]:.4f})，已自動分批平倉 20% 鎖定利潤，止損調整至保本價 {new_sl:.4f}"
                            elif new_tp_level == 2:
                                event_desc = f"價格達標 TP2 ({tps[1]:.4f})，已自動分批平倉 20%，止損調整至 TP1 {new_sl:.4f}"
                            elif new_tp_level == 3:
                                event_desc = f"價格達標 TP3 ({tps[2]:.4f})，已自動分批平倉 20%，止損調整至 TP2 {new_sl:.4f}"
                            elif new_tp_level == 4:
                                event_desc = f"價格達標 TP4 ({tps[3]:.4f})，不平倉啟動移動止損追蹤，止損鎖定至 TP3 ({new_sl:.4f})，每+5%推進止損+5%"
                            else:
                                event_desc = f"價格達標 TP{new_tp_level} ({tps[new_tp_level-1]:.4f})，分批平倉並移動止損至 {new_sl:.4f}"
                            
                            db_manager.append_historical_trade_event(
                                pos["pionex_order_id"], 
                                event_desc
                            )

                        # 發送通知
                        tp_price = tps[new_tp_level - 1]
                        if new_tp_level == 4:
                            msg = f"🚀 <b>[幣安託管通知] {symbol} 抵達極限停利點 TP4！移動止損追蹤啟動！</b>\n"
                            msg += f"────────────────────────────\n"
                            msg += f"當前價格: {last_price:.4f} (目標 TP4: {tp_price:.4f})\n"
                            msg += f"剩餘倉位: <b>40%（TP1+TP2+TP3 已各減倉 20%，共鎖利 60%）</b>\n"
                            msg += f"止損鎖定: 系統已將止損鎖定至 TP3 <b>{new_sl:.4f}</b>（保護利潤不低於 TP3）！\n"
                            msg += f"移動追蹤: <b>正式啟動！往後每有利運行 +5%，止損即同步上推 +5%！</b>\n"
                            msg += f"────────────────────────────\n"
                            send_telegram(msg)
                            log.info(f"🚀 {symbol} 達標 TP4，啟動移動止損追蹤，止損鎖定至 TP3 ({new_sl:.4f})。")
                        else:
                            msg = f"🎯 <b>[幣安託管通知] {symbol} 抵達 TP{new_tp_level}！</b>\n"
                            msg += f"────────────────────────────\n"
                            msg += f"當前價格: {last_price:.4f} (目標 TP{new_tp_level}: {tp_price:.4f})\n"
                            if new_tp_level == 1:
                                msg += f"分批停利: <b>已自動市價平倉 20% 鎖定利潤！</b>\n"
                                msg += f"止損調整: 已自動將止損價修改為開倉保本價 <b>{new_sl:.4f}</b> (鎖定零風險持倉)！\n"
                            elif new_tp_level == 2:
                                msg += f"分批停利: <b>已自動市價再平倉 20% 鎖定利潤！</b>\n"
                                msg += f"止損調整: 已自動將止損價修改為 <b>{new_sl:.4f}</b> (TP1 保護利潤)！\n"
                            elif new_tp_level == 3:
                                msg += f"分批停利: <b>已自動市價再平倉 20% 鎖定利潤！</b>\n"
                                msg += f"止損調整: 已自動將止損價修改為 <b>{new_sl:.4f}</b> (TP2 保護利潤)！\n"
                            msg += f"────────────────────────────\n"
                            send_telegram(msg)
                            log.info(f"🎯 {symbol} 抵達 TP{new_tp_level}，已發送 Telegram 通知。")
                    else:
                        # 💡 動態止損鎖定與收緊邏輯：已達標 TP1 以上才進行收緊，避免初期震盪停損
                        if current_tp_level == 4:
                            base_p = pos.get("trailing_base_price", 0.0)
                            if not base_p or base_p <= 0.0:
                                base_p = tps[3]
                            
                            _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                            updated = False
                            new_base_p = base_p
                            new_sl_val = sl
                            
                            if is_long:
                                # 每 +5% 觸發一次，止損同步上推 +5%
                                step_target = base_p * 1.05
                                if last_price >= step_target:
                                    new_base_p = round(step_target, price_prec)
                                    new_sl_val = round(sl + (base_p * 0.05), price_prec)
                                    updated = True
                            else:
                                # 空單：每 -5% 觸發一次，止損同步下推 -5%
                                step_target = base_p * 0.95
                                if last_price <= step_target:
                                    new_base_p = round(step_target, price_prec)
                                    new_sl_val = round(sl - (base_p * 0.05), price_prec)
                                    updated = True
                                    
                            if updated:
                                log.info(f"🚀 [Position Monitor] {symbol} (ID: {pos_id}) 移動止損推進 (+5%)！基準價: {base_p:.4f} -> {new_base_p:.4f} | 止損價: {sl:.4f} -> {new_sl_val:.4f}")
                                with db_manager.get_db() as conn:
                                    conn.execute("UPDATE active_positions SET sl = ?, trailing_base_price = ? WHERE id = ?", (new_sl_val, new_base_p, pos_id))
                                    conn.commit()
                                
                                if not mock_mode:
                                    sl_order_side = "SELL" if is_long else "BUY"
                                    db_manager.update_binance_stop_loss(symbol, sl_order_side, new_sl_val, position_id=pos_id)
                                    
                                msg = f"🔥 <b>[幣安託管通知] {symbol} 移動止損推進中！</b>\n"
                                msg += f"────────────────────────────\n"
                                msg += f"當前價格: {last_price:.4f}\n"
                                msg += f"推進條件: <b>向有利方向運行 +5%，止損同步上推！</b>\n"
                                msg += f"基準推移: {base_p:.4f} ➡️ <b>{new_base_p:.4f}</b>\n"
                                msg += f"止損推高: {sl:.4f} ➡️ <b>{new_sl_val:.4f} (持續鎖死利潤)！</b>\n"
                                msg += f"────────────────────────────\n"
                                send_telegram(msg)
                        
                        elif current_tp_level >= 1 and current_tp_level < len(tps):
                            next_tp = tps[current_tp_level]
                            if next_tp and next_tp > 0:
                                base_price = entry_price if current_tp_level == 1 else tps[current_tp_level - 2]
                                tighter_sl = sl
                                
                                # 1. 接近下一目標 (已於實盤中停用，改為留給移動保本/階梯止損足夠空間)
                                # 2. 持倉時間過長 (>= 12小時)，進行收緊至 25% 浮盈 (固定步階)
                                pos_time_str = pos.get("timestamp")
                                if pos_time_str:
                                    try:
                                        created_dt = parse_sim_time(pos_time_str)
                                        tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                                        now_dt = datetime.now(tz_taipei)
                                        created_dt = created_dt.replace(tzinfo=tz_taipei) if created_dt.tzinfo is None else created_dt
                                        if now_dt - created_dt > timedelta(hours=12):
                                            time_sl = base_price + 0.25 * (next_tp - base_price) if is_long else base_price - 0.25 * (base_price - next_tp)
                                            if is_long and time_sl > tighter_sl:
                                                tighter_sl = time_sl
                                            elif not is_long and time_sl < tighter_sl:
                                                tighter_sl = time_sl
                                    except Exception as t_err:
                                        log.error(f"計算時間滯留止損失敗: {t_err}")
                                
                                # 若有更佳的止損價格，則更新之 (只朝利潤方向前進)
                                if (is_long and tighter_sl > sl) or (not is_long and tighter_sl < sl):
                                    log.info(f"📈 [Position Monitor] {symbol} (ID: {pos_id}) 觸發動態止損調整: {sl:.4f} -> {tighter_sl:.4f}")
                                    with db_manager.get_db() as conn:
                                        conn.execute("UPDATE active_positions SET sl = ? WHERE id = ?", (tighter_sl, pos_id))
                                        conn.commit()
                                        
                                    if not mock_mode:
                                        sl_order_side = "SELL" if is_long else "BUY"
                                        db_manager.update_binance_stop_loss(symbol, sl_order_side, tighter_sl, position_id=pos_id)
                                        
                                    if pos.get("pionex_order_id"):
                                        db_manager.append_historical_trade_event(
                                            pos["pionex_order_id"],
                                            f"觸發動態止損收緊：移動止損收緊調整至 {tighter_sl:.4f}"
                                        )
                                        
                                    msg = (
                                        f"📈 <b>[幣安託管通知] {symbol} 觸發動態止損收緊！</b>\n"
                                        f"────────────────────────────\n"
                                        f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                                        f"原止損價: {sl:.4f}\n"
                                        f"新移動止損: <b>{tighter_sl:.4f}</b> (已鎖定階梯利潤！)\n"
                                        f"當前價格: {last_price:.4f}\n"
                                        f"目標下一TP: {next_tp:.4f}\n"
                                        f"────────────────────────────\n"
                                    )
                                    send_telegram(msg)
                            
            except Exception as e:
                log.error(f"監控線程循環異常: {e}")
                
            # 2. 監控未平倉的開單建議 (SIGNAL)
            try:
                with db_manager.get_db() as conn:
                    pending_signals = conn.execute("SELECT * FROM historical_trades WHERE trade_type IN ('SIGNAL', 'AI_SENTIMENT_SIGNAL') AND reach = 'PENDING'").fetchall()
                
                for sig in pending_signals:
                    sig_id = sig["id"]
                    sig_symbol = sig["symbol"]
                    sig_dir = sig["direction"]
                    sig_entry = sig["entry"]
                    sig_sl = sig["sl"]
                    sig_tps = [sig["tp1"], sig["tp2"], sig["tp3"], sig["tp4"]]
                    sig_tps = [t for t in sig_tps if t is not None and t > 0]
                    sig_curr_sl = sig["current_sl"] if sig["current_sl"] is not None else sig_sl
                    sig_curr_tp_level = sig["current_tp_level"]
                    sig_time_str = sig["time_str"]
                    
                    # 獲取即時價格
                    sig_price = None
                    try:
                        r_sig = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sig_symbol.upper()}", timeout=5)
                        if r_sig.ok:
                            sig_price = float(r_sig.json().get("price", 0.0))
                    except Exception as price_err:
                        log.error(f"信號監控獲取價格失敗 {sig_symbol}: {price_err}")
                        
                    if not sig_price:
                        continue
                        
                    is_sig_long = (sig_dir == "long")
                        
                    # 檢查超時失效 (24小時優化)
                    try:
                        created_dt = parse_sim_time(sig_time_str)
                        now_dt = datetime.now()
                        if now_dt - created_dt > timedelta(hours=24):
                            is_sig_in_profit = (sig_price > sig_entry) if is_sig_long else (sig_price < sig_entry)
                            if sig_curr_tp_level >= 1:
                                # 已達標 TP1 以上，繼續追蹤移動止損，不執行逾期結算
                                pass
                            elif is_sig_in_profit:
                                # 當前處於獲利狀態，繼續追蹤，不執行逾期結算
                                pass
                            else:
                                final_reach = "EXPIRED"
                                db_manager.update_historical_trade_status(trade_id=sig_id, reach=final_reach)
                                db_manager.append_historical_trade_event(None, "信號已逾期 24 小時且未獲利，停止監控追蹤", trade_id=sig_id)
                                log.info(f"⏰ 信號 {sig_symbol} 已逾期 24 小時未獲利，自動結算狀態為 {final_reach}。")
                                continue
                    except Exception as exp_err:
                        log.error(f"檢查信號過期失敗: {exp_err}")
                    
                    # 檢查是否觸發止損
                    sig_sl_triggered = False
                    if is_sig_long:
                        if sig_price <= sig_curr_sl:
                            sig_sl_triggered = True
                    else:
                        if sig_price >= sig_curr_sl:
                            sig_sl_triggered = True
                            
                    if sig_sl_triggered:
                        if sig_curr_tp_level > 0:
                            final_reach = f"TP{sig_curr_tp_level}"
                            event_txt = f"價格觸及移動止損 {sig_curr_sl:.4f}，追蹤平倉離場，最終達標 TP{sig_curr_tp_level}"
                        else:
                            final_reach = "SL"
                            event_txt = f"價格觸碰止損 {sig_sl:.4f}，信號失效"
                            
                        db_manager.update_historical_trade_status(trade_id=sig_id, reach=final_reach)
                        db_manager.append_historical_trade_event(None, event_txt, trade_id=sig_id)
                        log.info(f"🚨 信號 {sig_symbol} 達標狀態更新為 {final_reach} 並停止監控。")
                        continue
                        
                    # 檢查是否觸發停利
                    new_sig_tp_level = sig_curr_tp_level
                    new_sig_sl = sig_curr_sl
                    
                    if is_sig_long:
                        if sig_curr_tp_level < 1 and sig_price >= sig_tps[0]:
                            new_sig_tp_level = 1
                            new_sig_sl = sig_entry
                        if len(sig_tps) >= 2 and sig_curr_tp_level < 2 and sig_price >= sig_tps[1]:
                            new_sig_tp_level = 2
                            new_sig_sl = sig_tps[0]
                        if len(sig_tps) >= 3 and sig_curr_tp_level < 3 and sig_price >= sig_tps[2]:
                            new_sig_tp_level = 3
                            new_sig_sl = sig_tps[1]
                        if len(sig_tps) >= 4 and sig_curr_tp_level < 4 and sig_price >= sig_tps[3]:
                            new_sig_tp_level = 4
                            new_sig_sl = sig_tps[2]
                    else:
                        if sig_curr_tp_level < 1 and sig_price <= sig_tps[0]:
                            new_sig_tp_level = 1
                            new_sig_sl = sig_entry
                        if len(sig_tps) >= 2 and sig_curr_tp_level < 2 and sig_price <= sig_tps[1]:
                            new_sig_tp_level = 2
                            new_sig_sl = sig_tps[0]
                        if len(sig_tps) >= 3 and sig_curr_tp_level < 3 and sig_price <= sig_tps[2]:
                            new_sig_tp_level = 3
                            new_sig_sl = sig_tps[1]
                        if len(sig_tps) >= 4 and sig_curr_tp_level < 4 and sig_price <= sig_tps[3]:
                            new_sig_tp_level = 4
                            new_sig_sl = sig_tps[2]
                            
                    if new_sig_tp_level > sig_curr_tp_level:
                        tp_val_reached = sig_tps[new_sig_tp_level - 1]
                        event_txt = f"價格達標 TP{new_sig_tp_level} ({tp_val_reached:.4f})，移動止損調整至 {new_sig_sl:.4f}"
                        
                        # 抵達最後的 TP 則終止監控
                        final_reach = f"TP{new_sig_tp_level}" if new_sig_tp_level == len(sig_tps) else "PENDING"
                        
                        db_manager.update_historical_trade_status(
                            trade_id=sig_id,
                            reach=final_reach,
                            current_sl=new_sig_sl,
                            current_tp_level=new_sig_tp_level
                        )
                        db_manager.append_historical_trade_event(None, event_txt, trade_id=sig_id)
                        log.info(f"🎯 信號 {sig_symbol} 達標 TP{new_sig_tp_level}，移動止損更新為 {new_sig_sl:.4f}")
                    else:
                        # 💡 動態止損鎖定與收緊邏輯：已達標 TP1 以上才進行收緊，避免初期震盪停損
                        if sig_curr_tp_level >= 1 and sig_curr_tp_level < len(sig_tps):
                            sig_next_tp = sig_tps[sig_curr_tp_level]
                            if sig_next_tp and sig_next_tp > 0:
                                sig_base_price = sig_entry if sig_curr_tp_level == 1 else sig_tps[sig_curr_tp_level - 2]
                                tighter_sig_sl = sig_curr_sl
                                
                                denom = (sig_next_tp - sig_base_price) if is_sig_long else (sig_base_price - sig_next_tp)
                                if denom > 0:
                                    # 1. 接近下一目標 (進度 >= 70%) 鎖定該階段 50% 浮盈利潤 (固定步階)
                                    progress = (sig_price - sig_base_price) / denom if is_sig_long else (sig_base_price - sig_price) / denom
                                    if 0.70 <= progress < 1.0:
                                        tighter_sig_sl = sig_base_price + 0.50 * (sig_next_tp - sig_base_price) if is_sig_long else sig_base_price - 0.50 * (sig_base_price - sig_next_tp)
                                
                                # 2. 持倉時間過長 (>= 12小時)，進行收緊至 25% 浮盈 (固定步階)
                                if sig_time_str:
                                    try:
                                        created_dt = parse_sim_time(sig_time_str)
                                        now_dt = datetime.now()
                                        elapsed_hours = (now_dt - created_dt).total_seconds() / 3600.0
                                        if elapsed_hours > 12.0:
                                            time_sl = sig_base_price + 0.25 * (sig_next_tp - sig_base_price) if is_sig_long else sig_base_price - 0.25 * (sig_base_price - sig_next_tp)
                                            if is_sig_long and time_sl > tighter_sig_sl:
                                                tighter_sig_sl = time_sl
                                            elif not is_sig_long and time_sl < tighter_sig_sl:
                                                tighter_sig_sl = time_sl
                                    except Exception:
                                        pass
                                
                                # 若有更佳的止損價格，則更新資料庫
                                if (is_sig_long and tighter_sig_sl > sig_curr_sl) or (not is_sig_long and tighter_sig_sl < sig_curr_sl):
                                    db_manager.update_historical_trade_status(
                                        trade_id=sig_id,
                                        reach="PENDING",
                                        current_sl=tighter_sig_sl,
                                        current_tp_level=sig_curr_tp_level
                                    )
                                    db_manager.append_historical_trade_event(
                                        None,
                                        f"觸發動態止損調整：信號移動止損收緊調整至 {tighter_sig_sl:.4f}",
                                        trade_id=sig_id
                                    )
                                    log.info(f"🎯 信號 {sig_symbol} 觸發動態止損調整: {sig_curr_sl:.4f} -> {tighter_sig_sl:.4f}")
            except Exception as sig_err:
                log.error(f"信號監控子循環異常: {sig_err}")
                
            # 每小時執行一次自動信號對齊校正
            if time.time() - last_calib_time > 3600:
                calibrate_pending_signals()
                last_calib_time = time.time()
                
            curr_check_interval = int(db_manager.get_setting("position_check_interval", "10"))
            time.sleep(curr_check_interval)
