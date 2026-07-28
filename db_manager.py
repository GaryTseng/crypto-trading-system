import sqlite3
import time
import json
import hmac
import hashlib
import requests
import logging
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

from config import remove_active_trade_by_symbol_and_direction, BOT_TOKEN, CHAT_ID
DB_FILE = "trading_system.db"
log = logging.getLogger("scanner.db")

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
       
        # 1. 系統設定 Table (Key-Value)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
       
        # 2. 開單通知紀錄 Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                price REAL NOT NULL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                tp3 REAL NOT NULL,
                tp4 REAL NOT NULL,
                score INTEGER NOT NULL,
                count_active INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                expiry TEXT,
                tf TEXT
            )
        """)
        
        # 3. 派網開單託管持倉監控 Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,          -- 'BUY' (多) 或 'SELL' (空)
                entry_price REAL NOT NULL,
                current_price REAL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                tp3 REAL NOT NULL,
                tp4 REAL NOT NULL,
                current_tp_level INTEGER DEFAULT 0, -- 0: 未達, 1: TP1, 2: TP2, 3: TP3, 4: TP4
                size REAL NOT NULL,
                leverage INTEGER NOT NULL,
                margin REAL NOT NULL,
                pionex_order_id TEXT,
                tp1_order_id TEXT,
                status TEXT DEFAULT 'OPEN',   -- 'OPEN', 'CLOSED', 'SL_HIT', 'TP_HIT'
                timestamp TEXT NOT NULL,
                auto_close INTEGER DEFAULT 1,  -- 1: 啟用逾時自動平倉, 0: 關閉逾時自動平倉
                is_booster INTEGER DEFAULT 0,  -- 1: 啟動趨勢共振加碼, 0: 一般防禦倉位
                trailing_base_price REAL DEFAULT 0.0  -- TP4 後超額移動止損基準價
            )
        """)
        
        # 4. 歷史開單與信號分析 Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS historical_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_str TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,      -- 'long' 或 'short'
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL,
                tp3 REAL,
                tp4 REAL,
                rr REAL,                      -- 盈虧比
                tf TEXT,                      -- 週期
                leverage INTEGER,             -- 槓桿
                logic TEXT,                   -- 核心邏輯
                reach TEXT DEFAULT 'PENDING',  -- 最終狀態: 'PENDING', 'OPEN', 'SL', 'TP1', 'TP2', 'TP3', 'TP4', 'CLOSED', 'EXPIRED'
                note TEXT,                    -- 備註
                timeline TEXT,                -- 事件時間軸 (JSON string)
                trade_type TEXT NOT NULL,     -- 'SIGNAL' (開單建議) 或 'USER_TRADE' (用戶開單)
                pionex_order_id TEXT,
                margin REAL,
                pnl REAL,
                current_sl REAL,
                current_tp_level INTEGER DEFAULT 0
            )
        """)

        # 5. 資金費率套利交易 Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS arbitrage_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                amount_usdt REAL NOT NULL,
                qty REAL NOT NULL,
                spot_price REAL NOT NULL,
                futures_price REAL NOT NULL,
                status TEXT DEFAULT 'OPEN',     -- 'OPEN', 'CLOSED'
                open_time TEXT NOT NULL,
                close_time TEXT,
                total_funding_earned REAL DEFAULT 0.0,
                is_mock INTEGER DEFAULT 1       -- 1: 模擬, 0: 實盤
            )
        ''')
        
        # 數據庫遷移：檢查 notifications 是否有 tf 欄位，若無則 ALTER TABLE
        cursor.execute("PRAGMA table_info(notifications)")
        cols = [c[1] for c in cursor.fetchall()]
        if cols and "tf" not in cols:
            try:
                cursor.execute("ALTER TABLE notifications ADD COLUMN tf TEXT")
                log.info("Successfully migrated database: added 'tf' column to 'notifications' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'notifications' table: {e}")

        # 數據庫遷移：檢查 active_positions 是否有 auto_close 欄位，若無則 ALTER TABLE
        cursor.execute("PRAGMA table_info(active_positions)")
        cols_pos = [c[1] for c in cursor.fetchall()]
        if cols_pos and "auto_close" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN auto_close INTEGER DEFAULT 1")
                log.info("Successfully migrated database: added 'auto_close' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table: {e}")

        # 數據庫遷移：檢查 active_positions 是否有 tp1_order_id 欄位，若無則 ALTER TABLE
        if cols_pos and "tp1_order_id" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN tp1_order_id TEXT")
                log.info("Successfully migrated database: added 'tp1_order_id' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'tp1_order_id': {e}")

        # 數據庫遷移：檢查 active_positions 是否有 is_booster 欄位，若無則 ALTER TABLE
        if cols_pos and "is_booster" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN is_booster INTEGER DEFAULT 0")
                log.info("Successfully migrated database: added 'is_booster' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'is_booster': {e}")

        # 數據庫遷移：檢查 active_positions 是否有 trailing_base_price 欄位，若無則 ALTER TABLE
        if cols_pos and "trailing_base_price" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN trailing_base_price REAL DEFAULT 0.0")
                log.info("Successfully migrated database: added 'trailing_base_price' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'trailing_base_price': {e}")

        # 數據庫遷移：檢查 active_positions 是否有 accumulated_pnl 欄位，若無則 ALTER TABLE
        if cols_pos and "accumulated_pnl" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN accumulated_pnl REAL DEFAULT 0.0")
                log.info("Successfully migrated database: added 'accumulated_pnl' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'accumulated_pnl': {e}")

        # 數據庫遷移：檢查 active_positions 是否有 sl_algo_order_id 欄位，若無則 ALTER TABLE
        if cols_pos and "sl_algo_order_id" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN sl_algo_order_id TEXT")
                log.info("Successfully migrated database: added 'sl_algo_order_id' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'sl_algo_order_id': {e}")

        # 數據庫遷移：平倉時間，供「只看今天」同時涵蓋今日開倉與今日結算
        cursor.execute("PRAGMA table_info(active_positions)")
        cols_pos = [c[1] for c in cursor.fetchall()]
        if cols_pos and "closed_at" not in cols_pos:
            try:
                cursor.execute("ALTER TABLE active_positions ADD COLUMN closed_at TEXT")
                log.info("Successfully migrated database: added 'closed_at' column to 'active_positions' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'active_positions' table for 'closed_at': {e}")

        try:
            _backfill_active_positions_closed_at(cursor)
        except Exception as e:
            log.error(f"Failed to backfill active_positions.closed_at: {e}")

        # 數據庫遷移：檢查 historical_trades 是否有 margin 和 pnl 欄位，若無則 ALTER TABLE
        cursor.execute("PRAGMA table_info(historical_trades)")
        cols_hist = [c[1] for c in cursor.fetchall()]
        if cols_hist and "margin" not in cols_hist:
            try:
                cursor.execute("ALTER TABLE historical_trades ADD COLUMN margin REAL")
                log.info("Successfully migrated database: added 'margin' column to 'historical_trades' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'historical_trades' table for 'margin': {e}")
        if cols_hist and "pnl" not in cols_hist:
            try:
                cursor.execute("ALTER TABLE historical_trades ADD COLUMN pnl REAL")
                log.info("Successfully migrated database: added 'pnl' column to 'historical_trades' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'historical_trades' table for 'pnl': {e}")
        if cols_hist and "close_reason_code" not in cols_hist:
            try:
                cursor.execute("ALTER TABLE historical_trades ADD COLUMN close_reason_code TEXT")
                log.info("Successfully migrated database: added 'close_reason_code' column to 'historical_trades' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'historical_trades' table for 'close_reason_code': {e}")
        if cols_hist and "notify_sent" not in cols_hist:
            try:
                cursor.execute("ALTER TABLE historical_trades ADD COLUMN notify_sent INTEGER")
                log.info("Successfully migrated database: added 'notify_sent' column to 'historical_trades' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'historical_trades' table for 'notify_sent': {e}")
        if cols_hist and "notify_error" not in cols_hist:
            try:
                cursor.execute("ALTER TABLE historical_trades ADD COLUMN notify_error TEXT")
                log.info("Successfully migrated database: added 'notify_error' column to 'historical_trades' table.")
            except Exception as e:
                log.error(f"Failed to migrate 'historical_trades' table for 'notify_error': {e}")

        # 寫入預設設定
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pionex_api_key', '')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pionex_api_secret', '')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pionex_mock_mode', 'true')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('binance_api_key', '')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('binance_api_secret', '')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('binance_mock_mode', 'true')")
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('telegram_bot_token', ?)",
            (BOT_TOKEN or "",)
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('telegram_chat_id', ?)",
            (str(CHAT_ID) if CHAT_ID is not None else "",)
        )
        
        conn.commit()
    log.info("SQLite 數據庫初始化完畢。")

# ── 設定讀寫 ──
def get_setting(key, default=""):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
    except Exception as e:
        log.error(f"讀取設定 {key} 失敗: {e}")
        return default

def save_setting(key, value):
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
        return True
    except Exception as e:
        log.error(f"儲存設定 {key} 失敗: {e}")
        return False


def _now_taipei_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _estimate_closed_at_from_timeline(open_ts, timeline_list):
    """依開倉時間 + timeline 最後一筆相對時長估算平倉時間。"""
    if not open_ts:
        return None
    try:
        fmt = "%Y-%m-%d %H:%M:%S" if len(str(open_ts)) > 16 else "%Y-%m-%d %H:%M"
        open_dt = datetime.strptime(str(open_ts), fmt)
    except Exception:
        return None

    elapsed_h, elapsed_m = 0, 0
    if timeline_list:
        last = timeline_list[-1] if isinstance(timeline_list[-1], dict) else {}
        t = str(last.get("time") or "00:00").strip()
        if t and t.upper() != "AUTO":
            try:
                parts = t.split(":")
                elapsed_h = int(parts[0])
                elapsed_m = int(parts[1]) if len(parts) > 1 else 0
            except Exception:
                elapsed_h, elapsed_m = 0, 0
    closed_dt = open_dt + timedelta(hours=elapsed_h, minutes=elapsed_m)
    return closed_dt.strftime("%Y-%m-%d %H:%M:%S")


def _backfill_active_positions_closed_at(cursor):
    rows = cursor.execute(
        """
        SELECT id, timestamp, pionex_order_id, status
        FROM active_positions
        WHERE status IN ('CLOSED', 'SL_HIT', 'TP_HIT')
          AND (closed_at IS NULL OR closed_at = '')
        """
    ).fetchall()
    if not rows:
        return
    updated = 0
    for row in rows:
        pos_id = row[0]
        open_ts = row[1]
        order_id = row[2]
        timeline_list = []
        if order_id:
            h = cursor.execute(
                "SELECT timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'",
                (order_id,),
            ).fetchone()
            if h and h[0]:
                try:
                    timeline_list = json.loads(h[0])
                except Exception:
                    timeline_list = []
        closed_at = _estimate_closed_at_from_timeline(open_ts, timeline_list) or open_ts
        cursor.execute("UPDATE active_positions SET closed_at = ? WHERE id = ?", (closed_at, pos_id))
        updated += 1
    if updated:
        log.info(f"Backfilled closed_at for {updated} settled active_positions.")


# ── 通知紀錄 ──
def insert_notification(symbol, direction, price, sl, tps, score, count_active, expiry, tf="4h"):
    try:
        # 使用本地台北時間存入
        tz = timezone(timedelta(hours=8))
        time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
       
        # 確保 tps 長度符合
        tp_vals = [float(tps[i]) if i < len(tps) else 0.0 for i in range(4)]
       
        with get_db() as conn:
            conn.execute("""
                INSERT INTO notifications (symbol, direction, price, sl, tp1, tp2, tp3, tp4, score, count_active, timestamp, expiry, tf)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, direction, float(price), float(sl), tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], int(score), int(count_active), time_str, expiry, tf))
            conn.commit()
        return True
    except Exception as e:
        log.error(f"寫入通知紀錄失敗: {e}")
        return False

