import os
import json
import requests
import re
import html
# -*- coding: utf-8 -*-
"""
🧬 Antigravity Trading System Configuration
"""
import logging
from datetime import datetime

# ── Telegram Notification Configuration ──
BOT_TOKEN = "TelegramTOKEN"
CHAT_ID   = TelegramCHAT_ID


# ── Scan Configuration ──
COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "SOLUSDT", "TRXUSDT", "DOTUSDT", "LINKUSDT", "JTOUSDT",
    # 推薦追蹤的高趨勢性與高波動性幣種
    "NEARUSDT", "AVAXUSDT", "FTMUSDT", "TIAUSDT", "UNIUSDT", "WIFUSDT"
]

PRIMARY_TF     = "4h"
SCAN_TIMEFRAMES = ["1h", "4h"]
SCAN_INTERVAL  = 60            # Default scan interval (seconds)
MIN_CONDITIONS = 3             
MIN_SCORE      = 5             
MAX_CONCURRENT_TRADES = 3      
REQUEST_DELAY  = 0.2           
TZ_OFFSET      = 8             # Taipei UTC+8
WEB_PORT       = 5000          
ACTIVE_TRADES_FILE = "active_trades.json"

# BTC Macro state filter cache
BTC_MACRO_STATE = {
    "price": 0.0,
    "change": 0.0,
    "state": "NEUTRAL",
    "filter_rule": "⚡ 允許雙向交易",
    "analysis_text": "正在載入大盤聯動數據...",
    "levels": [],
    "confluences": []
}

# ── Unified Date Parser Helper ──
def parse_sim_time(sim_time_str):
    if not sim_time_str:
        return None
    val = sim_time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            pass
    raise ValueError(f"無法解析時間字串: '{sim_time_str}'")

# ── Logging Configuration (按日期分檔存放於 logs/ 資料夾) ──
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# 依日期格式生成今日 Log 路徑
today_str = datetime.now().strftime("%Y-%m-%d")
daily_log_path = os.path.join(LOGS_DIR, f"scanner_{today_str}.log")

from logging.handlers import TimedRotatingFileHandler

file_handler = TimedRotatingFileHandler(
    filename=daily_log_path,
    when="midnight",
    interval=1,
    backupCount=90,  # 保留 90 天歷史日誌
    encoding="utf-8"
)
file_handler.suffix = "%Y-%m-%d"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        file_handler,
    ],
)
log = logging.getLogger("scanner")

def _normalize(symbol: str) -> str:
    s = symbol.upper().strip()
    return s if s.endswith("USDT") else s + "USDT"


# ── Persistent Trade State Management ──
def load_active_trades() -> dict:
    if os.path.exists(ACTIVE_TRADES_FILE):
        try:
            with open(ACTIVE_TRADES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"讀取 {ACTIVE_TRADES_FILE} 失敗: {e}")
    return {}

def save_active_trades(trades: dict):
    try:
        with open(ACTIVE_TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"寫入 {ACTIVE_TRADES_FILE} 失敗: {e}")

def load_active_trades_ai_sentiment() -> dict:
    ai_file = "active_trades_ai_sentiment.json"
    if os.path.exists(ai_file):
        try:
            with open(ai_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"讀取 {ai_file} 失敗: {e}")
    return {}

def save_active_trades_ai_sentiment(trades: dict):
    ai_file = "active_trades_ai_sentiment.json"
    try:
        with open(ai_file, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"寫入 {ai_file} 失敗: {e}")

def remove_active_trade_by_symbol_and_direction(symbol: str, direction: str):
    try:
        # 1. 清除標準 active_trades.json 紀錄
        trades = load_active_trades()
        keys_to_delete = []
        target_dir = direction.lower() # 'long' 或 'short'
        target_sym = symbol.upper().strip()
        
        for key, trade in trades.items():
            if trade.get("symbol", "").upper().strip() == target_sym and trade.get("direction", "").lower() == target_dir:
                keys_to_delete.append(key)
                
        if keys_to_delete:
            for k in keys_to_delete:
                del trades[k]
            save_active_trades(trades)
            log.info(f"⚡ [Active Trades] 已自動將結束持倉之信號移出監控佇列: {keys_to_delete}")
            
        # 2. 清理 AI 智能情緒策略之 active_trades_ai_sentiment.json 檔
        ai_file = "active_trades_ai_sentiment.json"
        if os.path.exists(ai_file):
            try:
                with open(ai_file, "r", encoding="utf-8") as f:
                    ai_trades = json.load(f)
                ai_keys = []
                for key, trade in ai_trades.items():
                    if trade.get("symbol", "").upper().strip() == target_sym and trade.get("direction", "").lower() == target_dir:
                        ai_keys.append(key)
                if ai_keys:
                    for k in ai_keys:
                        del ai_trades[k]
                    with open(ai_file, "w", encoding="utf-8") as f:
                        json.dump(ai_trades, f, ensure_ascii=False, indent=2)
                    log.info(f"⚡ [Active Trades AI Sentiment] 已自動將結束持倉之信號移出監控佇列: {ai_keys}")
            except Exception as ae:
                log.error(f"清理 {ai_file} 失敗: {ae}")
                
    except Exception as e:
        log.error(f"自動移出監控佇列失敗: {e}")



