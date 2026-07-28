# -*- coding: utf-8 -*-
"""
📡 Antigravity Dashboard Web Server
"""
import os
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
import sqlite3
import requests
import db_manager
from config import (
    log, BOT_TOKEN, CHAT_ID, COINS, PRIMARY_TF, SCAN_TIMEFRAMES,
    SCAN_INTERVAL, MIN_CONDITIONS, MIN_SCORE, MAX_CONCURRENT_TRADES, REQUEST_DELAY,
    TZ_OFFSET, WEB_PORT, ACTIVE_TRADES_FILE, BTC_MACRO_STATE, parse_sim_time,
    send_telegram, load_active_trades, save_active_trades, _normalize, fetch_klines, fetch_ticker,
)
from analyzers import (
    calc_ema, calc_rsi, calc_atr, find_swings, calc_bb,
    check_vegas, check_fib, check_ob, check_rsi, check_sr, check_bb, check_fvg,
    decide_direction, gen_smart_tpsl, analyze_symbol, get_higher_tf, calc_signal_expiry,
    analyze_btc_macro
)
from monitor import calibrate_pending_signals, sync_all_positions_with_binance_on_startup
from analyzers.ai_sentiment import load_ai_sentiment_state, save_ai_sentiment_state, update_ai_sentiment_state

# Global storage for Felisa L/S scanner status (accessible via web API)
FELISA_LS_SCAN_RESULTS = {}
# Global storage for AI Sentiment scanner status
AI_SENTIMENT_SCAN_RESULTS = {}
# Global storage for latest AI Sentiment decision funnel metrics
AI_SENTIMENT_FUNNEL = {}

# Lightweight in-process caches to reduce repeated market-data calls
_MARKET_CACHE = {}
_MARKET_CACHE_LOCK = threading.Lock()
_BTC_MACRO_CACHE = {}
_BTC_MACRO_CACHE_LOCK = threading.Lock()
_AI_ACTIVE_TRADES_FILE_CACHE = {
    "mtime": None,
    "loaded_at": 0.0,
    "data": []
}
_AI_ACTIVE_TRADES_FILE_CACHE_LOCK = threading.Lock()
_AUTOSCAN_RESULTS_CACHE = {}
_AUTOSCAN_RESULTS_CACHE_LOCK = threading.Lock()
_AUTOSCAN_LATEST_PAYLOAD = {}
_AUTOSCAN_LATEST_PAYLOAD_LOCK = threading.Lock()
_API_METRICS = {}
_API_METRICS_LOCK = threading.Lock()


def _cache_get(cache: dict, lock: threading.Lock, key):
    now = time.time()
    with lock:
        item = cache.get(key)
        if not item:
            return None
        if item["expire_at"] <= now:
            cache.pop(key, None)
            return None
        return item["value"]


def _cache_set(cache: dict, lock: threading.Lock, key, value, ttl_sec: float):
    with lock:
        cache[key] = {
            "value": value,
            "expire_at": time.time() + max(0.1, float(ttl_sec))
        }


def _record_api_metric(method: str, path: str, status_code: int, elapsed_ms: float):
    now = time.time()
    key = f"{method} {path}"
    elapsed = max(0.0, float(elapsed_ms))
    with _API_METRICS_LOCK:
        m = _API_METRICS.get(key)
        if not m:
            m = {
                "method": method,
                "path": path,
                "count": 0,
                "error_count": 0,
                "last_status": 0,
                "last_ms": 0.0,
                "max_ms": 0.0,
                "sum_ms": 0.0,
                "samples": deque(maxlen=240),
                "updated_at": 0.0,
            }
            _API_METRICS[key] = m
        m["count"] += 1
        if int(status_code) >= 400:
            m["error_count"] += 1
        m["last_status"] = int(status_code)
        m["last_ms"] = elapsed
        m["sum_ms"] += elapsed
        m["max_ms"] = max(m["max_ms"], elapsed)
        m["samples"].append(elapsed)
        m["updated_at"] = now


def _metrics_snapshot():
    with _API_METRICS_LOCK:
        rows = []
        for _, m in _API_METRICS.items():
            samples = sorted(list(m["samples"]))
            n = len(samples)
            if n == 0:
                p50 = p95 = avg = 0.0
            else:
                p50 = samples[int((n - 1) * 0.50)]
                p95 = samples[int((n - 1) * 0.95)]
                avg = m["sum_ms"] / m["count"] if m["count"] > 0 else 0.0
            rows.append({
                "method": m["method"],
                "path": m["path"],
                "count": m["count"],
                "error_count": m["error_count"],
                "error_rate_pct": round((m["error_count"] / m["count"] * 100.0), 2) if m["count"] else 0.0,
                "last_status": m["last_status"],
                "last_ms": round(m["last_ms"], 2),
                "avg_ms": round(avg, 2),
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "max_ms": round(m["max_ms"], 2),
                "updated_at": datetime.fromtimestamp(m["updated_at"], tz=timezone.utc).isoformat() if m["updated_at"] else None,
            })
    rows.sort(key=lambda x: (x["path"], x["method"]))
    return {
        "success": True,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "rows": rows,
    }


def cached_fetch_klines(symbol: str, tf: str, limit: int = 200, end_time_ms: int = None, start_time_ms: int = None, ttl_sec: float = 3.0):
    norm_symbol = _normalize(symbol).upper()
    tf_norm = str(tf).lower()
    key = ("klines", norm_symbol, tf_norm, int(limit), end_time_ms, start_time_ms)
    hit = _cache_get(_MARKET_CACHE, _MARKET_CACHE_LOCK, key)
    if hit is not None:
        return hit
    data = fetch_klines(norm_symbol, tf_norm, limit=limit, end_time_ms=end_time_ms, start_time_ms=start_time_ms)
    if data:
        _cache_set(_MARKET_CACHE, _MARKET_CACHE_LOCK, key, data, ttl_sec)
    return data


def cached_fetch_ticker(symbol: str, ttl_sec: float = 2.0):
    norm_symbol = _normalize(symbol).upper()
    key = ("ticker", norm_symbol)
    hit = _cache_get(_MARKET_CACHE, _MARKET_CACHE_LOCK, key)
    if hit is not None:
        return hit
    data = fetch_ticker(norm_symbol)
    if data:
        _cache_set(_MARKET_CACHE, _MARKET_CACHE_LOCK, key, data, ttl_sec)
    return data


def cached_analyze_btc_macro(end_ts: int = None):
    # 模擬時間點屬固定歷史查詢，可長快取；即時值使用較短快取
    key = ("btc_macro", end_ts if end_ts is not None else "realtime")
    hit = _cache_get(_BTC_MACRO_CACHE, _BTC_MACRO_CACHE_LOCK, key)
    if hit is not None:
        return hit
    data = analyze_btc_macro(end_ts) if end_ts else BTC_MACRO_STATE
    ttl = 30.0 if end_ts is not None else 8.0
    _cache_set(_BTC_MACRO_CACHE, _BTC_MACRO_CACHE_LOCK, key, data, ttl)
    return data