def get_notifications(page=1, limit=10):
    try:
        offset = (page - 1) * limit
        with get_db() as conn:
            rows = conn.execute("""
                SELECT n.*, h.reach as outcome, COALESCE(n.tf, h.tf) as final_tf, h.timeline FROM notifications n
                LEFT JOIN historical_trades h ON n.symbol = h.symbol AND n.timestamp = h.time_str AND h.trade_type = 'SIGNAL'
                ORDER BY n.timestamp DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
           
            total = conn.execute("SELECT COUNT(*) as count FROM notifications").fetchone()["count"]
           
            result = []
            for r in rows:
                timeline_list = []
                if r["timeline"]:
                    try:
                        timeline_list = json.loads(r["timeline"])
                    except Exception:
                        pass
                result.append({
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "price": r["price"],
                    "sl": r["sl"],
                    "tps": [r["tp1"], r["tp2"], r["tp3"], r["tp4"]],
                    "score": r["score"],
                    "count_active": r["count_active"],
                    "timestamp": r["timestamp"],
                    "expiry": r["expiry"],
                    "outcome": r["outcome"] if r["outcome"] is not None else "PENDING",
                    "tf": r["final_tf"] or "4h",
                    "timeline": timeline_list
                })
            return {"success": True, "data": result, "total": total, "page": page, "limit": limit}
    except Exception as e:
        log.error(f"讀取通知紀錄失敗: {e}")
        return {"success": False, "error": str(e)}

# ── 派網 API 連接與下單 ──
def pionex_signature(secret, method, path, params, body_data=None):
    # 對 query 進行 ASCII 排序
    query_str = urlencode(sorted(params.items()))
    path_with_query = f"{path}?{query_str}"
   
    message = f"{method.upper()}{path_with_query}"
    if method.upper() in ["POST", "DELETE"] and body_data:
        message += json.dumps(body_data)
       
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def send_pionex_request(method, path, body_data=None):
    api_key = get_setting("pionex_api_key")
    api_secret = get_setting("pionex_api_secret")
   
    if not api_key or not api_secret:
        return {"success": False, "error": "尚未配置派網 API Key 或 Secret，請至設定頁面設定"}
       
    base_url = "https://api.pionex.com"
    params = {"timestamp": int(time.time() * 1000)}
   
    try:
        sig = pionex_signature(api_secret, method, path, params, body_data)
        headers = {
            "PIONEX-KEY": api_key,
            "PIONEX-SIGNATURE": sig,
            "Content-Type": "application/json"
        }
       
        url = f"{base_url}{path}?{urlencode(params)}"
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        else:
            r = requests.post(url, headers=headers, json=body_data, timeout=10)
           
        res_json = r.json()
        if r.ok and res_json.get("result", False):
            return {"success": True, "data": res_json.get("data", {})}
        else:
            return {"success": False, "error": res_json.get("message", "API 請求失敗")}
    except Exception as e:
        log.error(f"派網 API 請求異常: {e}")
        return {"success": False, "error": f"網路請求異常: {str(e)}"}

# ── 託管開單邏輯 ──
def place_pionex_futures_order(symbol, direction, leverage, margin, sl_price, tps, is_market=True, limit_price=None):
    try:
        mock_mode = get_setting("pionex_mock_mode", "true") == "true"
       
        # 派網永續合約代幣格式: BTC_USDT_PERP
        clean_sym = symbol.upper().replace("USDT", "")
        pionex_symbol = f"{clean_sym}_USDT_PERP"
       
        # 多單為 BUY, 空單為 SELL
        side = "BUY" if direction.lower() == "long" else "SELL"
       
        # 入場價格
        ticker_price = limit_price
        if not ticker_price:
            # 獲取即時價格
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}")
                ticker_price = float(r.json()["price"])
            except:
                ticker_price = 100.0  # 備用安全價格
               
        # 計算名義價值與開倉數量
        # size = (margin * leverage) / entry_price
        entry_price = float(ticker_price) if is_market else float(limit_price)
        size = round((float(margin) * int(leverage)) / entry_price, 4)
       
        order_id = f"MOCK_{int(time.time())}"
       
        if not mock_mode:
            # 1. 設定槓桿
            # UAPI 設定槓桿
            # POST /uapi/v1/account/leverage
            lev_res = send_pionex_request("POST", "/uapi/v1/account/leverage", {
                "symbol": pionex_symbol,
                "leverage": str(leverage)
            })
            if not lev_res["success"]:
                log.warning(f"設定槓桿失敗 (可能合約不支援此槓桿或已是此設定): {lev_res.get('error')}")
           
            # 2. 送出合約開單
            order_data = {
                "symbol": pionex_symbol,
                "side": side,
                "type": "MARKET" if is_market else "LIMIT",
                "size": str(size)
            }
            if not is_market:
                order_data["price"] = str(round(entry_price, 4))
               
            order_res = send_pionex_request("POST", "/uapi/v1/trade/order", order_data)
            if not order_res["success"]:
                return {"success": False, "error": f"開單失敗: {order_res.get('error')}"}
               
            order_id = order_res["data"].get("orderId", order_id)
           
        # 寫入 active_positions 進行本地追蹤與 trailing stop
        tz = timezone(timedelta(hours=8))
        time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
       
        tp_vals = [float(tps[i]) if i < len(tps) else 0.0 for i in range(4)]
       
        with get_db() as conn:
            conn.execute("""
                INSERT INTO active_positions (symbol, side, entry_price, current_price, sl, tp1, tp2, tp3, tp4, current_tp_level, size, leverage, margin, pionex_order_id, status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'OPEN', ?)
            """, (symbol.upper(), side, entry_price, entry_price, float(sl_price), tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], size, int(leverage), float(margin), str(order_id), time_str))
           
            # 同步寫入歷史分析表 (類別為 USER_TRADE，狀態為 OPEN)
            direction_str = "long" if side == "BUY" else "short"
            logic_str = "用戶市價合約開倉" if is_market else "用戶限價合約開倉"
           
            # 建立初始時間軸
            timeline_list = [{"time": "00:00", "event": f"用戶執行開倉，方向: {direction_str.upper()}，槓桿: {leverage}x，保證金: {margin} USDT，數量: {size}"}]
            timeline_json = json.dumps(timeline_list, ensure_ascii=False)
           
            # 計算盈虧比
            rr = 0.0
            try:
                valid_tps = [t for t in tp_vals if t is not None and t > 0]
                if valid_tps and entry_price and sl_price and abs(entry_price - float(sl_price)) > 0:
                    rr = round(abs(valid_tps[-1] - entry_price) / abs(entry_price - float(sl_price)), 2)
            except Exception as err:
                log.warning(f"計算開單盈虧比失敗: {err}")
               
            conn.execute("""
                INSERT INTO historical_trades (
                    time_str, symbol, direction, entry, sl, tp1, tp2, tp3, tp4, rr, tf, leverage, logic, reach, note, timeline, trade_type, pionex_order_id, current_sl, current_tp_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '4H + 1D', ?, ?, 'OPEN', '', ?, 'USER_TRADE', ?, ?, 0)
            """, (
                time_str, symbol.upper(), direction_str, entry_price, float(sl_price),
                tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], rr,
                int(leverage), logic_str, timeline_json, str(order_id), float(sl_price)
            ))
           
            conn.commit()
           
        mode_text = "【模擬託管】" if mock_mode else "【實盤託管】"
        type_text = "市價" if is_market else "限價"
        return {
            "success": True,
            "message": f"{mode_text}成功建立 {symbol} {side} {type_text}單。開倉價: {entry_price:.4f}，槓桿: {leverage}x，數量: {size}",
            "order_id": order_id
        }
    except Exception as e:
        log.error(f"託管開單異常: {e}")
        return {"success": False, "error": f"開單處理失敗: {str(e)}"}

# ── 託管平倉邏輯 ──
def close_pionex_position(position_id):
    try:
        mock_mode = get_setting("pionex_mock_mode", "true") == "true"
       
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
               
            if pos["status"] != "OPEN":
                return {"success": False, "error": f"此持倉狀態為 {pos['status']}，無法重複平倉"}
               
            symbol = pos["symbol"]
            side = pos["side"]
            size = pos["size"]
           
            # 平倉為反向
            close_side = "SELL" if side == "BUY" else "BUY"
           
            if not mock_mode:
                clean_sym = symbol.upper().replace("USDT", "")
                pionex_symbol = f"{clean_sym}_USDT_PERP"
               
                # 發送市價單平倉
                order_data = {
                    "symbol": pionex_symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "size": str(size)
                }
                close_res = send_pionex_request("POST", "/uapi/v1/trade/order", order_data)
                if not close_res["success"]:
                    return {"success": False, "error": f"派網平倉失敗: {close_res.get('error')}"}
           
            # 更新數據庫狀態
            conn.execute("UPDATE active_positions SET status = 'CLOSED', closed_at = COALESCE(closed_at, ?) WHERE id = ?", (_now_taipei_str(), position_id))
            remove_active_trade_by_symbol_and_direction(pos["symbol"], "long" if pos["side"] == "BUY" else "short")
           
            # 同步更新歷史分析表 (將 reach 設為 CLOSED，並記錄平倉時間軸事件)
            pionex_order_id = pos["pionex_order_id"]
            if pionex_order_id:
                hist_row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pionex_order_id,)).fetchone()
                if hist_row:
                    t_id = hist_row["id"]
                    start_time_str = hist_row["time_str"]
                    timeline_str = hist_row["timeline"]
                   
                    timeline_list = []
                    if timeline_str:
                        try:
                            timeline_list = json.loads(timeline_str)
                        except:
                            pass
                           
                    # 計算相對時間
                    elapsed_str = "00:00"
                    try:
                        tz_taipei = timezone(timedelta(hours=8))
                        fmt = "%Y-%m-%d %H:%M:%S" if len(start_time_str) > 16 else "%Y-%m-%d %H:%M"
                        start_dt = datetime.strptime(start_time_str, fmt)
                        start_dt_aware = start_dt.replace(tzinfo=tz_taipei)
                        now_dt_aware = datetime.now(tz_taipei)
                        diff = now_dt_aware - start_dt_aware
                        total_seconds = int(diff.total_seconds())
                        if total_seconds < 0:
                            total_seconds = 0
                        hours = total_seconds // 3600
                        minutes = (total_seconds % 3600) // 60
                        elapsed_str = f"{hours:02d}:{minutes:02d}"
                    except Exception as te:
                        log.warning(f"計算相對時間失敗: {te}")
                       
                    timeline_list.append({"time": elapsed_str, "event": "用戶手動市價平倉離場"})
                   
                    conn.execute("""
                        UPDATE historical_trades
                        SET reach = 'CLOSED', timeline = ?
                        WHERE id = ?
                    """, (json.dumps(timeline_list, ensure_ascii=False), t_id))
           
            conn.commit()
           
        mode_text = "【模擬託管】" if mock_mode else "【實盤託管】"
        return {"success": True, "message": f"{mode_text} {symbol} 持倉已成功市價平倉！"}
    except Exception as e:
        log.error(f"手動平倉異常: {e}")
        return {"success": False, "error": f"平倉操作失敗: {str(e)}"}

# ── 幣安 API 連接與下單精度處理 ──
def get_binance_symbol_precision(symbol):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5)
        if r.ok:
            info = r.json()
            for sym_info in info.get("symbols", []):
                if sym_info["symbol"] == symbol.upper():
                    quantity_precision = int(sym_info["quantityPrecision"])
                    price_precision = int(sym_info["pricePrecision"])
                    return quantity_precision, price_precision
    except Exception as e:
        log.error(f"獲取幣安精度失敗: {e}")
    return 3, 4

def binance_signature(secret: str, query_string: str) -> str:
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def send_binance_request(method, path, params=None):
    api_key = get_setting("binance_api_key")
    api_secret = get_setting("binance_api_secret")
   
    if not api_key or not api_secret:
        return {"success": False, "error": "尚未配置幣安 API Key 或 Secret，請至設定頁面設定"}
       
    base_url = "https://fapi.binance.com"
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
   
    query_str = urlencode(params)
    sig = binance_signature(api_secret, query_str)
    query_str += f"&signature={sig}"
   
    url = f"{base_url}{path}?{query_str}"
    headers = {
        "X-MBX-APIKEY": api_key,
        "Content-Type": "application/json"
    }
   
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            r = requests.post(url, headers=headers, timeout=10)
        elif method.upper() == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
        else:
            return {"success": False, "error": "不支援的 HTTP 方法"}
           
        try:
            res_json = r.json()
        except Exception as json_err:
            log.error(f"幣安 API 回傳非 JSON 格式 ({r.status_code}): {r.text[:200]}")
            return {"success": False, "error": f"幣安 API 回傳非 JSON 格式 (HTTP {r.status_code})"}
            
        if r.status_code == 200:
            return {"success": True, "data": res_json}
        else:
            err_msg = res_json.get("msg", "API 請求失敗")
            return {"success": False, "error": f"Binance API 錯誤 ({r.status_code}): {err_msg}"}
    except Exception as e:
        log.error(f"幣安 API 請求異常: {e}")
        return {"success": False, "error": f"網路請求異常: {str(e)}"}

def place_binance_futures_order(symbol, direction, leverage, margin, sl_price, tps, is_market=True, limit_price=None, is_booster=0):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        clean_sym = symbol.upper()
        side = "BUY" if direction.lower() == "long" else "SELL"
        tp1_order_id_val = None
        sl_algo_order_id_val = None
       
        ticker_price = limit_price
        if not ticker_price:
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={clean_sym}", timeout=5)
                ticker_price = float(r.json()["price"])
            except Exception as e:
                return {"success": False, "error": f"無法取得 {clean_sym} 即時價格，已中止開單: {e}"}
               
        qty_prec, price_prec = get_binance_symbol_precision(clean_sym)
        entry_price = float(ticker_price) if is_market else float(limit_price)
        size = round((float(margin) * int(leverage)) / entry_price, qty_prec)
       
        entry_price = round(entry_price, price_prec)
        sl_price = round(float(sl_price), price_prec)
        tps = [round(float(tp), price_prec) if tp else 0.0 for tp in tps]
       
        order_id = f"MOCK_{int(time.time())}"
       
        if not mock_mode:
            # 1. 調整槓桿
            lev_res = send_binance_request("POST", "/fapi/v1/leverage", {
                "symbol": clean_sym,
                "leverage": int(leverage)
            })
            if not lev_res["success"]:
                log.warning(f"調整幣安槓桿失敗: {lev_res.get('error')}")
           
            # 2. 發送開倉單
            order_data = {
                "symbol": clean_sym,
                "side": side,
                "type": "MARKET" if is_market else "LIMIT",
                "quantity": size
            }
            if not is_market:
                order_data["price"] = entry_price
                order_data["timeInForce"] = "GTC"
               
            order_res = send_binance_request("POST", "/fapi/v1/order", order_data)
            if not order_res["success"]:
                return {"success": False, "error": f"幣安開單失敗: {order_res.get('error')}"}
                
            order_id = order_res["data"].get("orderId", order_id)
            
            # 實盤開單成功後，如果是市價開倉，立刻在幣安上掛一個實體 STOP_MARKET 止損委託單
            if is_market:
                sl_side = "SELL" if side == "BUY" else "BUY"
                sl_data = {
                    "algoType": "CONDITIONAL",
                    "symbol": clean_sym,
                    "side": sl_side,
                    "type": "STOP_MARKET",
                    "triggerPrice": sl_price,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE"
                }
                sl_res = send_binance_request("POST", "/fapi/v1/algoOrder", sl_data)
                if not sl_res["success"]:
                    log.warning(f"幣安設置實體止損委託單失敗: {sl_res.get('error')}")
                else:
                    # 儲存 SL algo 訂單 ID，後續平倉時只撤除自己的止損單而不全清
                    sl_algo_order_id_val = str(sl_res["data"].get("algoId") or sl_res["data"].get("orderId", ""))
                    log.info(f"[Auto Trade] SL Algo 訂單已挂載，止損 ID: {sl_algo_order_id_val}")
                
                # ── 新增：限價掛載 TP1 減倉單 ──
                if len(tps) > 0 and tps[0] > 0:
                    tp1_ratio = 0.25 if is_booster == 1 else 0.50
                    tp1_size = round(size * tp1_ratio, qty_prec)
                    if tp1_size > 0:
                        tp1_price = round(tps[0], price_prec)
                        tp1_data = {
                            "symbol": clean_sym,
                            "side": sl_side,
                            "type": "LIMIT",
                            "price": tp1_price,
                            "quantity": tp1_size,
                            "timeInForce": "GTC",
                            "reduceOnly": "true"
                        }
                        tp1_res = send_binance_request("POST", "/fapi/v1/order", tp1_data)
                        if tp1_res["success"]:
                            tp1_order_id_val = str(tp1_res["data"].get("orderId"))
                            log.info(f"✅ [Auto Trade] 幣安實體限價 TP1 減倉單掛載成功！價格: {tp1_price}, 數量: {tp1_size}, 訂單 ID: {tp1_order_id_val}")
                        else:
                            log.warning(f"❌ [Auto Trade] 幣安實體限價 TP1 減倉單掛載失敗: {tp1_res.get('error')}")

        tz = timezone(timedelta(hours=8))
        time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        tp_vals = [float(tps[i]) if i < len(tps) else 0.0 for i in range(4)]
       
        pos_status = "OPEN" if is_market else "PENDING_ORDER"
        with get_db() as conn:
            conn.execute("""
                INSERT INTO active_positions (symbol, side, entry_price, current_price, sl, tp1, tp2, tp3, tp4, current_tp_level, size, leverage, margin, pionex_order_id, tp1_order_id, sl_algo_order_id, status, timestamp, is_booster)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (clean_sym, side, entry_price, entry_price, float(sl_price), tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], size, int(leverage), float(margin), str(order_id), tp1_order_id_val, sl_algo_order_id_val, pos_status, time_str, int(is_booster)))
           
            direction_str = "long" if side == "BUY" else "short"
            logic_str = "用戶幣安市價開倉" if is_market else "用戶幣安限價開倉"
            timeline_list = [{"time": "00:00", "event": f"用戶執行開倉，方向: {direction_str.upper()}，槓桿: {leverage}x，保證金: {margin} USDT，數量: {size}"}]
            timeline_json = json.dumps(timeline_list, ensure_ascii=False)
           
            rr = 0.0
            try:
                valid_tps = [t for t in tp_vals if t is not None and t > 0]
                if valid_tps and entry_price and sl_price and abs(entry_price - float(sl_price)) > 0:
                    rr = round(abs(valid_tps[-1] - entry_price) / abs(entry_price - float(sl_price)), 2)
            except Exception as err:
                log.warning(f"計算開單盈虧比失敗: {err}")
               
            conn.execute("""
                INSERT INTO historical_trades (
                    time_str, symbol, direction, entry, sl, tp1, tp2, tp3, tp4, rr, tf, leverage, logic, reach, note, timeline, trade_type, pionex_order_id, current_sl, current_tp_level, margin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '4H + 1D', ?, ?, 'OPEN', '', ?, 'USER_TRADE', ?, ?, 0, ?)
            """, (
                time_str, clean_sym, direction_str, entry_price, float(sl_price),
                tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], rr,
                int(leverage), logic_str, timeline_json, str(order_id), float(sl_price), float(margin)
            ))
            conn.commit()
           
        mode_text = "【幣安模擬】" if mock_mode else "【幣安實盤】"
        type_text = "市價" if is_market else "限價"
        return {
            "success": True,
            "message": f"{mode_text}成功建立 {clean_sym} {side} {type_text}單。開倉價: {entry_price:.4f}，槓桿: {leverage}x，數量: {size}",
            "order_id": order_id
        }
    except Exception as e:
        log.error(f"幣安開單異常: {e}")
        return {"success": False, "error": f"開單處理失敗: {str(e)}"}