# ── Telegram Notification Helper ──
_TG_ALLOWED_HTML_TAGS = [
    "b", "/b", "i", "/i", "u", "/u", "s", "/s",
    "code", "/code", "pre", "/pre", "tg-spoiler", "/tg-spoiler",
    "br", "br/"
]

def _sanitize_telegram_html(message: str) -> str:
    # 先全量轉義，確保動態資料中的 < > 不會破壞 Telegram HTML parser
    safe = html.escape(str(message), quote=False)
    for tag in _TG_ALLOWED_HTML_TAGS:
        safe = safe.replace(f"&lt;{tag}&gt;", f"<{tag}>")
    return safe

def _get_telegram_runtime_config():
    """Prefer DB settings (dashboard 全域設定); fall back to config.py defaults."""
    token = BOT_TOKEN
    chat_id = str(CHAT_ID) if CHAT_ID is not None else ""
    try:
        from db_manager import get_setting
        token = get_setting("telegram_bot_token", "") or token
        chat_id = get_setting("telegram_chat_id", "") or chat_id
    except Exception:
        pass
    return token, chat_id


def send_telegram(message: str) -> bool:
    """直連 Telegram Bot API 發送訊息，兼具 HTML 轉義與純文字重試備援機制"""
    bot_token, chat_id = _get_telegram_runtime_config()
    if not bot_token or not chat_id:
        log.warning("⚠️ 未配置 Telegram BOT_TOKEN 或 CHAT_ID，取消發送")
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_message = _sanitize_telegram_html(message)
    payload = {"chat_id": chat_id, "text": html_message, "parse_mode": "HTML"}
    
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.ok:
            log.info("  📤 Telegram 直連通知成功發送 ✅")
            return True
            
        log.error(f"  ❌ Bot API 回應失敗: HTTP {r.status_code}, body={r.text}")
        # HTML 解析失敗時，改用純文字重試備援，避免關鍵交易通知遺失
        err_body = (r.text or "").lower()
        if "can't parse entities" in err_body or "bad request" in err_body:
            plain_message = re.sub(r"</?[a-zA-Z][^>]*>", "", html_message)
            retry_payload = {"chat_id": chat_id, "text": plain_message}
            r2 = requests.post(url, json=retry_payload, timeout=12)
            if r2.ok:
                log.warning("  ⚠️ Telegram HTML 解析失敗，已以純文字重送成功 ✅")
                return True
            log.error(f"  ❌ Telegram 純文字重送失敗: HTTP {r2.status_code}, body={r2.text}")
    except Exception as e:
        log.error(f"  ❌ Bot API 發送異常: {e}")
        
    return False


def fetch_klines(symbol: str, interval: str, limit: int = 300, end_time_ms: int = None, start_time_ms: int = None) -> list:
    sym = _normalize(symbol)
    for base in [
        "https://fapi.binance.com/fapi/v1",
        "https://api.binance.com/api/v3",
    ]:
        try:
            params = {"symbol": sym, "interval": interval, "limit": limit}
            if end_time_ms:
                params["endTime"] = end_time_ms
            if start_time_ms:
                params["startTime"] = start_time_ms
            r = requests.get(f"{base}/klines", params=params, timeout=12)
            if r.ok:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    return data
        except Exception:
            pass
    return []

def fetch_ticker(symbol: str) -> dict:
    sym = _normalize(symbol)
    for base, path in [
        ("https://fapi.binance.com/fapi/v1", "/ticker/24hr"),
        ("https://api.binance.com/api/v3",   "/ticker/24hr"),
    ]:
        try:
            r = requests.get(f"{base}{path}", params={"symbol": sym}, timeout=10)
            if r.ok:
                return r.json()
        except Exception:
            pass
    return {}