def load_ai_sentiment_active_trades_cached():
    active_file = "active_trades_ai_sentiment.json"
    if not os.path.exists(active_file):
        return []
    try:
        mtime = os.path.getmtime(active_file)
        now = time.time()
        with _AI_ACTIVE_TRADES_FILE_CACHE_LOCK:
            cached_mtime = _AI_ACTIVE_TRADES_FILE_CACHE["mtime"]
            cached_loaded_at = _AI_ACTIVE_TRADES_FILE_CACHE["loaded_at"]
            if cached_mtime == mtime and (now - cached_loaded_at) <= 2.0:
                return _AI_ACTIVE_TRADES_FILE_CACHE["data"]
        with open(active_file, "r", encoding="utf-8") as f:
            active_data = json.load(f)
        parsed = list(active_data.values())
        with _AI_ACTIVE_TRADES_FILE_CACHE_LOCK:
            _AI_ACTIVE_TRADES_FILE_CACHE["mtime"] = mtime
            _AI_ACTIVE_TRADES_FILE_CACHE["loaded_at"] = now
            _AI_ACTIVE_TRADES_FILE_CACHE["data"] = parsed
        return parsed
    except Exception as fe:
        log.error(f"Failed to parse active_trades_ai_sentiment.json: {fe}")
        return []


def _compute_autoscan_symbol(sym: str, tf: str, higher_tf: str, btc_state: str, ai_sentiment: str):
    klines = cached_fetch_klines(sym, tf, 200, ttl_sec=3.0)
    ticker = cached_fetch_ticker(sym, ttl_sec=2.0)
    res = analyze_symbol(sym, klines, ticker)
    if not res:
        return None

    direction = res["direction"]
    higher_klines = cached_fetch_klines(sym, higher_tf, 200, ttl_sec=3.0)
    higher_result = analyze_symbol(sym, higher_klines, ticker) if higher_klines else None

    mtf_conflict = False
    if higher_result:
        if higher_result["trend"] != "neutral":
            agree = higher_result["trend"] == direction
            if not agree:
                mtf_conflict = True
        h_price = higher_result.get("price", 0.0)
        h_e144 = higher_result.get("last_e144")
        h_e169 = higher_result.get("last_e169")
        if h_price > 0 and h_e144 and h_e169:
            h_max_ema = max(h_e144, h_e169)
            h_min_ema = min(h_e144, h_e169)
            price = res["price"]
            if direction == "short" and price > h_max_ema:
                mtf_conflict = True
            elif direction == "long" and price < h_min_ema:
                mtf_conflict = True

    suppressed = False
    suppression_reason = ""
    if ai_sentiment == "BULLISH" and direction == "short":
        suppressed = True
        suppression_reason = "AI 宏觀輿情看多，過濾空單信號"
    elif ai_sentiment == "BEARISH" and direction == "long":
        suppressed = True
        suppression_reason = "AI 宏觀輿情看空，過濾多單信號"
    elif ai_sentiment == "NEUTRAL":
        if btc_state == "DIVERGENT":
            suppressed = True
            suppression_reason = "大盤 K 線技術面多空分歧，暫停開單"
        elif btc_state == "BEARISH" and direction == "long":
            suppressed = True
            suppression_reason = "大盤 K 線技術面破位偏空，過濾多單信號"
        elif btc_state == "BULLISH" and direction == "short":
            suppressed = True
            suppression_reason = "大盤 K 線技術面多頭強勢，過濾空單信號"

    if not suppressed and mtf_conflict:
        suppressed = True
        suppression_reason = "高週期趨勢衝突"
    elif not suppressed and direction == "short" and res.get("last_rsi") is not None and res["last_rsi"] > 58:
        suppressed = True
        suppression_reason = f"RSI 反彈過強 (RSI={res['last_rsi']:.1f})"

    return {
        "symbol": res["symbol"],
        "price": res["price"],
        "change24h": res["change24h"],
        "direction": res["direction"],
        "score": res["score"],
        "count_active": res["count_active"],
        "count_met": res["count_met"],
        "suppressed": suppressed,
        "suppression_reason": suppression_reason
    }


def compute_autoscan_results_cached(tf: str, btc_state: str, ai_sentiment: str):
    key = ("autoscan", tf, btc_state, ai_sentiment, tuple(COINS))
    cached = _cache_get(_AUTOSCAN_RESULTS_CACHE, _AUTOSCAN_RESULTS_CACHE_LOCK, key)
    if cached is not None:
        return cached

    higher_tf = get_higher_tf(tf)
    results = []
    max_workers = max(2, min(8, len(COINS)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_compute_autoscan_symbol, sym, tf, higher_tf, btc_state, ai_sentiment)
            for sym in COINS
        ]
        for f in as_completed(futures):
            try:
                row = f.result()
                if row:
                    results.append(row)
            except Exception as e:
                log.debug(f"[Web Server] autoscan symbol compute failed: {e}")

    _cache_set(_AUTOSCAN_RESULTS_CACHE, _AUTOSCAN_RESULTS_CACHE_LOCK, key, results, ttl_sec=5.0)
    return results


def _build_autoscan_payload(tf: str, btc_state: str, ai_sentiment: str):
    results = compute_autoscan_results_cached(tf, btc_state, ai_sentiment)
    min_conds = int(db_manager.get_setting("auto_trade_min_conditions", "3"))
    min_conf = int(db_manager.get_setting("auto_trade_confidence", "70"))
    results = sorted(
        results,
        key=lambda x: (
            x["direction"] != "neutral"
            and x.get("count_met", 0) >= min_conds
            and (x["score"] / 14 * 100) >= min_conf
            and not x.get("suppressed", False),
            x["score"],
        ),
        reverse=True,
    )
    return {
        "success": True,
        "results": results,
        "min_conditions": min_conds,
        "min_confidence": min_conf,
    }


def get_autoscan_payload(tf: str, btc_state: str, ai_sentiment: str):
    key = (tf, btc_state, ai_sentiment)
    with _AUTOSCAN_LATEST_PAYLOAD_LOCK:
        payload = _AUTOSCAN_LATEST_PAYLOAD.get(key)
    if payload:
        return payload
    payload = _build_autoscan_payload(tf, btc_state, ai_sentiment)
    with _AUTOSCAN_LATEST_PAYLOAD_LOCK:
        _AUTOSCAN_LATEST_PAYLOAD[key] = payload
    return payload