def close_binance_position(position_id, event_txt="用戶手動市價平倉離場", reach_status="CLOSED", pnl_percent=None, close_reason_code=None):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
       
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
               
            if pos["status"] != "OPEN":
                return {"success": False, "error": f"此持倉狀態為 {pos['status']}，無法重複平倉"}
               
            symbol = pos["symbol"]
            side = pos["side"]
            size = pos["size"]
            close_side = "SELL" if side == "BUY" else "BUY"
            pionex_order_id = pos["pionex_order_id"]
           
            if not mock_mode:
                order_data = {
                    "symbol": symbol.upper(),
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": size,
                    "reduceOnly": "true"
                }
                close_res = send_binance_request("POST", "/fapi/v1/order", order_data)
                if not close_res["success"]:
                    return {"success": False, "error": f"幣安平倉失敗: {close_res.get('error')}"}
                
                # 平倉後，只撤除「機器人自己的」 TP1 和 SL Algo 挂單，禁止全清防止影響手動倉
                tp1_order_id_to_cancel = pos.get("tp1_order_id")
                sl_algo_order_id_to_cancel = pos.get("sl_algo_order_id")
                if tp1_order_id_to_cancel:
                    try:
                        send_binance_request("DELETE", "/fapi/v1/order", {"symbol": symbol.upper(), "orderId": int(tp1_order_id_to_cancel)})
                        log.info(f"[close_position] 撤除機器人 TP1 挂單 (OrderID: {tp1_order_id_to_cancel})")
                    except Exception as cancel_err:
                        log.warning(f"[close_position] 撤除 TP1 挂單失敗: {cancel_err}")
                if sl_algo_order_id_to_cancel:
                    try:
                        send_binance_request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol.upper(), "algoId": int(sl_algo_order_id_to_cancel)})
                        log.info(f"[close_position] 撤除機器人 SL Algo 挂單 (AlgoID: {sl_algo_order_id_to_cancel})")
                    except Exception as cancel_err:
                        log.warning(f"[close_position] 撤除 SL Algo 挂單失敗: {cancel_err}")
                
                # 如果沒有對應 ID（舊倉位早期開的），才退化為全清保播，並在 log 層發警告
                if not tp1_order_id_to_cancel and not sl_algo_order_id_to_cancel:
                    log.warning(f"[close_position] {symbol} 倉位無儲存機器人訂單 ID，退化為全清模式（可能影響手動倉）。")
                    send_binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
                    send_binance_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol.upper()})
           
            conn.execute("UPDATE active_positions SET status = 'CLOSED', closed_at = COALESCE(closed_at, ?) WHERE id = ?", (_now_taipei_str(), position_id))
           
            if event_txt:
                if pionex_order_id:
                    hist_row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pionex_order_id,)).fetchone()
                    if hist_row:
                        t_id = hist_row["id"]
                        start_time_str = hist_row["time_str"]
                        timeline_str = hist_row["timeline"]
                       
                        timeline_list = []
                        if timeline_str:
                            try:
                                timeline_list = json.loads(timeline_str)
                            except:
                                pass
                               
                        elapsed_str = "00:00"
                        try:
                            tz_taipei = timezone(timedelta(hours=8))
                            fmt = "%Y-%m-%d %H:%M:%S" if len(start_time_str) > 16 else "%Y-%m-%d %H:%M"
                            start_dt = datetime.strptime(start_time_str, fmt)
                            start_dt_aware = start_dt.replace(tzinfo=tz_taipei)
                            now_dt_aware = datetime.now(tz_taipei)
                            diff = now_dt_aware - start_dt_aware
                            total_seconds = int(diff.total_seconds())
                            if total_seconds < 0:
                                total_seconds = 0
                            hours = total_seconds // 3600
                            minutes = (total_seconds % 3600) // 60
                            elapsed_str = f"{hours:02d}:{minutes:02d}"
                        except Exception as te:
                            log.warning(f"計算相對時間失敗: {te}")
                           
                        timeline_list.append({"time": elapsed_str, "event": event_txt})
                        margin_val = pos["margin"]
                        pnl_val = None
                        if margin_val is not None:
                            if pnl_percent is not None:
                                pnl_val = round(float(margin_val) * (float(pnl_percent) / 100.0), 4)
                            else:
                                try:
                                    r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}", timeout=5)
                                    if r.ok:
                                        data = r.json()
                                        last_p = float(data.get("price", 0))
                                        entry_p = float(pos["entry_price"])
                                        is_long = (pos["side"] == "BUY")
                                        raw_pnl = ((last_p - entry_p) / entry_p) if is_long else ((entry_p - last_p) / entry_p)
                                        pnl_val = round(float(margin_val) * raw_pnl * float(pos["leverage"]), 4)
                                except Exception as e:
                                    log.error(f"計算手動平倉 PnL 異常: {e}")
                                    pnl_val = 0.0
                        reason_code = close_reason_code if close_reason_code else str(reach_status)
                        conn.execute("""
                            UPDATE historical_trades
                            SET reach = ?, timeline = ?, pnl = ?, close_reason_code = ?
                            WHERE id = ?
                        """, (reach_status, json.dumps(timeline_list, ensure_ascii=False), pnl_val, reason_code, t_id))

                        # 同步關聯的 SIGNAL / AI_SENTIMENT_SIGNAL 狀態，避免歷史仍卡在 PENDING
                        sig_sync_event = f"關聯 USER_TRADE 已{reach_status}，同步更新策略信號狀態。"
                        sync_linked_signal_status_from_user_trade(
                            symbol=symbol,
                            direction="long" if side == "BUY" else "short",
                            user_trade_id=t_id,
                            reach_status=reach_status,
                            event_text=sig_sync_event
                        )
            conn.commit()
            if pionex_order_id:
                reconcile_trade_pnl(pionex_order_id, fallback_pnl=pnl_val)

            # 平倉完成後，確保主/AI 監控佇列同步移除，避免前端仍顯示監控中
            remove_active_trade_by_symbol_and_direction(symbol, "long" if pos["side"] == "BUY" else "short")
           
        mode_text = "【幣安模擬】" if mock_mode else "【幣安實盤】"
        return {"success": True, "message": f"{mode_text} {symbol} 持倉已成功市價平倉！"}
    except Exception as e:
        log.error(f"手動平倉異常: {e}")
        return {"success": False, "error": f"平倉操作失敗: {str(e)}"}

