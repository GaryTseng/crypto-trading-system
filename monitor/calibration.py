# -*- coding: utf-8 -*-
"""
🔄 Antigravity Calibration Service
"""
import time
import json
from datetime import datetime, timezone, timedelta
import db_manager
from config import log, parse_sim_time, TZ_OFFSET, send_telegram, load_active_trades, save_active_trades, fetch_klines
from analyzers import analyze_symbol

def calibrate_pending_signals():
    try:
        log.info("  🔍 [Calibration] 正在自動對齊/校正所有未結算的 PENDING 歷史信號...")
        import sqlite3
        with db_manager.get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM historical_trades WHERE trade_type IN ('SIGNAL', 'FELISA_LS_SIGNAL', 'AI_SENTIMENT_SIGNAL') AND reach = 'PENDING'").fetchall()
        
        if not rows:
            log.info("  🔍 [Calibration] 無任何 PENDING 歷史信號需要校正。")
            return
            
        log.info(f"  🔍 [Calibration] 發現 {len(rows)} 筆 PENDING 信號，開始下載 K 線並對齊狀態...")
        for r in rows:
            sig_id = r["id"]
            symbol = r["symbol"]
            direction = r["direction"].upper()
            entry = r["entry"]
            sl = r["sl"]
            tps = [r["tp1"], r["tp2"], r["tp3"], r["tp4"]]
            tps = [t for t in tps if t is not None and t > 0]
            time_str = r["time_str"]
            
            try:
                dt = parse_sim_time(time_str)
            except Exception as pe:
                log.error(f"  [Calibration] 解析信號時間失敗 ID {sig_id}: {pe}")
                continue
                
            tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
            dt_aware = dt.replace(tzinfo=tz_taipei)
            start_ts = int(dt_aware.timestamp() * 1000)
            
            # 下載 1m K 線
            klines = fetch_klines(symbol, "1m", limit=1500, start_time_ms=start_ts)
            if not klines:
                continue
                
            reached_tp = 0
            hit_results = [False] * len(tps)
            sl_hit = False
            expired = False
            extreme_price = float(klines[0][1])
            current_sl = sl
            exit_price = None
            exit_reason = "PENDING"
            exit_time_str = "N/A"
            expiry_ts = start_ts + int(24.0 * 3600 * 1000)
            
            for k in klines:
                timestamp = int(k[0])
                open_p, high, low, close = map(float, k[1:5])
                
                if direction == 'LONG':
                    if high > extreme_price: extreme_price = high
                else:
                    if low < extreme_price or extreme_price == 0: extreme_price = low
                    
                if timestamp > expiry_ts:
                    is_k_in_profit = (close > entry) if direction == 'LONG' else (close < entry)
                    if reached_tp >= 1:
                        # 已達標 TP1 以上，不執行逾期
                        pass
                    elif is_k_in_profit:
                        # 浮盈中，不執行逾期
                        pass
                    else:
                        expired = True
                        ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                        exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                        exit_reason = "EXPIRED"
                        break
                    
                if direction == 'LONG':
                    if low <= current_sl:
                        sl_hit = True
                        exit_price = current_sl
                        ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                        exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                        exit_reason = f"TP{reached_tp}" if reached_tp > 0 else "SL"
                        break
                else:
                    if high >= current_sl:
                        sl_hit = True
                        exit_price = current_sl
                        ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                        exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                        exit_reason = f"TP{reached_tp}" if reached_tp > 0 else "SL"
                        break
                        
                is_break_final_tp = False
                new_tp_hit_this_kline = False
                for i in range(len(tps)):
                    tp = tps[i]
                    is_new_tp = False
                    if direction == 'LONG':
                        if high >= tp and not hit_results[i]:
                            is_new_tp = True
                    else:
                        if low <= tp and not hit_results[i]:
                            is_new_tp = True
                            
                    if is_new_tp:
                        hit_results[i] = True
                        reached_tp = max(reached_tp, i + 1)
                        new_tp_hit_this_kline = True
                        
                        if reached_tp == 1:
                            current_sl = entry
                        elif reached_tp == 2:
                            current_sl = tps[0]
                        elif reached_tp == 3:
                            current_sl = tps[1]
                        elif reached_tp == 4:
                            current_sl = tps[2]
                            
                        if reached_tp == len(tps):
                            is_break_final_tp = True
                            exit_price = tp
                            ts_utc = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                            exit_time_str = (ts_utc + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d %H:%M")
                            exit_reason = f"TP{reached_tp}"
                            break
                if is_break_final_tp:
                    break
                    
                # 💡 動態止損收緊優化
                if not new_tp_hit_this_kline and reached_tp >= 1 and reached_tp < len(tps):
                    next_tp = tps[reached_tp]
                    if next_tp and next_tp > 0:
                        base_price = entry if reached_tp == 1 else tps[reached_tp - 2]
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
            
            now_dt = datetime.now()
            is_over_24h = (now_dt - dt) > timedelta(hours=24)
            
            # 判斷是否真正該逾期結算
            should_expire_now = False
            if is_over_24h:
                is_curr_in_profit = False
                if klines:
                    last_k_close = float(klines[-1][4])
                    is_curr_in_profit = (last_k_close > entry) if direction == 'LONG' else (last_k_close < entry)
                
                if reached_tp >= 1:
                    should_expire_now = False
                elif is_curr_in_profit:
                    should_expire_now = False
                else:
                    should_expire_now = True
            
            if exit_reason != "PENDING" or should_expire_now:
                if exit_reason == "PENDING" and should_expire_now:
                    exit_reason = "EXPIRED"
                    exit_time_str = (dt + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
                
                timeline_str = r["timeline"]
                timeline_list = []
                if timeline_str:
                    try:
                        timeline_list = json.loads(timeline_str)
                    except Exception:
                        pass
                
                if exit_reason == "SL":
                    event_txt = f"價格觸碰止損 {sl:.4f}，信號失效結算"
                elif exit_reason == "EXPIRED":
                    event_txt = "信號已逾期 24 小時未達標，失效結算"
                else:
                    event_txt = f"價格觸及目標停利 {exit_reason} ({tps[reached_tp-1]:.4f})，結算完成"
                    if sl_hit:
                        event_txt += f" (後續觸碰移動止損 {current_sl:.4f} 結束追蹤)"
                    elif expired:
                        event_txt += f" (24小時逾期結算，停止追蹤)"
                
                elapsed_str = "24:00"
                if exit_time_str != "N/A":
                    try:
                        exit_dt = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M")
                        diff = exit_dt - dt
                        total_seconds = int(diff.total_seconds())
                        if total_seconds < 0: total_seconds = 0
                        hours = total_seconds // 3600
                        minutes = (total_seconds % 3600) // 60
                        elapsed_str = f"{hours:02d}:{minutes:02d}"
                    except Exception:
                        pass
                
                timeline_list.append({"time": elapsed_str, "event": event_txt})
                
                with db_manager.get_db() as conn:
                    conn.execute(
                        "UPDATE historical_trades SET reach = ?, current_sl = ?, current_tp_level = ?, timeline = ? WHERE id = ?",
                        (exit_reason, current_sl, reached_tp, json.dumps(timeline_list, ensure_ascii=False), sig_id)
                    )
                log.info(f"  [Calibration] 自動成功校正信號 ID {sig_id} ({symbol}) 為 {exit_reason}")
                
            time.sleep(0.05)
        log.info("  🔍 [Calibration] 信號狀態自動對齊校正完成。")
    except Exception as ce_err:
        log.error(f"  [Calibration] 自動對齊失敗: {ce_err}")

def sync_all_positions_with_binance_on_startup():
    mock_mode = db_manager.get_setting("binance_mock_mode", "true") == "true"
    if mock_mode:
        log.info("  [Position Monitor] 當前為模擬模式，略過啟動交易所持倉校正。")
        return
        
    log.info("  🔄 [Position Monitor] 開始校正所有託管持倉與交易所狀態...")
    active_pos_res = db_manager.get_active_positions()
    if not active_pos_res.get("success"):
        return
        
    positions = [p for p in active_pos_res.get("data", []) if p["status"] == "OPEN"]
    if not positions:
        log.info("  [Position Monitor] 當前無任何進行中的託管持倉紀錄。")
        return
        
    for pos in positions:
        symbol = pos["symbol"]
        pos_id = pos["id"]
        side = pos["side"]
        entry_price = pos["entry_price"]
        sl = pos["sl"]
        leverage = pos["leverage"]
        
        try:
            # 1. 查詢幣安實體持倉
            pos_risk_res = db_manager.send_binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()})
            if not pos_risk_res["success"]:
                log.warning(f"  [Position Monitor] 啟動校正無法獲取 {symbol} 持倉狀態: {pos_risk_res.get('error')}")
                continue
                
            matched_risk = None
            is_long = (side == "BUY")
            for pr in pos_risk_res["data"]:
                amt = float(pr.get("positionAmt", 0.0))
                if (is_long and amt > 0) or (not is_long and amt < 0):
                    matched_risk = pr
                    break
            
            # 獲取即時價格
            last_price = None
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}", timeout=5)
                if r.ok:
                    last_price = float(r.json().get("price", 0.0))
            except:
                pass
                
            if matched_risk is None or float(matched_risk.get("positionAmt", 0.0)) == 0.0:
                # 交易所上已無此部位，說明在電腦關機期間已經被平倉或止損了！
                log.info(f"  🔍 [Position Monitor] 發現交易所實體持倉 {symbol} (ID: {pos_id}) 已不存在，進行補正同步...")
                
                # 判斷是否為止損
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
                pnl_with_lev = pnl * leverage
                
                with db_manager.get_db() as conn:
                    conn.execute("UPDATE active_positions SET status = ? WHERE id = ?", (status_db, pos_id))
                    conn.commit()
                    
                if pos.get("pionex_order_id"):
                    db_manager.update_historical_trade_status(pionex_order_id=pos["pionex_order_id"], reach=reach_status)
                    db_manager.append_historical_trade_event(
                        pos["pionex_order_id"],
                        f"啟動自我校正：檢測到幣安實體持倉已結束，判定為 {'止損離場' if hit_sl_real else '交易所平倉或手動停利'}，盈虧 {pnl_with_lev:+.2f}%"
                    )
                
                # 自動從 active_trades.json 中移出
                active_trades = load_active_trades()
                keys_to_remove = [k for k in active_trades if active_trades[k]["symbol"] == symbol]
                for k in keys_to_remove:
                    del active_trades[k]
                save_active_trades(active_trades)
                
                msg = (
                    f"🔄 <b>[啟動自我校正] 發現 {symbol} 交易所持倉已結束！</b>\n"
                    f"────────────────────────────\n"
                    f"方向: {'📈 多 (LONG)' if is_long else '📉 空 (SHORT)'}\n"
                    f"判定原因: 交易所上已無該部位，自動結算為 {'🛑 觸發止損' if hit_sl_real else '✅ 交易所手動平倉/停利'}\n"
                    f"最後價格: {last_price:.4f if last_price else '未知'}\n"
                    f"估算盈虧: {pnl_with_lev:+.2f}%\n"
                    f"────────────────────────────\n"
                    f"狀態: 已自動校正為已結算，釋放持倉與監控名額。"
                )
                send_telegram(msg)
                
            else:
                # 交易所上有持倉，檢查是否有止損單掛載
                # 查詢該幣種的所有未完成 algo 訂單
                algo_orders_res = db_manager.send_binance_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()})
                has_sl_order = False
                if algo_orders_res["success"]:
                    orders = algo_orders_res.get("data")
                    if isinstance(orders, dict):
                        orders = orders.get("orders", [])
                    if isinstance(orders, list):
                        for order in orders:
                            o_type = order.get("orderType") or order.get("type")
                            if o_type == "STOP_MARKET" and order.get("algoType") == "CONDITIONAL":
                                has_sl_order = True
                                break
                            
                if not has_sl_order:
                    # 沒有止損單！立刻為其重新掛載一個實體止損單
                    log.info(f"  ⚠️ [Position Monitor] 發現交易所持倉 {symbol} (ID: {pos_id}) 缺少實體止損委託單！正在自動補掛...")
                    
                    # 獲取價格精度並格式化止損價
                    try:
                        _, price_prec = db_manager.get_binance_symbol_precision(symbol.upper())
                        sl_formatted = round(float(sl), price_prec)
                    except Exception:
                        sl_formatted = round(float(sl), 4)
                        
                    sl_order_side = "SELL" if is_long else "BUY"
                    sl_data = {
                        "algoType": "CONDITIONAL",
                        "symbol": symbol.upper(),
                        "side": sl_order_side,
                        "type": "STOP_MARKET",
                        "triggerPrice": sl_formatted,
                        "closePosition": "true",
                        "workingType": "MARK_PRICE"
                    }
                    sl_res = db_manager.send_binance_request("POST", "/fapi/v1/algoOrder", sl_data)
                    if sl_res["success"]:
                        log.info(f"  ✅ [Position Monitor] {symbol} 實體止損單補掛成功！價格: {sl_formatted:.4f}")
                        send_telegram(f"🔧 <b>[啟動自我校正] 已為 {symbol} 補掛裝飾好的實體止損單</b>\n止損價格: {sl_formatted:.4f}")
                    else:
                        log.error(f"  ❌ [Position Monitor] {symbol} 實體止損單補掛失敗: {sl_res.get('error')}")
                else:
                    log.info(f"  ✅ [Position Monitor] 交易所持倉 {symbol} 校正完畢：持倉正常且已正確掛載止損單。")
                    
        except Exception as err:
            log.error(f"  [Position Monitor] 校正持倉 {symbol} 發生錯誤: {err}")