def warm_autoscan_payloads():
    try:
        ai_state = load_ai_sentiment_state()
        ai_sentiment = ai_state.get("sentiment", "NEUTRAL") if ai_state.get("auto_enabled", True) else "NEUTRAL"
        btc_state = BTC_MACRO_STATE.get("state", "NEUTRAL")
        timeframes = sorted(set([PRIMARY_TF] + list(SCAN_TIMEFRAMES or [])))
        with _AUTOSCAN_LATEST_PAYLOAD_LOCK:
            _AUTOSCAN_LATEST_PAYLOAD.clear()
        for tf in timeframes:
            payload = _build_autoscan_payload(tf, btc_state, ai_sentiment)
            with _AUTOSCAN_LATEST_PAYLOAD_LOCK:
                _AUTOSCAN_LATEST_PAYLOAD[(tf, btc_state, ai_sentiment)] = payload
    except Exception as e:
        log.warning(f"[Web Server] Warm autoscan payloads failed: {e}")

# Load DASHBOARD_HTML dynamically from templates/dashboard.html
def load_dashboard_html():
    template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"<h1>Error loading dashboard: {e}</h1>"

DASHBOARD_HTML = load_dashboard_html()

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def _reset_req_metric(self, method: str):
        self._req_method = method
        self._req_path = urlparse(self.path).path if self.path else ""
        self._req_started_at = time.time()
        self._req_metric_recorded = False

    def _commit_req_metric(self, status_code: int):
        if getattr(self, "_req_metric_recorded", False):
            return
        started = getattr(self, "_req_started_at", None)
        method = getattr(self, "_req_method", "")
        path = getattr(self, "_req_path", "")
        if started is None or not method or not path:
            return
        elapsed_ms = (time.time() - started) * 1000.0
        _record_api_metric(method, path, int(status_code), elapsed_ms)
        self._req_metric_recorded = True

    def log_message(self, format, *args):
        pass  # 靜音 http.server 日誌，防干擾 CLI 控制台

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self._commit_req_metric(code)

    def do_GET(self):
        self._reset_req_metric("GET")
        try:
            self._do_GET_impl()
        except (ConnectionError, OSError) as e:
            log.debug(f"  📡 [Web Server] 連線已被客戶端中止 (do_GET): {e}")
            self._commit_req_metric(499)
        except Exception as e:
            log.error(f"  📡 [Web Server] 處理 GET 請求時發生錯誤: {e}", exc_info=True)
            self._commit_req_metric(500)

    def _do_GET_impl(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        query  = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(load_dashboard_html().encode("utf-8"))
            self._commit_req_metric(200)
            return



        # ── API: AI 智能情緒策略狀態 ──
        if path == "/api/ai_sentiment_state":
            try:
                state = load_ai_sentiment_state()
                active_trades = load_ai_sentiment_active_trades_cached()
                
                scan_results = list(AI_SENTIMENT_SCAN_RESULTS.values())
                
                self.send_json({
                    "success": True,
                    "state": state,
                    "active_trades": active_trades,
                    "history": [],
                    "scan_results": scan_results,
                    "funnel": AI_SENTIMENT_FUNNEL
                })
            except Exception as e:
                log.error(f"Error in /api/ai_sentiment_state: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/metrics":
            self.send_json(_metrics_snapshot())
            return

        # ── API: 獲取 AI 智能情緒策略獨立歷史紀錄 (分頁支援) ──
        if path == "/api/ai_sentiment_history":
            try:
                page = int(query.get("page", ["1"])[0])
                limit = int(query.get("limit", ["10"])[0])
                if page < 1: page = 1
                if limit < 1: limit = 10
                offset = (page - 1) * limit

                with db_manager.get_db() as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    
                    # 總數
                    total_row = cursor.execute("SELECT COUNT(*) as total FROM historical_trades WHERE trade_type = 'AI_SENTIMENT_SIGNAL'").fetchone()
                    total = total_row["total"] if total_row else 0
                    
                    rows = cursor.execute("""
                        SELECT * FROM historical_trades 
                        WHERE trade_type = 'AI_SENTIMENT_SIGNAL' 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (limit, offset)).fetchall()
                    
                    data = []
                    for r in rows:
                        data.append({
                            "id": r["id"],
                            "time_str": r["time_str"],
                            "symbol": r["symbol"],
                            "direction": r["direction"],
                            "entry": r["entry"],
                            "sl": r["sl"],
                            "tp1": r["tp1"],
                            "tp2": r["tp2"],
                            "tp3": r["tp3"],
                            "tp4": r["tp4"],
                            "reach": r["reach"],
                            "note": r["note"],
                            "pnl": r["pnl"],
                            "logic": r["logic"],
                            "pionex_order_id": r["pionex_order_id"],
                            "close_reason_code": r["close_reason_code"] if "close_reason_code" in r.keys() else None,
                            "notify_sent": r["notify_sent"] if "notify_sent" in r.keys() else None,
                            "notify_error": r["notify_error"] if "notify_error" in r.keys() else None
                        })
                    
                    self.send_json({
                        "success": True,
                        "data": data,
                        "total": total,
                        "page": page,
                        "limit": limit
                    })
            except Exception as e:
                log.error(f"Error in /api/ai_sentiment_history: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return




        # ── API: 即時 / 模擬條件掃描 ──
        if path == "/api/scan":
            symbol = query.get("symbol", ["BTCUSDT"])[0]
            tf     = query.get("tf", ["4h"])[0]
            sim_time = query.get("simTime", [None])[0]

            try:
                end_ts = None
                if sim_time:
                    # 解析模擬時間
                    dt = parse_sim_time(sim_time)
                    # 時區轉換 UTC
                    tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                    dt_aware = dt.replace(tzinfo=tz_taipei)
                    end_ts = int(dt_aware.timestamp() * 1000)

                market_ttl = 30.0 if end_ts is not None else 3.0
                klines = cached_fetch_klines(symbol, tf, 200, end_time_ms=end_ts, ttl_sec=market_ttl)
                ticker = cached_fetch_ticker(symbol, ttl_sec=2.0)
                
                result = analyze_symbol(symbol, klines, ticker)
                
                # 計算該時間點的 BTC 大盤過濾狀態
                btc_macro = cached_analyze_btc_macro(end_ts)
                
                if result:
                    # 加入信號時效文本
                    result["expiry"] = calc_signal_expiry(tf, result["count_active"])
                    
                    # 判斷信號是否被大盤過濾
                    direction = result["direction"]
                    btc_state = btc_macro.get("state", "NEUTRAL")
                    suppressed = False
                    suppression_reason = ""
                    if btc_state == "DIVERGENT":
                        suppressed = True
                        suppression_reason = "大盤大週期偏空但短線反彈，多空方向衝突，暫停開單"
                    elif btc_state == "BEARISH" and direction == "long":
                        suppressed = True
                        suppression_reason = "大盤跌破關鍵支撐，過濾多單信號"
                    elif btc_state == "BULLISH" and direction == "short":
                        suppressed = True
                        suppression_reason = "大盤處於強勢多頭，過濾空單信號"
                        
                    result["suppressed"] = suppressed
                    result["suppression_reason"] = suppression_reason
                    result["btc_macro"] = btc_macro
                    
                    # 查詢此時間段內是否有對應的信號或用戶交易記錄 (用於圖表點位繪製)
                    trade_records = []
                    if sim_time:
                        with db_manager.get_db() as conn:
                            conn.row_factory = sqlite3.Row
                            time_prefix = sim_time[:16] # YYYY-MM-DD HH:MM
                            rows = conn.execute(
                                "SELECT * FROM historical_trades WHERE symbol = ? AND time_str LIKE ?",
                                (symbol, f"{time_prefix}%")
                            ).fetchall()
                            for r in rows:
                                trade_records.append(dict(r))
                    result["trade_records"] = trade_records
                    
                    self.send_json({"success": True, "result": result, "klines": klines})
                else:
                    self.send_json({"success": False, "error": "無法分析 K 線"}, 400)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        # ── API: K 線拉取與歷史擬合 ──
        if path == "/api/klines":
            symbol = query.get("symbol", ["BTCUSDT"])[0]
            tf     = query.get("tf", ["4h"])[0]
            sim_time = query.get("simTime", [None])[0]
            mock   = query.get("mock", ["false"])[0].lower() == "true"

            try:
                start_ts = None
                end_ts = None
                if sim_time:
                    dt = parse_sim_time(sim_time)
                    tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                    dt_aware = dt.replace(tzinfo=tz_taipei)
                    entry_ts = int(dt_aware.timestamp() * 1000)
                        
                    tf_ms = {
                        "1m": 60 * 1000,
                        "3m": 3 * 60 * 1000,
                        "5m": 5 * 60 * 1000,
                        "15m": 15 * 60 * 1000,
                        "30m": 30 * 60 * 1000,
                        "1h": 3600 * 1000,
                        "2h": 2 * 3600 * 1000,
                        "4h": 4 * 3600 * 1000,
                        "6h": 6 * 3600 * 1000,
                        "8h": 8 * 3600 * 1000,
                        "12h": 12 * 3600 * 1000,
                        "1d": 24 * 3600 * 1000,
                        "3d": 3 * 24 * 3600 * 1000,
                        "1w": 7 * 24 * 3600 * 1000,
                    }
                    interval_ms = tf_ms.get(tf.lower(), 4 * 3600 * 1000)
                    start_ts = entry_ts - 80 * interval_ms
                    end_ts = entry_ts + 120 * interval_ms
                    
                market_ttl = 30.0 if sim_time else 3.0
                klines = cached_fetch_klines(symbol, tf, 200, end_time_ms=end_ts, start_time_ms=start_ts, ttl_sec=market_ttl)

                self.send_json(klines)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # ── API: 自動多幣種掃描 ──
        if path == "/api/autoscan":
            tf = query.get("tf", [PRIMARY_TF])[0]
            btc_state = BTC_MACRO_STATE.get("state", "NEUTRAL")
            ai_state = load_ai_sentiment_state()
            ai_sentiment = ai_state.get("sentiment", "NEUTRAL") if ai_state.get("auto_enabled", True) else "NEUTRAL"
            payload = get_autoscan_payload(tf, btc_state, ai_sentiment)
            self.send_json(payload)
            return

        # ── API: 獲取 BTC 大盤分析數據 ──
        if path == "/api/btc_macro":
            sim_time = query.get("simTime", [None])[0]
            try:
                end_ts = None
                if sim_time:
                    dt = parse_sim_time(sim_time)
                    tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                    dt_aware = dt.replace(tzinfo=tz_taipei)
                    end_ts = int(dt_aware.timestamp() * 1000)
                
                btc_macro = cached_analyze_btc_macro(end_ts)
                self.send_json({"success": True, "btc_macro": btc_macro})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        # ── API: 獲取系統配置幣種與主週期 ──
        if path == "/api/coins":
            self.send_json({"success": True, "coins": COINS, "primary_tf": PRIMARY_TF})
            return

        # ── API: 獲取指定幣種當前市場行情 ──
        if path == "/api/ticker":
            symbol = query.get("symbol", ["BTCUSDT"])[0].upper()
            try:
                ticker = cached_fetch_ticker(symbol, ttl_sec=2.0)
                price_str = ticker.get("lastPrice", ticker.get("price", "0.0"))
                price = float(price_str) if price_str else 0.0
                change_str = ticker.get("priceChangePercent", "0.0")
                change = float(change_str) if change_str else 0.0
                
                # 動態計算週線水平支撐與阻力位，提供給手動開倉參考
                weekly_support = None
                weekly_resistance = None
                try:
                    klines_1w = cached_fetch_klines(symbol, "1w", limit=15, ttl_sec=10.0)
                    if klines_1w and len(klines_1w) >= 3:
                        lows_1w = [float(k[3]) for k in klines_1w]
                        highs_1w = [float(k[2]) for k in klines_1w]
                        closes_1w = [float(k[4]) for k in klines_1w]
                        weekly_supports = sorted(lows_1w[-8:])[:2] + [sum(closes_1w[-4:]) / 4]
                        weekly_resistances = sorted(highs_1w[-8:])[-2:]
                        
                        weekly_support = min(weekly_supports, key=lambda x: abs(price - x))
                        weekly_resistance = min(weekly_resistances, key=lambda x: abs(price - x))
                except Exception as e:
                    log.warning(f"  📡 [Web Server] 計算週線水平結構失敗 {symbol}: {e}")
                
                self.send_json({
                    "success": True,
                    "symbol": symbol,
                    "price": price,
                    "change": change,
                    "weekly_support": round(weekly_support, 4) if weekly_support else None,
                    "weekly_resistance": round(weekly_resistance, 4) if weekly_resistance else None
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        # ── API: 估算停利點位 (基於前高前低實體 + 斐波那契延伸位) ──
        if path == "/api/calc_tpsl":
            symbol = query.get("symbol", ["BTCUSDT"])[0]
            direction = query.get("direction", ["long"])[0]
            entry_str = query.get("entry", [None])[0]
            sl_str = query.get("sl", [None])[0]
            
            try:
                entry = float(entry_str) if entry_str else 0.0
                sl = float(sl_str) if sl_str else 0.0
                
                # Fetch 1H Klines to find structure & calculate ATR/Fib
                klines_1h = cached_fetch_klines(symbol, "1h", limit=100, ttl_sec=10.0)
                if not klines_1h or len(klines_1h) < 30:
                    self.send_json({"success": False, "error": "無法獲取足夠的 1H K 線進行結構計算"}, 400)
                    return
                    
                highs_1h = [float(k[2]) for k in klines_1h]
                lows_1h = [float(k[3]) for k in klines_1h]
                closes_1h = [float(k[4]) for k in klines_1h]
                
                swings = find_swings(highs_1h, lows_1h, 5)
                atr_vals = calc_atr(highs_1h, lows_1h, closes_1h, 14)
                atr = atr_vals[-1] if (atr_vals and atr_vals[-1] is not None) else (abs(entry) * 0.01)
                
                # If entry was 0 (market mode), use current last price
                if entry <= 0:
                    entry = closes_1h[-1]
                    
                calc_res = gen_smart_tpsl(entry, direction, swings, atr)
                tps = calc_res.get("tps", [])
                
                # If custom SL was provided, adjust TP1 to respect minimum RR or custom SL distance if valid
                sl_dist = abs(entry - sl) if sl > 0 else abs(entry - calc_res.get("sl", entry))
                if sl_dist > 0:
                    if direction == "long":
                        tps[0] = max(tps[0], entry + sl_dist * 0.75)
                        tps[1] = max(tps[1], entry + sl_dist * 1.4)
                        tps[2] = max(tps[2], entry + sl_dist * 2.2)
                        tps[3] = max(tps[3], entry + sl_dist * 3.5)
                    else:
                        tps[0] = min(tps[0], entry - sl_dist * 0.75)
                        tps[1] = min(tps[1], entry - sl_dist * 1.4)
                        tps[2] = min(tps[2], entry - sl_dist * 2.2)
                        tps[3] = min(tps[3], entry - sl_dist * 3.5)
                
                self.send_json({
                    "success": True,
                    "symbol": symbol,
                    "direction": direction,
                    "entry": entry,
                    "tps": [round(x, 6) for x in tps]
                })
            except Exception as e:
                log.error(f"  📡 [Web Server] 計算 TP/SL 失敗: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        # ── API: 獲取歷史開單與信號分析數據 ──
        if path == "/api/historical":
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["100"])[0])
            res = db_manager.get_historical_trades(page, limit)
            self.send_json(res)
            return

        # ── API: 獲取開單通知紀錄 ──
        if path == "/api/notifications":
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["10"])[0])
            res = db_manager.get_notifications(page, limit)
            self.send_json(res)
            return

        # ── API: 獲取幣安託管持倉 ──
        if path == "/api/binance/positions" or path == "/api/pionex/positions":
            try:
                page = int(query.get("page", ["1"])[0])
                limit = int(query.get("limit", ["10"])[0])
            except ValueError:
                page = 1
                limit = 10
            view_filter = (query.get("view", ["all"])[0] or "all").lower().strip()
            res = db_manager.get_active_positions(page, limit, view_filter=view_filter)
            if res.get("success") and "data" in res:
                btc_state = BTC_MACRO_STATE.get("state", "NEUTRAL")
                ai_sentiment_state = load_ai_sentiment_state()
                ai_sentiment = ai_sentiment_state.get("sentiment", "NEUTRAL")

                for pos in res["data"]:
                    if pos["status"] == "OPEN":
                        is_long = (pos["side"] == "BUY")
                        conflict = False
                        warning_msg = ""
                        
                        # 若持倉方向與 AI 智能輿情一致，視為共振無衝突
                        if is_long and ai_sentiment == "BULLISH":
                            conflict = False
                            warning_msg = "與 AI 宏觀輿情偏多共振"
                        elif not is_long and ai_sentiment == "BEARISH":
                            conflict = False
                            warning_msg = "與 AI 宏觀輿情偏空共振"
                        else:
                            # 檢查純 K 線技術面大盤指標衝突
                            if btc_state == "DIVERGENT":
                                conflict = True
                                warning_msg = "大盤 K 線技術面多空分歧，注意震盪風險"
                            elif btc_state == "BEARISH" and is_long:
                                conflict = True
                                warning_msg = "大盤 K 線技術面破位偏空，多單注意風險"
                            elif btc_state == "BULLISH" and not is_long:
                                conflict = True
                                warning_msg = "大盤 K 線技術面多頭強勢，空單注意風險"

                        pos["macro_conflict"] = conflict
                        pos["macro_warning"] = warning_msg
                        
                    # 計算自動平倉時間倒數
                    pos_time_str = pos.get("timestamp")
                    countdown_str = "-"
                    auto_close_enabled = pos.get("auto_close", 1) == 1
                    
                    if not auto_close_enabled:
                        countdown_str = "已停用"
                    elif pos_time_str and pos["status"] in ["OPEN", "PENDING_ORDER"]:
                        try:
                            created_dt = parse_sim_time(pos_time_str)
                            tz_taipei = timezone(timedelta(hours=TZ_OFFSET))
                            now_dt = datetime.now(tz_taipei)
                            created_dt = created_dt.replace(tzinfo=tz_taipei) if created_dt.tzinfo is None else created_dt
                            
                            expire_dt = created_dt + timedelta(hours=24)
                            if now_dt >= expire_dt:
                                if pos["status"] == "OPEN":
                                    last_price = pos.get("current_price") or 0.0
                                    entry_price = pos.get("entry_price") or 1.0
                                    is_long = (pos.get("side") == "BUY")
                                    is_in_profit = (last_price > entry_price) if is_long else (last_price < entry_price)
                                    current_tp_level = pos.get("current_tp_level", 0)
                                    
                                    if current_tp_level >= 1 or is_in_profit:
                                        countdown_str = "已逾期 (豁免平倉)"
                                    else:
                                        countdown_str = "已逾期 (即將平倉)"
                                else:
                                    countdown_str = "已逾期 (即將撤單)"
                            else:
                                diff = expire_dt - now_dt
                                diff_secs = int(diff.total_seconds())
                                hrs = diff_secs // 3600
                                mins = (diff_secs % 3600) // 60
                                countdown_str = f"{hrs}h {mins}m"
                        except Exception as e:
                            log.error(f"計算自動平倉倒數失敗: {e}")
                    else:
                        countdown_str = "已平倉結算"
                        
                    pos["auto_close_countdown"] = countdown_str
            self.send_json(res)
            return

        # ── API: 獲取當前背景活躍信號監控佇列 ──
        if path == "/api/active_trades":
            trades = load_active_trades()
            self.send_json({"success": True, "data": trades})
            return

        # ── API: 獲取幣安合約可用餘額 ──
        if path == "/api/binance/balance":
            res = db_manager.get_binance_futures_balance()
            self.send_json(res)
            return

        # ── API: 獲取幣安配置設定 ──
        if path == "/api/settings":
            api_key = db_manager.get_setting("binance_api_key")
            api_secret = db_manager.get_setting("binance_api_secret")
            mock_mode = db_manager.get_setting("binance_mock_mode", "true") == "true"
            
            auto_trade = db_manager.get_setting("auto_trade_enabled", "false") == "true"
            auto_fully_managed = db_manager.get_setting("auto_trade_fully_managed", "false") == "true"
            auto_confidence = int(db_manager.get_setting("auto_trade_confidence", "70"))
            auto_min_conds = int(db_manager.get_setting("auto_trade_min_conditions", "4"))
            auto_margin = float(db_manager.get_setting("auto_trade_margin", "20.0"))
            auto_leverage = int(db_manager.get_setting("auto_trade_leverage", "10"))
            auto_req_inds = db_manager.get_setting("auto_trade_required_indicators", "")
            auto_max_concurrent = int(db_manager.get_setting("auto_trade_max_concurrent", "3"))
            pos_check_int = int(db_manager.get_setting("position_check_interval", "10"))
            scan_int = int(db_manager.get_setting("scan_interval", "300"))
            
            auto_use_dynamic_atr = db_manager.get_setting("auto_trade_use_dynamic_atr", "false") == "true"
            auto_use_oi_cvd = db_manager.get_setting("auto_trade_use_oi_cvd", "false") == "true"
            auto_risk_pct = float(db_manager.get_setting("auto_trade_risk_pct", "1.0"))
            auto_max_margin_pct = float(db_manager.get_setting("auto_trade_max_margin_pct", "10.0"))
            auto_booster_enabled = db_manager.get_setting("auto_trade_booster_enabled", "false") == "true"

            telegram_bot_token = db_manager.get_setting("telegram_bot_token", BOT_TOKEN or "")
            telegram_chat_id = db_manager.get_setting("telegram_chat_id", str(CHAT_ID) if CHAT_ID is not None else "")
            
            pos_cols_config = db_manager.get_setting("pos_visible_columns", "")
            
            self.send_json({
                "success": True,
                "pionex_api_key": api_key,
                "binance_api_key": api_key,
                "pionex_api_secret_set": bool(api_secret),
                "binance_api_secret_set": bool(api_secret),
                "pionex_mock_mode": mock_mode,
                "binance_mock_mode": mock_mode,
                "auto_trade_enabled": auto_trade,
                "auto_trade_fully_managed": auto_fully_managed,
                "auto_trade_confidence": auto_confidence,
                "auto_trade_min_conditions": auto_min_conds,
                "auto_trade_margin": auto_margin,
                "auto_trade_leverage": auto_leverage,
                "auto_trade_required_indicators": auto_req_inds,
                "auto_trade_max_concurrent": auto_max_concurrent,
                "position_check_interval": pos_check_int,
                "scan_interval": scan_int,
                "auto_trade_use_dynamic_atr": auto_use_dynamic_atr,
                "auto_trade_risk_pct": auto_risk_pct,
                "auto_trade_max_margin_pct": auto_max_margin_pct,
                "auto_trade_use_oi_cvd": auto_use_oi_cvd,
                "auto_trade_booster_enabled": auto_booster_enabled,
                "telegram_bot_token": telegram_bot_token,
                "telegram_chat_id": telegram_chat_id,
            })
            return



        self.send_json({"success": False, "error": "Not Found"}, 404)

    def do_POST(self):
        self._reset_req_metric("POST")
        try:
            self._do_POST_impl()
        except (ConnectionError, OSError) as e:
            log.debug(f"  📡 [Web Server] 連線已被客戶端中止 (do_POST): {e}")
            self._commit_req_metric(499)
        except Exception as e:
            log.error(f"  📡 [Web Server] 處理 POST 請求時發生錯誤: {e}", exc_info=True)
            self._commit_req_metric(500)

    def _do_POST_impl(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 讀取 POST Body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        
        try:
            body_data = json.loads(body) if body else {}
        except Exception:
            body_data = {}


        if path == "/api/save_ai_sentiment_settings":
            try:
                auto_enabled = body_data.get("auto_enabled", "false")
                tg_notify_enabled = body_data.get("tg_notify_enabled", "false")
                margin = body_data.get("margin", "20.0")
                leverage = body_data.get("leverage", "10")
                max_concurrent = body_data.get("max_concurrent", "3")
                max_same_direction = body_data.get("max_same_direction", "2")
                reentry_cooldown_minutes = body_data.get("reentry_cooldown_minutes", "90")
                
                state = load_ai_sentiment_state()
                state["auto_enabled"] = "true" if auto_enabled in [True, "true", "True"] else "false"
                state["tg_notify_enabled"] = "true" if tg_notify_enabled in [True, "true", "True"] else "false"
                state["margin"] = str(margin)
                state["leverage"] = str(leverage)
                state["max_concurrent"] = str(max_concurrent)
                state["max_same_direction"] = str(max_same_direction)
                state["reentry_cooldown_minutes"] = str(reentry_cooldown_minutes)
                save_ai_sentiment_state(state)
                
                self.send_json({"success": True, "state": state})
            except Exception as e:
                log.error(f"Error in /api/save_ai_sentiment_settings: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/ai_sentiment/update":
            try:
                state, success = update_ai_sentiment_state()
                self.send_json({"success": success, "state": state})
            except Exception as e:
                log.error(f"Error in /api/ai_sentiment/update: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/ai_sentiment_active_trades/delete":
            try:
                key = body_data.get("key")
                if not key:
                    self.send_json({"success": False, "error": "缺少 key 參數"}, 400)
                    return
                
                active_file = "active_trades_ai_sentiment.json"
                removed_trade = None
                if os.path.exists(active_file):
                    try:
                        with open(active_file, "r", encoding="utf-8") as f:
                            trades = json.load(f)
                        if key in trades:
                            removed_trade = trades.get(key)
                            del trades[key]
                            with open(active_file, "w", encoding="utf-8") as f:
                                json.dump(trades, f, ensure_ascii=False, indent=2)
                            log.info(f"⚡ [AI Sentiment Active Trades] 用戶手動移出監控: {key}")
                    except Exception as fe:
                        self.send_json({"success": False, "error": f"刪除失敗: {fe}"}, 500)
                        return

                # 同步歷史狀態，避免 AI 訊號殘留在 PENDING
                try:
                    target_trade_id = None
                    if isinstance(removed_trade, dict):
                        db_id = removed_trade.get("db_id")
                        if isinstance(db_id, int) and db_id > 0:
                            target_trade_id = db_id

                    if target_trade_id is None:
                        parts = str(key).split("_")
                        if len(parts) >= 3:
                            symbol = parts[0].upper()
                            direction = parts[1].lower()
                            try:
                                ts = int(parts[-1])
                            except Exception:
                                ts = None

                            with db_manager.get_db() as conn:
                                if ts:
                                    dt_local = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                                    row = conn.execute(
                                        """
                                        SELECT id
                                        FROM historical_trades
                                        WHERE trade_type = 'AI_SENTIMENT_SIGNAL'
                                          AND symbol = ?
                                          AND direction = ?
                                          AND reach IN ('PENDING', 'OPEN')
                                          AND time_str <= ?
                                        ORDER BY time_str DESC
                                        LIMIT 1
                                        """,
                                        (symbol, direction, dt_local),
                                    ).fetchone()
                                else:
                                    row = conn.execute(
                                        """
                                        SELECT id
                                        FROM historical_trades
                                        WHERE trade_type = 'AI_SENTIMENT_SIGNAL'
                                          AND symbol = ?
                                          AND direction = ?
                                          AND reach IN ('PENDING', 'OPEN')
                                        ORDER BY time_str DESC
                                        LIMIT 1
                                        """,
                                        (symbol, direction),
                                    ).fetchone()
                                if row:
                                    target_trade_id = int(row["id"])

                    if target_trade_id is not None:
                        db_manager.update_historical_trade_status(trade_id=target_trade_id, reach="EXPIRED")
                        db_manager.append_historical_trade_event(
                            None,
                            "用戶手動移出 AI 背景監控佇列，狀態同步為 EXPIRED",
                            trade_id=target_trade_id,
                        )
                except Exception as sync_err:
                    log.error(f"同步 AI 歷史狀態失敗（key={key}）: {sync_err}")
                
                self.send_json({"success": True})
            except Exception as e:
                log.error(f"Error in /api/ai_sentiment_active_trades/delete: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/historical/stop_monitor":
            try:
                trade_id = body_data.get("id")
                if not trade_id:
                    self.send_json({"success": False, "error": "缺少 id 參數"}, 400)
                    return
                
                symbol = None
                direction = None
                with db_manager.get_db() as conn:
                    row = conn.execute(
                        "SELECT symbol, direction, trade_type FROM historical_trades WHERE id = ?",
                        (int(trade_id),),
                    ).fetchone()
                    if row:
                        symbol = row["symbol"]
                        direction = row["direction"]
                    conn.execute("UPDATE historical_trades SET reach = 'EXPIRED' WHERE id = ?", (trade_id,))
                    conn.commit()
                
                db_manager.append_historical_trade_event(None, "用戶手動停止背景監控，設定為已失效", trade_id=trade_id)

                # 同步移出標準監控佇列，避免佇列與歷史狀態不一致
                if symbol and direction:
                    try:
                        trades = load_active_trades()
                        queue_key = f"{str(symbol).upper()}_{str(direction).lower()}"
                        if queue_key in trades:
                            del trades[queue_key]
                            save_active_trades(trades)
                            log.info(f"⚡ [Active Trades] 停止監控同步移出佇列: {queue_key}")
                    except Exception as qe:
                        log.error(f"停止監控時清理 active_trades 失敗: {qe}")

                log.info(f"🛑 用戶手動停止歷史信號 ID {trade_id} 的背景監控")
                
                self.send_json({"success": True})
            except Exception as e:
                log.error(f"Error in /api/historical/stop_monitor: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/historical/repair_signal":
            try:
                trade_id = body_data.get("id")
                if not trade_id:
                    self.send_json({"success": False, "error": "缺少 id 參數"}, 400)
                    return
                res = db_manager.repair_pending_signal_status(int(trade_id))
                code = 200 if res.get("success") else 400
                self.send_json(res, code)
            except Exception as e:
                log.error(f"Error in /api/historical/repair_signal: {e}")
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        if path == "/api/settings":
            api_key = body_data.get("pionex_api_key", body_data.get("binance_api_key", ""))
            api_secret = body_data.get("pionex_api_secret", body_data.get("binance_api_secret", ""))
            mock_mode = body_data.get("pionex_mock_mode", body_data.get("binance_mock_mode", None))
            
            auto_trade = body_data.get("auto_trade_enabled")
            auto_fully_managed = body_data.get("auto_trade_fully_managed")
            auto_confidence = body_data.get("auto_trade_confidence")
            auto_min_conds = body_data.get("auto_trade_min_conditions")
            auto_margin = body_data.get("auto_trade_margin")
            auto_leverage = body_data.get("auto_trade_leverage")
            auto_req_indicators = body_data.get("auto_trade_required_indicators", None)
            auto_max_concurrent = body_data.get("auto_trade_max_concurrent")
            pos_check_interval = body_data.get("position_check_interval")
            scan_interval = body_data.get("scan_interval")
            
            auto_use_dynamic_atr = body_data.get("auto_trade_use_dynamic_atr")
            auto_use_oi_cvd = body_data.get("auto_trade_use_oi_cvd")
            auto_risk_pct = body_data.get("auto_trade_risk_pct")
            auto_max_margin_pct = body_data.get("auto_trade_max_margin_pct")
            auto_booster_enabled = body_data.get("auto_trade_booster_enabled")

            # 全域設定：僅在請求包含對應欄位時更新，避免託管設定儲存誤清 API / Telegram
            if "binance_api_key" in body_data or "pionex_api_key" in body_data:
                db_manager.save_setting("binance_api_key", api_key)
            if api_secret:
                db_manager.save_setting("binance_api_secret", api_secret)
            if "telegram_bot_token" in body_data:
                db_manager.save_setting("telegram_bot_token", str(body_data.get("telegram_bot_token") or ""))
            if "telegram_chat_id" in body_data:
                db_manager.save_setting("telegram_chat_id", str(body_data.get("telegram_chat_id") or ""))

            if mock_mode is not None:
                db_manager.save_setting("binance_mock_mode", "true" if mock_mode in [True, "true", "True"] else "false")
            
            if auto_trade is not None:
                db_manager.save_setting("auto_trade_enabled", "true" if auto_trade in [True, "true", "True"] else "false")
            if auto_fully_managed is not None:
                db_manager.save_setting("auto_trade_fully_managed", "true" if auto_fully_managed in [True, "true", "True"] else "false")
            if auto_confidence is not None:
                db_manager.save_setting("auto_trade_confidence", str(auto_confidence))
            if auto_min_conds is not None:
                db_manager.save_setting("auto_trade_min_conditions", str(auto_min_conds))
            if auto_margin is not None:
                db_manager.save_setting("auto_trade_margin", str(auto_margin))
            if auto_leverage is not None:
                db_manager.save_setting("auto_trade_leverage", str(auto_leverage))
            if auto_req_indicators is not None:
                db_manager.save_setting("auto_trade_required_indicators", str(auto_req_indicators))
            if auto_max_concurrent is not None:
                db_manager.save_setting("auto_trade_max_concurrent", str(auto_max_concurrent))
            if pos_check_interval is not None:
                db_manager.save_setting("position_check_interval", str(pos_check_interval))
            if scan_interval is not None:
                db_manager.save_setting("scan_interval", str(scan_interval))
                
            if auto_use_dynamic_atr is not None:
                db_manager.save_setting("auto_trade_use_dynamic_atr", "true" if auto_use_dynamic_atr in [True, "true", "True"] else "false")
            if auto_use_oi_cvd is not None:
                db_manager.save_setting("auto_trade_use_oi_cvd", "true" if auto_use_oi_cvd in [True, "true", "True"] else "false")
            if auto_risk_pct is not None:
                db_manager.save_setting("auto_trade_risk_pct", str(auto_risk_pct))
            if auto_max_margin_pct is not None:
                db_manager.save_setting("auto_trade_max_margin_pct", str(auto_max_margin_pct))
            if auto_booster_enabled is not None:
                db_manager.save_setting("auto_trade_booster_enabled", "true" if auto_booster_enabled in [True, "true", "True"] else "false")
                
            self.send_json({"success": True, "message": "設定儲存成功"})
            return

        if path == "/api/user_settings":
            key = body_data.get("key")
            val = body_data.get("value")
            if key:
                db_manager.save_setting(key, str(val))
                self.send_json({"success": True, "key": key, "value": val})
            else:
                self.send_json({"success": False, "error": "Missing key"}, 400)
            return

        if path == "/api/binance/order" or path == "/api/pionex/order":
            symbol = body_data.get("symbol")
            if symbol:
                symbol = _normalize(symbol)
            direction = body_data.get("direction")
            leverage = body_data.get("leverage")
            margin = body_data.get("margin")
            sl_price = body_data.get("sl_price")
            tps = body_data.get("tps")
            is_market = body_data.get("is_market", True)
            limit_price = body_data.get("limit_price")

            if not symbol or not direction or not leverage or not margin or not sl_price or not tps:
                self.send_json({"success": False, "error": "缺少必要下單參數"}, 400)
                return

            res = db_manager.place_binance_futures_order(
                symbol=symbol,
                direction=direction,
                leverage=leverage,
                margin=margin,
                sl_price=sl_price,
                tps=tps,
                is_market=is_market,
                limit_price=limit_price
            )
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/close" or path == "/api/pionex/close":
            position_id = body_data.get("position_id")
            if not position_id:
                self.send_json({"success": False, "error": "缺少持倉 ID"}, 400)
                return

            res = db_manager.close_binance_position(position_id)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/cancel_order":
            position_id = body_data.get("position_id")
            if not position_id:
                self.send_json({"success": False, "error": "缺少持倉 ID"}, 400)
                return

            res = db_manager.cancel_binance_limit_order(position_id)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/delete_position":
            position_id = body_data.get("position_id")
            if not position_id:
                self.send_json({"success": False, "error": "缺少持倉 ID"}, 400)
                return
            res = db_manager.delete_active_position(position_id)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/sync_position":
            position_id = body_data.get("position_id")
            if not position_id:
                self.send_json({"success": False, "error": "缺少持倉 ID"}, 400)
                return
            res = db_manager.sync_position_with_binance(position_id)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/toggle_auto_close":
            position_id = body_data.get("position_id")
            auto_close = body_data.get("auto_close")
            if not position_id or auto_close is None:
                self.send_json({"success": False, "error": "缺少持倉 ID 或設定參數"}, 400)
                return
            res = db_manager.update_position_auto_close(position_id, auto_close)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/binance/update_sl":
            position_id = body_data.get("position_id")
            new_sl = body_data.get("new_sl")
            if not position_id or new_sl is None:
                self.send_json({"success": False, "error": "缺少持倉 ID 或新止損價"}, 400)
                return
            try:
                new_sl = float(new_sl)
            except ValueError:
                self.send_json({"success": False, "error": "止損價格式不正確"}, 400)
                return
            res = db_manager.update_position_sl(position_id, new_sl)
            code = 200 if res["success"] else 400
            self.send_json(res, code)
            return

        if path == "/api/active_trades/delete":
            key = body_data.get("key")
            if not key:
                self.send_json({"success": False, "error": "缺少監控 Key"}, 400)
                return
            trades = load_active_trades()
            if key in trades:
                removed_trade = trades.get(key)
                del trades[key]
                save_active_trades(trades)

                # 與 AI 佇列刪除一致：同步 SIGNAL 為 EXPIRED，避免開單通知仍顯示「監控中」
                try:
                    symbol = None
                    direction = None
                    if isinstance(removed_trade, dict):
                        symbol = removed_trade.get("symbol")
                        direction = removed_trade.get("direction")
                    if not symbol or not direction:
                        parts = str(key).split("_")
                        if len(parts) >= 2:
                            symbol = parts[0]
                            direction = parts[1]
                    synced_id = db_manager.expire_pending_signal_on_queue_remove(
                        symbol,
                        direction,
                        event_text="用戶手動移出標準背景監控佇列，狀態同步為 EXPIRED",
                        trade_type="SIGNAL",
                    )
                    if synced_id:
                        log.info(f"⚡ [Active Trades] 已同步 SIGNAL#{synced_id} → EXPIRED (key={key})")
                except Exception as sync_err:
                    log.error(f"同步標準 SIGNAL 歷史狀態失敗（key={key}）: {sync_err}")

                self.send_json({"success": True, "message": "已成功移出監控佇列"})
            else:
                self.send_json({"success": False, "error": "找不到該監控條目"}, 404)
            return



        self.send_json({"success": False, "error": "Not Found"}, 404)

# ── 自動信號狀態校正與對齊 ──
# ── 導入持倉狀態監控與校正服務模組 ──
from monitor import (
    calibrate_pending_signals, sync_all_positions_with_binance_on_startup,
    PositionMonitorThread
)
class WebServerThread(threading.Thread):
    def __init__(self, host="127.0.0.1", port=5000):
        super().__init__()
        self.host = host
        self.port = port
        self.daemon = True
        
    def run(self):
        try:
            warm_thread = AutoScanCacheWarmThread()
            warm_thread.start()
            server = ThreadingHTTPServer((self.host, self.port), DashboardHTTPHandler)
            log.info(f"  📡 [Web Server] 啟動成功（Threaded），訪問地址: http://{self.host}:{self.port}")
            server.serve_forever()
        except Exception as e:
            log.error(f"  ❌ Web Server 啟動失敗: {e}")


class AutoScanCacheWarmThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        while True:
            try:
                warm_autoscan_payloads()
            except Exception as e:
                log.warning(f"[Web Server] Autoscan warm cycle failed: {e}")
            try:
                interval = int(db_manager.get_setting("web_autoscan_refresh_sec", "20"))
            except Exception:
                interval = 20
            time.sleep(max(5, min(interval, 120)))