def cancel_binance_limit_order(position_id, event_txt="用戶手動撤銷限價委託單", reach_status="CLOSED"):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
       
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
               
            if pos["status"] != "PENDING_ORDER":
                return {"success": False, "error": f"此委託狀態為 {pos['status']}，無法執行撤單"}
               
            symbol = pos["symbol"]
            order_id = pos["pionex_order_id"]
           
            if not mock_mode:
                if order_id and not str(order_id).startswith("MOCK_"):
                    cancel_order_res = send_binance_request("DELETE", "/fapi/v1/order", {
                        "symbol": symbol.upper(),
                        "orderId": int(order_id)
                    })
                    if not cancel_order_res["success"]:
                        err_msg = cancel_order_res.get("error", "")
                        log.warning(f"撤銷限價單失敗 {symbol} (OrderID: {order_id}): {err_msg}")
                
                send_binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
                send_binance_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol.upper()})
           
            conn.execute("UPDATE active_positions SET status = 'CLOSED', closed_at = COALESCE(closed_at, ?) WHERE id = ?", (_now_taipei_str(), position_id))
           
            if event_txt:
                if order_id:
                    hist_row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (str(order_id),)).fetchone()
                    if hist_row:
                        t_id = hist_row["id"]
                        start_time_str = hist_row["time_str"]
                        timeline_str = hist_row["timeline"]
                       
                        timeline_list = []
                        if timeline_str:
                            try:
                                timeline_list = json.loads(timeline_str)
                            except:
                                pass
                               
                        elapsed_str = "00:00"
                        try:
                            tz_taipei = timezone(timedelta(hours=8))
                            fmt = "%Y-%m-%d %H:%M:%S" if len(start_time_str) > 16 else "%Y-%m-%d %H:%M"
                            start_dt = datetime.strptime(start_time_str, fmt)
                            start_dt_aware = start_dt.replace(tzinfo=tz_taipei)
                            now_dt_aware = datetime.now(tz_taipei)
                            diff = now_dt_aware - start_dt_aware
                            total_seconds = int(diff.total_seconds())
                            if total_seconds < 0:
                                total_seconds = 0
                            hours = total_seconds // 3600
                            minutes = (total_seconds % 3600) // 60
                            elapsed_str = f"{hours:02d}:{minutes:02d}"
                        except Exception as te:
                            log.warning(f"計算相對時間失敗: {te}")
                           
                        timeline_list.append({"time": elapsed_str, "event": event_txt})
                        conn.execute("""
                            UPDATE historical_trades
                            SET reach = ?, timeline = ?
                            WHERE id = ?
                        """, (reach_status, json.dumps(timeline_list, ensure_ascii=False), t_id))

                        sync_linked_signal_status_from_user_trade(
                            symbol=symbol,
                            direction="long" if pos["side"] == "BUY" else "short",
                            user_trade_id=t_id,
                            reach_status=reach_status,
                            event_text=f"關聯 USER_TRADE 已{reach_status}（撤單/失效），同步更新策略信號狀態。"
                        )
            conn.commit()

            # 撤單結束監控，避免殘留在 active_trades/active_trades_ai_sentiment
            remove_active_trade_by_symbol_and_direction(symbol, "long" if pos["side"] == "BUY" else "short")
           
        mode_text = "【幣安模擬】" if mock_mode else "【幣安實盤】"
        return {"success": True, "message": f"{mode_text} {symbol} 限價委託單已成功撤銷！"}
    except Exception as e:
        log.error(f"撤銷限價單異常: {e}")
        return {"success": False, "error": f"撤銷限價單操作失敗: {str(e)}"}

def partially_close_binance_position(position_id, ratio=0.25, event_txt="部分減倉", reach_status="PARTIAL"):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
       
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
               
            if pos["status"] != "OPEN":
                return {"success": False, "error": f"此持倉狀態為 {pos['status']}，無法操作部分減倉"}
               
            symbol = pos["symbol"]
            side = pos["side"]
            size = pos["size"]
            margin = pos["margin"]
            close_side = "SELL" if side == "BUY" else "BUY"
            
            qty_prec, price_prec = get_binance_symbol_precision(symbol.upper())
            
            # 計算要減倉的數量
            close_size = round(size * ratio, qty_prec)
            if close_size <= 0:
                # 精度不夠，至少為最小精度單位
                close_size = round(10 ** (-qty_prec), qty_prec)
                
            if close_size >= size:
                # 如果減倉數量大於等於總數量，直接全平
                return close_binance_position(position_id, event_txt=event_txt, reach_status="CLOSED")
                
            # 剩餘數量與保證金
            new_size = round(size - close_size, qty_prec)
            new_margin = margin * (new_size / size)
           
            if not mock_mode:
                order_data = {
                    "symbol": symbol.upper(),
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": close_size,
                    "reduceOnly": "true"
                }
                close_res = send_binance_request("POST", "/fapi/v1/order", order_data)
                if not close_res["success"]:
                    return {"success": False, "error": f"幣安部分減倉失敗: {close_res.get('error')}"}
           
            # 計算部分平倉盈虧並累加到 accumulated_pnl
            entry_p = float(pos["entry_price"])
            last_p = float(pos["current_price"]) if pos["current_price"] else entry_p
            try:
                r_px = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}", timeout=3)
                if r_px.ok:
                    last_p = float(r_px.json().get("price", last_p))
            except Exception:
                pass

            is_long = (side == "BUY")
            raw_diff = (last_p - entry_p) if is_long else (entry_p - last_p)
            partial_pnl = round(close_size * raw_diff, 4)

            curr_acc_pnl = pos["accumulated_pnl"] if "accumulated_pnl" in pos.keys() and pos["accumulated_pnl"] is not None else 0.0
            new_acc_pnl = round(curr_acc_pnl + partial_pnl, 4)

            # 更新資料庫中的持倉大小、保證金與累計已實現盈虧
            conn.execute("""
                UPDATE active_positions 
                SET size = ?, margin = ?, accumulated_pnl = ? 
                WHERE id = ?
            """, (new_size, new_margin, new_acc_pnl, position_id))
           
            if event_txt:
                pionex_order_id = pos["pionex_order_id"]
                if pionex_order_id:
                    hist_row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pionex_order_id,)).fetchone()
                    if hist_row:
                        t_id = hist_row["id"]
                        start_time_str = hist_row["time_str"]
                        timeline_str = hist_row["timeline"]
                       
                        timeline_list = []
                        if timeline_str:
                            try:
                                timeline_list = json.loads(timeline_str)
                            except:
                                pass
                               
                        elapsed_str = "00:00"
                        try:
                            tz_taipei = timezone(timedelta(hours=8))
                            fmt = "%Y-%m-%d %H:%M:%S" if len(start_time_str) > 16 else "%Y-%m-%d %H:%M"
                            start_dt = datetime.strptime(start_time_str, fmt)
                            start_dt_aware = start_dt.replace(tzinfo=tz_taipei)
                            now_dt_aware = datetime.now(tz_taipei)
                            diff = now_dt_aware - start_dt_aware
                            total_seconds = int(diff.total_seconds())
                            if total_seconds < 0:
                                total_seconds = 0
                            hours = total_seconds // 3600
                            minutes = (total_seconds % 3600) // 60
                            elapsed_str = f"{hours:02d}:{minutes:02d}"
                        except Exception as te:
                            log.warning(f"計算相對時間失敗: {te}")
                           
                        timeline_list.append({"time": elapsed_str, "event": event_txt})
                        conn.execute("""
                            UPDATE historical_trades
                            SET timeline = ?, pnl = ?
                            WHERE id = ?
                        """, (json.dumps(timeline_list, ensure_ascii=False), new_acc_pnl, t_id))
            conn.commit()
           
        mode_text = "【幣安模擬】" if mock_mode else "【幣安實盤】"
        return {"success": True, "message": f"{mode_text} {symbol} 已成功部分減倉 {ratio*100}%，減倉數量: {close_size}，剩餘數量: {new_size}。"}
    except Exception as e:
        log.error(f"部分平倉異常: {e}")
        return {"success": False, "error": f"部分平倉操作失敗: {str(e)}"}


# ── 查詢當前託管持倉 ──
def get_active_positions(page=None, limit=None, view_filter="all"):
    try:
        view_filter = (view_filter or "all").lower().strip()
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

        where_sql = "1=1"
        where_params = []
        if view_filter == "open":
            where_sql = "status IN ('OPEN', 'PENDING_ORDER')"
        elif view_filter == "today":
            # 今日開倉 或 今日結算（含跨日持倉今天才平倉的單）
            where_sql = "(substr(timestamp, 1, 10) = ? OR (closed_at IS NOT NULL AND substr(closed_at, 1, 10) = ?))"
            where_params = [today, today]
        elif view_filter == "anomaly":
            # 異常多發生在仍活躍倉；細節再由前端/API 強化欄位過濾
            where_sql = "status IN ('OPEN', 'PENDING_ORDER')"
        # view_filter == "all" → no extra where

        with get_db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) as count FROM active_positions WHERE {where_sql}",
                where_params,
            ).fetchone()["count"]
            
            if page is not None and limit is not None:
                offset = (page - 1) * limit
                rows = conn.execute(
                    f"SELECT * FROM active_positions WHERE {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (*where_params, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM active_positions WHERE {where_sql} ORDER BY timestamp DESC",
                    where_params,
                ).fetchall()
                
            result = []
            for r in rows:
                auto_close_val = r["auto_close"] if "auto_close" in r.keys() else 1
                
                # 獲取關聯的歷史交易事件時間軸
                timeline_list = []
                p_order_id = r["pionex_order_id"]
                if p_order_id:
                    try:
                        h_row = conn.execute("SELECT timeline FROM historical_trades WHERE pionex_order_id = ?", (p_order_id,)).fetchone()
                        if h_row and h_row["timeline"]:
                            timeline_list = json.loads(h_row["timeline"])
                    except Exception:
                        pass
                        
                # 實時對接幣安成交明細與歷史資料庫計算已實現盈虧
                realized_pnl = 0.0
                acc_pnl = r["accumulated_pnl"] if "accumulated_pnl" in r.keys() and r["accumulated_pnl"] is not None else 0.0
                status_val = r["status"]
                closed_at_val = r["closed_at"] if "closed_at" in r.keys() else None
                
                # 查詢策略來源 (AI 智能情緒 vs 標準指標) 及歷史結算盈虧
                strategy_type = "STANDARD"
                trade_logic = ""
                hist_pnl = None
                if p_order_id:
                    try:
                        h_sig = conn.execute("""
                            SELECT trade_type, logic, pnl 
                            FROM historical_trades 
                            WHERE (pionex_order_id = ? OR (symbol = ? AND id <= (SELECT id FROM historical_trades WHERE pionex_order_id = ? LIMIT 1)))
                            ORDER BY id DESC LIMIT 2
                        """, (p_order_id, r["symbol"], p_order_id)).fetchall()
                        for hs in h_sig:
                            tt = str(hs["trade_type"] or "").upper()
                            lg = str(hs["logic"] or "")
                            if hs["pnl"] is not None and hist_pnl is None:
                                hist_pnl = float(hs["pnl"])
                            if "AI" in tt or "AI" in lg:
                                strategy_type = "AI_SENTIMENT"
                                trade_logic = lg
                                break
                            elif lg and not trade_logic:
                                trade_logic = lg
                    except Exception:
                        pass

                if hist_pnl is not None and hist_pnl != 0.0:
                    realized_pnl = hist_pnl
                elif acc_pnl != 0.0:
                    realized_pnl = acc_pnl
                elif status_val == "OPEN" and p_order_id and not p_order_id.startswith("MOCK_") and get_setting("binance_mock_mode", "true") == "false":
                    try:
                        tz_taipei = timezone(timedelta(hours=8))
                        open_time_str = r["timestamp"]
                        fmt = "%Y-%m-%d %H:%M:%S" if len(open_time_str) > 16 else "%Y-%m-%d %H:%M"
                        open_dt = datetime.strptime(open_time_str, fmt).replace(tzinfo=tz_taipei)
                        start_ts_ms = int((open_dt - timedelta(minutes=10)).timestamp() * 1000)
                        
                        params = {
                            "symbol": r["symbol"].upper(),
                            "startTime": start_ts_ms,
                            "limit": 1000
                        }
                        trade_res = send_binance_request("GET", "/fapi/v1/userTrades", params)
                        if trade_res.get("success") and trade_res.get("data"):
                            realized_pnl = sum(float(t["realizedPnl"]) for t in trade_res["data"])
                    except Exception as te:
                        log.error(f"Error fetching live realized PnL for active position {r['symbol']}: {te}")
                        
                result.append({
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "entry_price": r["entry_price"],
                    "current_price": r["current_price"],
                    "sl": r["sl"],
                    "tps": [r["tp1"], r["tp2"], r["tp3"], r["tp4"]],
                    "current_tp_level": r["current_tp_level"],
                    "size": r["size"],
                    "leverage": r["leverage"],
                    "margin": r["margin"],
                    "pionex_order_id": p_order_id,
                    "status": r["status"],
                    "timestamp": r["timestamp"],
                    "closed_at": closed_at_val,
                    "auto_close": auto_close_val,
                    "timeline": timeline_list,
                    "tp1_order_id": r["tp1_order_id"] if "tp1_order_id" in r.keys() else None,
                    "sl_algo_order_id": r["sl_algo_order_id"] if "sl_algo_order_id" in r.keys() else None,
                    "is_booster": r["is_booster"] if "is_booster" in r.keys() else 0,
                    "strategy_type": strategy_type,
                    "trade_logic": trade_logic,
                    "trailing_base_price": r["trailing_base_price"] if "trailing_base_price" in r.keys() else 0.0,
                    "accumulated_pnl": acc_pnl,
                    "realized_pnl": realized_pnl
                })
            return {"success": True, "data": result, "total": total, "view_filter": view_filter}
    except Exception as e:
        log.error(f"查詢持倉失敗: {e}")
        return {"success": False, "error": str(e)}

def update_position_auto_close(position_id, auto_close):
    try:
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": f"找不到 ID 為 {position_id} 的持倉記錄"}
            
            conn.execute("UPDATE active_positions SET auto_close = ? WHERE id = ?", (int(auto_close), position_id))
            conn.commit()
            
        status_text = "已啟用" if auto_close == 1 else "已關閉"
        log.info(f"✏️ [Position Monitor] 已變更 {pos['symbol']} (ID: {position_id}) 逾時自動平倉設定為: {status_text}")
        return {"success": True, "message": f"成功將逾時自動平倉設定修改為 {status_text}！"}
    except Exception as e:
        log.error(f"修改持倉自動平倉設定失敗: {e}")
        return {"success": False, "error": str(e)}

def update_position_sl(position_id, new_sl):
    try:
        with get_db() as conn:
            pos_row = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos_row:
                return {"success": False, "error": f"找不到 ID 為 {position_id} 的持倉記錄"}
            pos = dict(pos_row)
            symbol = pos["symbol"]
            old_sl = pos["sl"]
            
            # 更新資料庫止損
            conn.execute("UPDATE active_positions SET sl = ? WHERE id = ?", (new_sl, position_id))
            conn.commit()
            
        # 同步更新幣安實體止損委託
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        if not mock_mode:
            is_long = (pos["side"].lower() == "long" or "long" in pos["side"].lower())
            sl_order_side = "SELL" if is_long else "BUY"
            update_binance_stop_loss(symbol, sl_order_side, new_sl)
            
        # 更新歷史日誌與事件記錄
        p_order_id = pos.get("pionex_order_id")
        if p_order_id:
            append_historical_trade_event(p_order_id, f"使用者手動修改止損：{old_sl:.4f} ➡️ {new_sl:.4f}")
            
        # 發送 Telegram 通知
        from config import send_telegram
        msg = f"📝 <b>[手動修改止損] {symbol} 止損已更新！</b>\n"
        msg += f"────────────────────────────\n"
        msg += f"持倉 ID: {position_id}\n"
        msg += f"原止損價: {old_sl:.4f}\n"
        msg += f"新止損價: <b>{new_sl:.4f}</b>\n"
        if mock_mode:
            msg += f"模式: <b>模擬盤 (本地已更新)</b>\n"
        else:
            msg += f"模式: <b>實盤 (幣安交易所委託已同步更新)</b>\n"
        msg += f"────────────────────────────\n"
        send_telegram(msg)
        
        log.info(f"✏️ [Position Monitor] 已手動變更 {symbol} (ID: {position_id}) 止損價: {old_sl:.4f} ➡️ {new_sl:.4f}")
        return {"success": True, "message": f"已成功將止損價修改為 {new_sl:.4f}！"}
    except Exception as e:
        log.error(f"手動修改止損價失敗: {e}")
        return {"success": False, "error": str(e)}

# ── 歷史開單與信號分析 ──
def is_historical_trades_empty():
    try:
        with get_db() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM historical_trades").fetchone()
            return row["count"] == 0 if row else True
    except Exception as e:
        log.error(f"檢查歷史交易表是否為空失敗: {e}")
        return True

def insert_historical_trade(symbol, direction, entry, sl, tps, leverage, logic, trade_type, pionex_order_id=None, time_str=None, reach='PENDING', timeline=None, tf="4H + 1D", note=""):
    try:
        tz = timezone(timedelta(hours=8))
        if not time_str:
            time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
       
        tp_vals = [float(tps[i]) if i < len(tps) else None for i in range(4)]
       
        # 計算盈虧比
        rr = 0.0
        try:
            valid_tps = [t for t in tp_vals if t is not None and t > 0]
            if valid_tps and entry and sl and abs(entry - sl) > 0:
                rr = round(abs(valid_tps[-1] - float(entry)) / abs(float(entry) - float(sl)), 2)
        except Exception as err:
            log.warning(f"計算盈虧比失敗: {err}")
           
        if not timeline:
            timeline_list = [{"time": "00:00", "event": "開單信號觸發，進場參考價 " + str(entry)}]
            timeline = json.dumps(timeline_list, ensure_ascii=False)
        elif isinstance(timeline, list):
            timeline = json.dumps(timeline, ensure_ascii=False)
           
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO historical_trades (
                    time_str, symbol, direction, entry, sl, tp1, tp2, tp3, tp4, rr, tf, leverage, logic, reach, note, timeline, trade_type, pionex_order_id, current_sl, current_tp_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                time_str, symbol.upper(), direction.lower(), float(entry), float(sl),
                tp_vals[0], tp_vals[1], tp_vals[2], tp_vals[3], rr,
                tf, int(leverage) if leverage else 6, logic, reach, note, timeline, trade_type, pionex_order_id, float(sl)
            ))
            conn.commit()
            return int(cur.lastrowid)
    except Exception as e:
        log.error(f"寫入歷史開單記錄失敗: {e}")
        return False

def get_historical_trades(page=1, limit=100):
    try:
        offset = (page - 1) * limit
        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM historical_trades
                ORDER BY time_str DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
           
            total = conn.execute("SELECT COUNT(*) as count FROM historical_trades").fetchone()["count"]
           
            result = []
            for r in rows:
                tps = []
                for tp_col in ["tp1", "tp2", "tp3", "tp4"]:
                    if r[tp_col] is not None and r[tp_col] > 0:
                        tps.append(r[tp_col])
                       
                timeline_list = []
                if r["timeline"]:
                    try:
                        timeline_list = json.loads(r["timeline"])
                    except Exception:
                        pass
               
                result.append({
                    "id": r["id"],
                    "time_str": r["time_str"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "entry": r["entry"],
                    "sl": r["sl"],
                    "tps": tps,
                    "rr": r["rr"],
                    "tf": r["tf"],
                    "leverage": r["leverage"],
                    "logic": r["logic"],
                    "reach": r["reach"],
                    "note": r["note"],
                    "timeline": timeline_list,
                    "trade_type": r["trade_type"],
                    "pionex_order_id": r["pionex_order_id"],
                    "margin": r["margin"] if "margin" in r.keys() else None,
                    "pnl": r["pnl"] if "pnl" in r.keys() else None,
                    "close_reason_code": r["close_reason_code"] if "close_reason_code" in r.keys() else None,
                    "notify_sent": r["notify_sent"] if "notify_sent" in r.keys() else None,
                    "notify_error": r["notify_error"] if "notify_error" in r.keys() else None
                })
            
            # 統計所有有開單過的實體損益總和
            sum_row = conn.execute("SELECT SUM(pnl) as sum_pnl FROM historical_trades WHERE trade_type = 'USER_TRADE'").fetchone()
            total_realized_pnl = sum_row["sum_pnl"] if sum_row and sum_row["sum_pnl"] is not None else 0.0
            
            return {"success": True, "cases": result, "total": total, "total_realized_pnl": total_realized_pnl}
    except Exception as e:
        log.error(f"讀取歷史開單記錄失敗: {e}")
        return {"success": False, "error": str(e), "cases": [], "total_realized_pnl": 0.0}

def append_historical_trade_event(pionex_order_id, event_text, trade_id=None):
    try:
        with get_db() as conn:
            if trade_id:
                row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE id = ?", (trade_id,)).fetchone()
            elif pionex_order_id:
                row = conn.execute("SELECT id, time_str, timeline FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pionex_order_id,)).fetchone()
            else:
                return False
               
            if not row:
                return False
               
            t_id = row["id"]
            start_time_str = row["time_str"]
            timeline_str = row["timeline"]
           
            timeline_list = []
            if timeline_str:
                try:
                    timeline_list = json.loads(timeline_str)
                except Exception:
                    pass
           
            elapsed_str = "00:00"
            try:
                tz_taipei = timezone(timedelta(hours=8))
                fmt = "%Y-%m-%d %H:%M:%S" if len(start_time_str) > 16 else "%Y-%m-%d %H:%M"
                start_dt = datetime.strptime(start_time_str, fmt)
                start_dt_aware = start_dt.replace(tzinfo=tz_taipei)
                now_dt_aware = datetime.now(tz_taipei)
                diff = now_dt_aware - start_dt_aware
                total_seconds = int(diff.total_seconds())
                if total_seconds < 0:
                    total_seconds = 0
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                elapsed_str = f"{hours:02d}:{minutes:02d}"
            except Exception as te:
                log.warning(f"計算相對時間失敗: {te}")
               
            timeline_list.append({"time": elapsed_str, "event": event_text})
           
            conn.execute("UPDATE historical_trades SET timeline = ? WHERE id = ?", (json.dumps(timeline_list, ensure_ascii=False), t_id))
            conn.commit()
        return True
    except Exception as e:
        log.error(f"附加歷史事件失敗: {e}")
        return False

def update_historical_trade_status(trade_id=None, reach=None, current_sl=None, current_tp_level=None, pionex_order_id=None, margin=None, pnl=None):
    try:
        with get_db() as conn:
            updates = []
            params = []
            if reach is not None:
                updates.append("reach = ?")
                params.append(reach)
            if current_sl is not None:
                updates.append("current_sl = ?")
                params.append(float(current_sl))
            if current_tp_level is not None:
                updates.append("current_tp_level = ?")
                params.append(int(current_tp_level))
            if margin is not None:
                updates.append("margin = ?")
                params.append(float(margin))
            if pnl is not None:
                updates.append("pnl = ?")
                params.append(float(pnl))
               
            if not updates:
                return False
               
            if trade_id:
                params.append(trade_id)
                conn.execute(f"UPDATE historical_trades SET {', '.join(updates)} WHERE id = ?", params)
            elif pionex_order_id:
                params.append(pionex_order_id)
                conn.execute(f"UPDATE historical_trades SET {', '.join(updates)} WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", params)
            else:
                return False
               
            conn.commit()
        return True
    except Exception as e:
        log.error(f"更新歷史交易狀態失敗: {e}")
        return False

def update_historical_trade_audit(trade_id=None, pionex_order_id=None, close_reason_code=None, notify_sent=None, notify_error=None):
    try:
        with get_db() as conn:
            updates = []
            params = []
            if close_reason_code is not None:
                updates.append("close_reason_code = ?")
                params.append(str(close_reason_code))
            if notify_sent is not None:
                updates.append("notify_sent = ?")
                params.append(int(1 if notify_sent else 0))
            if notify_error is not None:
                updates.append("notify_error = ?")
                params.append(str(notify_error))

            if not updates:
                return False

            if trade_id:
                params.append(trade_id)
                conn.execute(f"UPDATE historical_trades SET {', '.join(updates)} WHERE id = ?", params)
            elif pionex_order_id:
                params.append(str(pionex_order_id))
                conn.execute(f"UPDATE historical_trades SET {', '.join(updates)} WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", params)
            else:
                return False

            conn.commit()
        return True
    except Exception as e:
        log.error(f"更新歷史交易審計欄位失敗: {e}")
        return False


def sync_linked_signal_status_from_user_trade(symbol, direction, user_trade_id, reach_status, event_text=None):
    """
    將同幣種同方向、且在 USER_TRADE 之前最近一筆 SIGNAL/AI_SENTIMENT_SIGNAL 同步為相同結果，
    避免 USER_TRADE 已平倉但策略歷史仍停留在 PENDING。
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT id, timeline, reach
                FROM historical_trades
                WHERE symbol = ?
                  AND direction = ?
                  AND trade_type IN ('SIGNAL', 'AI_SENTIMENT_SIGNAL')
                  AND id < ?
                  AND reach IN ('PENDING', 'OPEN', 'TP1', 'TP2', 'TP3', 'TP4')
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol.upper(), direction.lower(), int(user_trade_id)),
            ).fetchone()
            if not row:
                return False

            timeline = []
            if row["timeline"]:
                try:
                    timeline = json.loads(row["timeline"])
                except Exception:
                    timeline = []

            # 若傳入的 reach_status 為 CLOSED 但實際上交易為虧損 (pnl < 0)，則自動映射為 SL
            target_reach = reach_status
            if reach_status in ['CLOSED', 'SL_HIT']:
                user_trade_row = conn.execute("SELECT pnl, reach FROM historical_trades WHERE id = ?", (int(user_trade_id),)).fetchone()
                if user_trade_row:
                    u_pnl = user_trade_row["pnl"]
                    u_reach = user_trade_row["reach"]
                    if u_reach == 'SL' or (u_pnl is not None and float(u_pnl) < 0):
                        target_reach = 'SL'

            if event_text:
                timeline.append({"time": "AUTO", "event": event_text})

            conn.execute(
                "UPDATE historical_trades SET reach = ?, timeline = ? WHERE id = ?",
                (target_reach, json.dumps(timeline, ensure_ascii=False), row["id"]),
            )
            conn.commit()
            return True
    except Exception as e:
        log.error(f"同步關聯 SIGNAL 狀態失敗: {e}")
        return False


def expire_pending_signal_on_queue_remove(symbol, direction, event_text=None, trade_type="SIGNAL"):
    """
    手動移出監控佇列時，將同幣種同方向仍為 PENDING/OPEN 的策略信號同步為 EXPIRED，
    避免開單通知紀錄仍顯示「監控中」。
    """
    try:
        if not symbol or not direction:
            return None
        symbol = str(symbol).upper().strip()
        direction = str(direction).lower().strip()
        trade_types = (trade_type,) if isinstance(trade_type, str) else tuple(trade_type)
        placeholders = ",".join("?" for _ in trade_types)
        with get_db() as conn:
            row = conn.execute(
                f"""
                SELECT id
                FROM historical_trades
                WHERE trade_type IN ({placeholders})
                  AND symbol = ?
                  AND direction = ?
                  AND reach IN ('PENDING', 'OPEN')
                ORDER BY time_str DESC, id DESC
                LIMIT 1
                """,
                (*trade_types, symbol, direction),
            ).fetchone()
            if not row:
                return None
            trade_id = int(row["id"])

        update_historical_trade_status(trade_id=trade_id, reach="EXPIRED")
        append_historical_trade_event(
            None,
            event_text or "用戶手動移出背景監控佇列，狀態同步為 EXPIRED",
            trade_id=trade_id,
        )
        return trade_id
    except Exception as e:
        log.error(f"移出佇列時同步信號 EXPIRED 失敗 ({symbol}/{direction}): {e}")
        return None


def repair_pending_signal_status(trade_id: int):
    """
    修復卡在 PENDING/OPEN 的 SIGNAL / AI_SENTIMENT_SIGNAL：
    1) 若後續存在同幣種同方向 USER_TRADE，跟隨其終態；
    2) 若無後續 USER_TRADE 且已逾 24h，標記 EXPIRED；
    3) 否則維持原狀。
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT id, time_str, symbol, direction, reach, trade_type, timeline
                FROM historical_trades
                WHERE id = ?
                """,
                (int(trade_id),),
            ).fetchone()
            if not row:
                return {"success": False, "error": "找不到此歷史記錄"}
            if row["trade_type"] not in ("SIGNAL", "AI_SENTIMENT_SIGNAL"):
                return {"success": False, "error": "僅支援修復 SIGNAL / AI_SENTIMENT_SIGNAL"}

            current_reach = str(row["reach"] or "PENDING")
            if current_reach not in ("PENDING", "OPEN", "TP1", "TP2", "TP3", "TP4"):
                return {"success": True, "message": f"此筆狀態已為 {current_reach}，無需修復"}

            linked_user = conn.execute(
                """
                SELECT id, reach
                FROM historical_trades
                WHERE symbol = ?
                  AND direction = ?
                  AND trade_type = 'USER_TRADE'
                  AND id > ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (row["symbol"], row["direction"], int(trade_id)),
            ).fetchone()

            timeline = []
            if row["timeline"]:
                try:
                    timeline = json.loads(row["timeline"])
                except Exception:
                    timeline = []

            new_reach = None
            if linked_user and linked_user["reach"] and linked_user["reach"] != "OPEN":
                new_reach = str(linked_user["reach"])
                timeline.append({"time": "AUTO", "event": f"手動修復：同步關聯 USER_TRADE 終態 ({new_reach})。"})
            else:
                try:
                    start = parse_sim_time(row["time_str"])
                    now_tpe = datetime.now(timezone(timedelta(hours=8)))
                    start_tpe = start.replace(tzinfo=timezone(timedelta(hours=8))) if start.tzinfo is None else start
                    if now_tpe - start_tpe > timedelta(hours=24):
                        new_reach = "EXPIRED"
                        timeline.append({"time": "AUTO", "event": "手動修復：超過 24h 未結算，標記為 EXPIRED。"})
                except Exception:
                    pass

            if not new_reach:
                return {"success": True, "message": "尚無可同步的終態（可能仍在有效監控時間內）"}

            conn.execute(
                "UPDATE historical_trades SET reach = ?, timeline = ? WHERE id = ?",
                (new_reach, json.dumps(timeline, ensure_ascii=False), int(trade_id)),
            )
            conn.commit()
            return {"success": True, "message": f"已修復為 {new_reach}", "reach": new_reach}
    except Exception as e:
        log.error(f"修復 Pending 信號狀態失敗: {e}")
        return {"success": False, "error": str(e)}

def reconcile_trade_pnl(pionex_order_id, fallback_pnl=None):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        with get_db() as conn:
            # Fetch trade from historical_trades
            ht = conn.execute("SELECT * FROM historical_trades WHERE pionex_order_id = ? AND trade_type = 'USER_TRADE'", (pionex_order_id,)).fetchone()
            if not ht:
                return False
                
            if mock_mode:
                if fallback_pnl is not None:
                    conn.execute("UPDATE historical_trades SET pnl = ? WHERE id = ?", (fallback_pnl, ht["id"]))
                    conn.commit()
                return True
                
            # Real mode: wait a moment for fills to settle
            time.sleep(1.5)
            
            symbol = ht["symbol"]
            open_time_str = ht["time_str"]
            
            # Parse open time
            tz_taipei = timezone(timedelta(hours=8))
            fmt = "%Y-%m-%d %H:%M:%S" if len(open_time_str) > 16 else "%Y-%m-%d %H:%M"
            open_dt = datetime.strptime(open_time_str, fmt).replace(tzinfo=tz_taipei)
            start_ts_ms = int((open_dt - timedelta(minutes=10)).timestamp() * 1000)
            
            # End time is now + 5 minutes
            end_ts_ms = int((datetime.now(tz_taipei) + timedelta(minutes=5)).timestamp() * 1000)
            
            # Query Binance user trades
            params = {
                "symbol": symbol.upper(),
                "startTime": start_ts_ms,
                "endTime": end_ts_ms,
                "limit": 1000
            }
            
            max_retries = 4
            for attempt in range(max_retries):
                res = send_binance_request("GET", "/fapi/v1/userTrades", params)
                if res.get("success") and res.get("data"):
                    trades = res["data"]
                    
                    # Try to locate the entry trade by orderId
                    entry_trades = [t for t in trades if str(t.get("orderId")) == str(pionex_order_id)]
                    if entry_trades:
                        entry_qty = sum(float(t["qty"]) for t in entry_trades)
                        entry_side = entry_trades[0]["side"]
                        entry_time = min(int(t["time"]) for t in entry_trades)
                        
                        close_side = "SELL" if entry_side == "BUY" else "BUY"
                        close_trades = [t for t in trades if t["side"] == close_side and int(t["time"]) >= entry_time]
                        close_qty = sum(float(t["qty"]) for t in close_trades)
                        
                        # Verify if the closed size matches the entry size
                        if abs(close_qty - entry_qty) < 1e-5:
                            binance_pnl = sum(float(t["realizedPnl"]) for t in close_trades)
                            conn.execute("UPDATE historical_trades SET pnl = ? WHERE id = ?", (binance_pnl, ht["id"]))
                            conn.commit()
                            log.info(f"成功對接對帳(完整平倉)：交易 ID {ht['id']} ({symbol}) PnL 經交易所對帳校正為: {binance_pnl:.4f} USDT")
                            return True
                        else:
                            log.warning(f"對接對帳數量不符 (第 {attempt+1}/{max_retries} 次嘗試): 預期平倉數量 {entry_qty:.4f}, 實際平倉數量 {close_qty:.4f}")
                    else:
                        log.warning(f"對接對帳未找到開倉交易 ID: {pionex_order_id} (第 {attempt+1}/{max_retries} 次嘗試)")
                else:
                    log.warning(f"對接對帳未取得交易所數據 (第 {attempt+1}/{max_retries} 次嘗試)")
                
                if attempt < max_retries - 1:
                    time.sleep(2.0)
            
            # If we run out of retries, fallback to local calculation
            final_pnl = fallback_pnl
            if final_pnl is None:
                # If no fallback_pnl is provided, sum whatever realized PnL we have as a last resort
                res = send_binance_request("GET", "/fapi/v1/userTrades", params)
                if res.get("success") and res.get("data"):
                    final_pnl = sum(float(t["realizedPnl"]) for t in res["data"])
                else:
                    final_pnl = 0.0
            
            conn.execute("UPDATE historical_trades SET pnl = ? WHERE id = ?", (final_pnl, ht["id"]))
            conn.commit()
            log.warning(f"對接對帳未取得交易所完整平倉數據(數量不符或未找到開倉單)，使用本地計算/備用數據 (Fallback) 交易 ID {ht['id']} ({symbol}) PnL: {final_pnl:.4f} USDT")
            return True
            
    except Exception as e:
        log.error(f"執行 reconcile_trade_pnl 異常 (pionex_order_id: {pionex_order_id}): {e}")
    return False

def get_binance_futures_balance():
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        if mock_mode:
            return {"success": True, "balance": 10000.0, "mock": True}
            
        res = send_binance_request("GET", "/fapi/v2/balance")
        if not res["success"]:
            if "尚未配置" in res.get("error", ""):
                return {"success": False, "error": "尚未配置 API KEY"}
            return res
            
        balances = res["data"]
        if isinstance(balances, list):
            for b in balances:
                if b.get("asset") == "USDT":
                    avail_bal = float(b.get("availableBalance", 0.0))
                    return {"success": True, "balance": avail_bal, "mock": False}
            return {"success": True, "balance": 0.0, "mock": False, "note": "找不到 USDT 餘額"}
        return {"success": False, "error": "API 回傳格式錯誤"}
    except Exception as e:
        log.error(f"獲取幣安餘額異常: {e}")
        return {"success": False, "error": str(e)}

def update_binance_stop_loss(symbol, side, sl_price, position_id=None):
    """更新幣安止損委託單。
    優先透過 position_id 查找記錄的 sl_algo_order_id，只撤除自己的 SL 單再重掛新 SL。
    若無記錄則退化為全清（向舊版相容），並發出警告。
    """
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        if mock_mode:
            return {"success": True}
            
        clean_sym = symbol.upper()
        
        # 1. 嘗試只撤除本持倉記錄的 SL Algo 單
        old_sl_algo_id = None
        if position_id:
            try:
                with get_db() as conn:
                    pos_row = conn.execute("SELECT sl_algo_order_id FROM active_positions WHERE id = ?", (position_id,)).fetchone()
                    if pos_row:
                        old_sl_algo_id = pos_row["sl_algo_order_id"]
            except Exception as db_err:
                log.warning(f"讀取 sl_algo_order_id 失敗: {db_err}")
        
        if old_sl_algo_id:
            try:
                cancel_res = send_binance_request("DELETE", "/fapi/v1/algoOrder", {"symbol": clean_sym, "algoId": int(old_sl_algo_id)})
                if cancel_res["success"]:
                    log.info(f"[update_sl] 已撤除舊 SL Algo 單 (AlgoID: {old_sl_algo_id}) for {clean_sym}")
                else:
                    log.warning(f"[update_sl] 撤除舊 SL Algo 單失敗 (AlgoID: {old_sl_algo_id}): {cancel_res.get('error')}")
            except Exception as cancel_err:
                log.warning(f"[update_sl] 撤除舊 SL Algo 單異常: {cancel_err}")
        else:
            # 無記錄 ID，退化為全清（向舊版相容，但發出警告）
            log.warning(f"[update_sl] {clean_sym} 無 sl_algo_order_id 記錄，退化為全清模式（可能影響 TP1 掛單或手動倉止損）。")
            cancel_res = send_binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": clean_sym})
            if not cancel_res["success"]:
                log.warning(f"幣安更新止損時取消舊委託失敗 {clean_sym}: {cancel_res.get('error')}")
            cancel_algo_res = send_binance_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": clean_sym})
            if not cancel_algo_res["success"]:
                log.warning(f"幣安更新止損時取消舊 Algo 委託失敗 {clean_sym}: {cancel_algo_res.get('error')}")
            
        # 2. 重新掛上新的 STOP_MARKET 止損委託單 (使用新版 Algo Order API)
        sl_data = {
            "algoType": "CONDITIONAL",
            "symbol": clean_sym,
            "side": side,
            "type": "STOP_MARKET",
            "triggerPrice": sl_price,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        }
        sl_res = send_binance_request("POST", "/fapi/v1/algoOrder", sl_data)
        
        # 3. 若成功，儲存新的 sl_algo_order_id 回 DB
        if sl_res.get("success") and position_id:
            try:
                new_sl_algo_id = str(sl_res["data"].get("algoId") or sl_res["data"].get("orderId", ""))
                if new_sl_algo_id:
                    with get_db() as conn:
                        conn.execute("UPDATE active_positions SET sl_algo_order_id = ? WHERE id = ?", (new_sl_algo_id, position_id))
                        conn.commit()
                    log.info(f"[update_sl] {clean_sym} 新 SL Algo 單已掛載，更新 DB sl_algo_order_id = {new_sl_algo_id}")
            except Exception as upd_err:
                log.warning(f"[update_sl] 更新 sl_algo_order_id 失敗: {upd_err}")
        
        return sl_res
    except Exception as e:
        log.error(f"幣安更新止損委託失敗: {e}")
        return {"success": False, "error": str(e)}

def delete_active_position(position_id):
    try:
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
            conn.execute("DELETE FROM active_positions WHERE id = ?", (position_id,))
            conn.commit()
        return {"success": True, "message": "持倉記錄已成功刪除！"}
    except Exception as e:
        log.error(f"刪除持倉記錄失敗: {e}")
        return {"success": False, "error": str(e)}

def sync_position_with_binance(position_id):
    try:
        mock_mode = get_setting("binance_mock_mode", "true") == "true"
        with get_db() as conn:
            pos = conn.execute("SELECT * FROM active_positions WHERE id = ?", (position_id,)).fetchone()
            if not pos:
                return {"success": False, "error": "找不到此持倉記錄"}
            
            symbol = pos["symbol"]
            status = pos["status"]
            side = pos["side"]
            pionex_order_id = pos["pionex_order_id"]
            
            if status not in ["OPEN", "PENDING_ORDER"]:
                return {"success": True, "message": f"此持倉已是結算狀態 ({status})", "status": status}
                
            if mock_mode:
                return {"success": True, "message": "模擬模式下無法與交易所同步", "status": status}
                
            if status == "PENDING_ORDER":
                order_id = pionex_order_id
                if order_id and not str(order_id).startswith("MOCK_"):
                    order_res = send_binance_request("GET", "/fapi/v1/order", {"symbol": symbol.upper(), "orderId": int(order_id)})
                    if order_res["success"]:
                        order_state = order_res["data"].get("status")
                        if order_state == "FILLED":
                            entry_px = float(order_res["data"].get("price", pos["entry_price"]))
                            conn.execute("UPDATE active_positions SET status = 'OPEN', entry_price = ? WHERE id = ?", (entry_px, position_id))
                            
                            update_historical_trade_status(pionex_order_id=pionex_order_id, reach="OPEN")
                            append_historical_trade_event(pionex_order_id, "限價單成交：與幣安同步檢測到限價單已成交，本地更新為進行中。")
                            
                            sl_price = pos["sl"]
                            sl_side = "SELL" if side == "BUY" else "BUY"
                            sl_data = {
                                "algoType": "CONDITIONAL",
                                "symbol": symbol.upper(),
                                "side": sl_side,
                                "type": "STOP_MARKET",
                                "triggerPrice": sl_price,
                                "closePosition": "true",
                                "workingType": "MARK_PRICE"
                            }
                            # 稍息一秒以防交易所持倉狀態延遲導致 GTE 報錯
                            time.sleep(1.0)
                            sl_res = send_binance_request("POST", "/fapi/v1/algoOrder", sl_data)
                            if not sl_res["success"]:
                                log.warning(f"幣安設置實體止損委託單失敗: {sl_res.get('error')}")
                                
                            # ── 新增：限價掛載 TP1 減倉單 (50% 數量) ──
                            tp1_price = pos["tp1"]
                            if tp1_price and tp1_price > 0:
                                qty_prec, price_prec = get_binance_symbol_precision(symbol.upper())
                                pos_size = float(pos["size"])
                                tp1_size = round(pos_size * 0.50, qty_prec)
                                if tp1_size > 0:
                                    tp1_price_formatted = round(tp1_price, price_prec)
                                    tp1_data = {
                                        "symbol": symbol.upper(),
                                        "side": sl_side,
                                        "type": "LIMIT",
                                        "price": tp1_price_formatted,
                                        "quantity": tp1_size,
                                        "timeInForce": "GTC",
                                        "reduceOnly": "true"
                                    }
                                    tp1_res = send_binance_request("POST", "/fapi/v1/order", tp1_data)
                                    if tp1_res["success"]:
                                        log.info(f"✅ [Position Sync] 限價建倉成功後，實體限價 TP1 減倉單掛載成功！價格: {tp1_price_formatted}, 數量: {tp1_size}")
                                    else:
                                        log.warning(f"❌ [Position Sync] 限價建倉成功後，實體限價 TP1 減倉單掛載失敗: {tp1_res.get('error')}")
                            
                            conn.commit()
                            
                            msg = (
                                f"📈 <b>[幣安限價單成交] {symbol} 已成功建倉！</b>\n"
                                f"────────────────────────────\n"
                                f"方向: {'📈 多 (LONG)' if side == 'BUY' else '📉 空 (SHORT)'}\n"
                                f"成交價格: {entry_px:.4f}\n"
                                f"數量: {pos['size']:.4f}\n"
                                f"────────────────────────────\n"
                                f"狀態: 已自動轉為進行中，開始追蹤持倉與移動止損。"
                            )
                            send_telegram(msg)
                            
                            return {"success": True, "message": "同步完成：限價委託單已成交，本地已更新為進行中持倉。", "status": "OPEN"}
                        elif order_state in ["CANCELED", "EXPIRED"]:
                            conn.execute("UPDATE active_positions SET status = 'CLOSED', closed_at = COALESCE(closed_at, ?) WHERE id = ?", (_now_taipei_str(), position_id))
                            update_historical_trade_status(pionex_order_id=pionex_order_id, reach="CLOSED")
                            append_historical_trade_event(pionex_order_id, f"與幣安同步：檢測到限價委託單已撤銷或失效 (狀態: {order_state})。")
                            conn.commit()
                            return {"success": True, "message": "同步完成：限價單已撤銷/失效，本地已更新為已關閉狀態。", "status": "CLOSED"}
                        else:
                            return {"success": True, "message": f"同步完成：限價委託單仍處於掛單狀態 ({order_state})", "status": "PENDING_ORDER"}
                    else:
                        return {"success": False, "error": f"獲取幣安訂單狀態失敗: {order_res.get('error')}"}
                return {"success": True, "message": "限價單無效或為模擬單", "status": "PENDING_ORDER"}
                
            # 獲取幣安實體持倉 (status == "OPEN")
            res = send_binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()})
            if not res["success"]:
                return {"success": False, "error": f"獲取幣安持倉失敗: {res.get('error')}"}
                
            data = res["data"]
            matched_pos = None
            is_long = (side == "BUY")
            for p in data:
                amt = float(p.get("positionAmt", 0.0))
                if (is_long and amt > 0) or (not is_long and amt < 0):
                    matched_pos = p
                    break
            
            if matched_pos is None or float(matched_pos.get("positionAmt", 0.0)) == 0.0:
                conn.execute("UPDATE active_positions SET status = 'CLOSED', closed_at = COALESCE(closed_at, ?) WHERE id = ?", (_now_taipei_str(), position_id))
                if pionex_order_id:
                    update_historical_trade_status(pionex_order_id=pionex_order_id, reach="CLOSED")
                    append_historical_trade_event(pionex_order_id, "與幣安同步：檢測到交易所持倉已關閉，本地同步平倉結算。")
                conn.commit()
                return {"success": True, "message": "已成功同步：檢測到幣安交易所持倉已結束，本地已更新為已結算狀態。", "status": "CLOSED"}
            else:
                curr_price = float(matched_pos.get("markPrice", pos["current_price"]))
                conn.execute("UPDATE active_positions SET current_price = ? WHERE id = ?", (curr_price, position_id))
                conn.commit()
                return {"success": True, "message": f"同步完成：幣安持倉正常（目前標記價格為 {curr_price:.4f}）", "status": "OPEN"}
    except Exception as e:
        log.error(f"同步幣安持倉失敗: {e}")
        return {"success": False, "error": str(e)}



# ── 資金費率套利與 OI/CVD 擴充模組 ──

def get_binance_oi_cvd(symbol, period="5m", limit=30):
    '''
    獲取幣安公開的 OI 和 CVD 數據 (無須 API 金鑰，公有數據)
    '''
    try:
        symbol = symbol.upper().strip()
        oi_url = "https://fapi.binance.com/futures/data/openInterestHist"
        vol_url = "https://fapi.binance.com/futures/data/takerBuySellVol"
        
        headers = {"Content-Type": "application/json"}
        
        oi_res = requests.get(oi_url, params={"symbol": symbol, "period": period, "limit": limit}, headers=headers, timeout=5)
        oi_data = oi_res.json() if oi_res.status_code == 200 else []
        
        vol_res = requests.get(vol_url, params={"symbol": symbol, "period": period, "limit": limit}, headers=headers, timeout=5)
        vol_data = vol_res.json() if vol_res.status_code == 200 else []
        
        return {
            "success": True,
            "oi": oi_data,
            "vol": vol_data
        }
    except Exception as e:
        log.error(f"  🧬 [OI/CVD API] 獲取 {symbol} 失敗: {e}")
        return {"success": False, "error": str(e)}

# ── 幣安現貨交易對資訊快取與精確度處理 ──
_SPOT_SYMBOLS_CACHE = {}  # symbol -> metadata dict
_SPOT_SYMBOLS_LAST_FETCH = 0.0

def get_spot_symbol_info(symbol):
    global _SPOT_SYMBOLS_CACHE, _SPOT_SYMBOLS_LAST_FETCH
    symbol = symbol.upper().strip()
    now = time.time()
    
    # 每一小時更新一次快取
    if not _SPOT_SYMBOLS_CACHE or (now - _SPOT_SYMBOLS_LAST_FETCH > 3600):
        try:
            r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10)
            if r.status_code == 200:
                data = r.json()
                new_cache = {}
                for sym_info in data.get("symbols", []):
                    sym_name = sym_info["symbol"]
                    if sym_info["status"] != "TRADING":
                        continue
                    
                    filters = {f["filterType"]: f for f in sym_info.get("filters", [])}
                    
                    # 數量精確度 (stepSize)
                    qty_precision = 4  # 預設防呆
                    if "LOT_SIZE" in filters:
                        step_size = filters["LOT_SIZE"]["stepSize"].rstrip('0')
                        if '.' in step_size:
                            qty_precision = len(step_size.split('.')[1])
                        else:
                            qty_precision = 0
                            
                    # 價格精確度 (tickSize)
                    price_precision = 4  # 預設防呆
                    if "PRICE_FILTER" in filters:
                        tick_size = filters["PRICE_FILTER"]["tickSize"].rstrip('0')
                        if '.' in tick_size:
                            price_precision = len(tick_size.split('.')[1])
                        else:
                            price_precision = 0
                            
                    # 最小名義價值
                    min_notional = 5.0
                    if "NOTIONAL" in filters:
                        min_notional = float(filters["NOTIONAL"]["minNotional"])
                        
                    new_cache[sym_name] = {
                        "qty_precision": qty_precision,
                        "price_precision": price_precision,
                        "min_notional": min_notional,
                        "base_asset": sym_info["baseAsset"],
                        "quote_asset": sym_info["quoteAsset"]
                    }
                if new_cache:
                    _SPOT_SYMBOLS_CACHE = new_cache
                    _SPOT_SYMBOLS_LAST_FETCH = now
                    log.info(f"📊 [Spot Cache] 已更新 {len(new_cache)} 個幣安現貨交易對資訊快取")
        except Exception as e:
            log.error(f"  🧬 [Spot Cache] 獲取幣安現貨 ExchangeInfo 失敗: {e}")
            
    return _SPOT_SYMBOLS_CACHE.get(symbol)

def get_binance_spot_asset_balance(asset):
    '''
    獲取現貨某個幣種的可用餘額
    '''
    try:
        api_key = get_setting("binance_api_key")
        api_secret = get_setting("binance_api_secret")
        if not api_key or not api_secret:
            return 0.0
            
        base_url = "https://api.binance.com"
        params = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000
        }
        query_str = urlencode(params)
        sig = binance_signature(api_secret, query_str)
        url = f"{base_url}/api/v3/account?{query_str}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}
        
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            res_json = r.json()
            balances = res_json.get("balances", [])
            for b in balances:
                if b.get("asset") == asset.upper():
                    return float(b.get("free", 0.0))
        else:
            log.error(f"  ❌ [Spot Balance] 獲取餘額 API 回傳錯誤: {r.status_code} - {r.text}")
        return 0.0
    except Exception as e:
        log.error(f"  ❌ [Spot Balance] 獲取 {asset} 餘額失敗: {e}")
        return 0.0

def redeem_simple_earn_flexible(asset):
    '''
    檢查並贖回 Binance Simple Earn Flexible 中對應幣種的資產至現貨帳戶
    '''
    try:
        api_key = get_setting("binance_api_key")
        api_secret = get_setting("binance_api_secret")
        if not api_key or not api_secret:
            return False
            
        base_url = "https://api.binance.com"
        
        # 1. 查詢 Simple Earn Flexible 持倉
        params_pos = {
            "asset": asset.upper(),
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000
        }
        query_pos = urlencode(params_pos)
        sig_pos = binance_signature(api_secret, query_pos)
        url_pos = f"{base_url}/sapi/v1/simple-earn/flexible/position?{query_pos}&signature={sig_pos}"
        headers = {"X-MBX-APIKEY": api_key}
        
        r_pos = requests.get(url_pos, headers=headers, timeout=10)
        if r_pos.status_code != 200:
            log.warning(f"💰 [Simple Earn] 查詢 {asset} 定期/活期持倉失敗: {r_pos.text}")
            return False
            
        pos_data = r_pos.json()
        rows = pos_data.get("rows", [])
        if not rows:
            return False
            
        redeemed_any = False
        for row in rows:
            product_id = row.get("productId")
            total_amt = float(row.get("totalAmount", 0.0))
            if total_amt > 0 and product_id:
                log.info(f"💰 [Simple Earn] 發現 {asset} 活期存儲持倉: {total_amt}，產品ID: {product_id}，正在進行自動贖回...")
                
                # 2. 發送贖回請求
                params_red = {
                    "productId": product_id,
                    "redeemAll": "true",
                    "timestamp": int(time.time() * 1000),
                    "recvWindow": 5000
                }
                query_red = urlencode(params_red)
                sig_red = binance_signature(api_secret, query_red)
                url_red = f"{base_url}/sapi/v1/simple-earn/flexible/redeem?{query_red}&signature={sig_red}"
                
                r_red = requests.post(url_red, headers=headers, timeout=10)
                if r_red.status_code == 200:
                    res_red = r_red.json()
                    if res_red.get("success"):
                        log.info(f"💰 [Simple Earn] {asset} 活期自動贖回成功: {res_red}")
                        redeemed_any = True
                    else:
                        log.error(f"💰 [Simple Earn] {asset} 活期自動贖回失敗: {res_red}")
                else:
                    log.error(f"💰 [Simple Earn] 活期自動贖回 API 錯誤: {r_red.status_code} - {r_red.text}")
                    
        if redeemed_any:
            # 延遲一下等待餘額入帳更新
            time.sleep(1.5)
            return True
        return False
    except Exception as e:
        log.error(f"  ❌ [Simple Earn] 處理 {asset} 活期自動贖回時異常: {e}")
        return False
