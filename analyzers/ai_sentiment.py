import os
import sys
import json
import re
import time
import requests
import xml.etree.ElementTree as ET
import subprocess
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

# Try importing log from config, fallback to print if config is not in path
try:
    from config import log
except ImportError:
    import logging
    log = logging.getLogger("ai_sentiment")

STATE_FILE = "ai_sentiment_state.json"
DEFAULT_STATE = {
    "sentiment": "NEUTRAL",
    "reason": "無初始狀態",
    "last_updated": "-",
    "auto_enabled": "false",
    "tg_notify_enabled": "false",
    "margin": "20.0",
    "leverage": "10",
    "max_concurrent": "3",
    "max_same_direction": "2",
    "reentry_cooldown_minutes": "90"
}

def load_ai_sentiment_state():
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE, f, indent=2, ensure_ascii=False)
        return DEFAULT_STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_STATE

def save_ai_sentiment_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

def get_latest_crypto_news():
    try:
        r = requests.get('https://www.coindesk.com/arc/outboundfeeds/rss/', timeout=10)
        if r.ok:
            root = ET.fromstring(r.content)
            items = root.findall('.//item')
            news_list = []
            for item in items[:5]: # Take top 5 news
                title = item.find('title').text
                description = item.find('description').text if item.find('description') is not None else ""
                news_list.append(f"- Title: {title}\n  Summary: {description}")
            return "\n".join(news_list)
    except Exception as e:
        return f"Error fetching Coindesk news: {e}"
    return "No Coindesk news found."

def get_jin10_news():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        r = requests.get('https://www.jin10.com/', headers=headers, timeout=10)
        if r.ok:
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, 'html.parser')
            text = soup.get_text()
            
            # Match HH:MM:SS followed by text until newline
            pattern = re.compile(r'(\d{2}:\d{2}:\d{2})([^\n]+)')
            matches = pattern.findall(text)
            
            news_items = []
            for time_str, content in matches:
                content_clean = content.strip()
                # Ignore noise and VIP promos
                if len(content_clean) > 15 and "下載" not in content_clean:
                    content_clean = re.sub(r'\s+', ' ', content_clean)
                    news_items.append(f"[{time_str}] {content_clean}")
            return "\n".join(news_items[:15])
    except Exception as e:
        return f"Error fetching Jin10 news: {e}"
    return "No Jin10 news found."

def get_fear_greed_index_text():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=3", timeout=5)
        if r.ok:
            data = r.json().get("data", [])
            out = ["=== CRYPTO FEAR & GREED INDEX ==="]
            for x in data:
                t = datetime.fromtimestamp(int(x['timestamp']), tz=timezone(timedelta(hours=8))).strftime('%m-%d')
                out.append(f"  [{t}] Value: {x['value']} ({x['value_classification']})")
            return "\n".join(out)
    except Exception as e:
        return f"Error fetching Fear & Greed index: {e}"
    return "No Fear & Greed index found."

def simple_ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0
    alpha = 2 / (period + 1)
    ema = values[0]
    for val in values[1:]:
        ema = val * alpha + ema * (1 - alpha)
    return ema

def get_us_stock_macro_text():
    try:
        import yfinance as yf
        # Fetch Nasdaq Futures (NQ=F) hourly candles
        nq = yf.Ticker("NQ=F").history(period="2d", interval="1h")
        # Fetch US Dollar Index (DX-Y.NYB) daily candles
        dxy = yf.Ticker("DX-Y.NYB").history(period="5d", interval="1d")
        
        out = ["=== US STOCK & MACRO REAL-TIME INDICATORS ==="]
        if not nq.empty:
            closes_nq = nq["Close"].tolist()
            ema21_nq = simple_ema(closes_nq, 21)
            last_nq = closes_nq[-1]
            nq_trend = "BULLISH (NQ Futures > 21-EMA) 📈" if last_nq > ema21_nq else "BEARISH (NQ Futures < 21-EMA) 📉"
            out.append(f"  Nasdaq Futures (NQ=F) Last: {last_nq:.2f} | 21-EMA: {ema21_nq:.2f} | Trend: {nq_trend}")
            
        if not dxy.empty:
            closes_dxy = dxy["Close"].tolist()
            last_dxy = closes_dxy[-1]
            prev_dxy = closes_dxy[-2] if len(closes_dxy) >= 2 else last_dxy
            dxy_dir = "RISING (Bearish for Crypto) 📈" if last_dxy > prev_dxy else "FALLING (Bullish for Crypto) 📉"
            out.append(f"  US Dollar Index (DXY) Last: {last_dxy:.4f} | Previous: {prev_dxy:.4f} | Trend: {dxy_dir}")
            
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching US stock macro: {e}"

def get_binance_futures_data_text(symbol):
    base_url = "https://fapi.binance.com"
    out = [f"=== BINANCE FUTURES DATA FOR {symbol.upper()} ==="]
    try:
        # 0. Funding Rate
        r = requests.get(f"{base_url}/fapi/v1/premiumIndex?symbol={symbol}", timeout=5)
        if r.ok and r.json():
            data = r.json()
            funding_rate = float(data.get("lastFundingRate", 0.0))
            out.append(f"Current Funding Rate: {funding_rate*100:.6f}% (Annualized: {funding_rate*100*3*365:.2f}%)")

        # 1. Global Long/Short Ratio
        r = requests.get(f"{base_url}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=3", timeout=5)
        if r.ok and r.json():
            data = r.json()
            out.append("Global L/S Account Ratio:")
            for x in data:
                t = datetime.fromtimestamp(x['timestamp']/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                out.append(f"  [{t}] Ratio: {x['longShortRatio']}, Long%: {float(x['longAccount'])*100:.2f}%, Short%: {float(x['shortAccount'])*100:.2f}%")
                
        # 2. Top Trader Accounts L/S
        r = requests.get(f"{base_url}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=1h&limit=3", timeout=5)
        if r.ok and r.json():
            data = r.json()
            out.append("Top Traders L/S Account Ratio:")
            for x in data:
                t = datetime.fromtimestamp(x['timestamp']/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                out.append(f"  [{t}] Ratio: {x['longShortRatio']}, Long%: {float(x['longAccount'])*100:.2f}%, Short%: {float(x['shortAccount'])*100:.2f}%")
                
        # 3. Top Trader Positions L/S
        r = requests.get(f"{base_url}/futures/data/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=3", timeout=5)
        if r.ok and r.json():
            data = r.json()
            out.append("Top Traders L/S Position Ratio:")
            for x in data:
                t = datetime.fromtimestamp(x['timestamp']/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                out.append(f"  [{t}] Ratio: {x['longShortRatio']}, Long%: {float(x['longAccount'])*100:.2f}%, Short%: {float(x['shortAccount'])*100:.2f}%")
                
        # 4. Taker Buy/Sell Volume Ratio
        r = requests.get(f"{base_url}/futures/data/takerlongshortRatio?symbol={symbol}&period=1h&limit=3", timeout=5)
        if r.ok and r.json():
            data = r.json()
            out.append("Taker Buy/Sell Volume Ratio:")
            for x in data:
                t = datetime.fromtimestamp(x['timestamp']/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                out.append(f"  [{t}] BuyVol: {float(x['buyVol']):,.2f}, SellVol: {float(x['sellVol']):,.2f}, Ratio: {x['buySellRatio']}")
                
        # 5. Open Interest History
        r = requests.get(f"{base_url}/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=3", timeout=5)
        if r.ok and r.json():
            data = r.json()
            out.append("Open Interest History:")
            for x in data:
                t = datetime.fromtimestamp(x['timestamp']/1000, tz=timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                out.append(f"  [{t}] OI (Amt): {float(x['sumOpenInterest']):,.2f}, OI (USDT): {float(x['sumOpenInterestValue']):,.2f}")
                
    except Exception as e:
        out.append(f"Error fetching Binance trading data: {e}")
    return "\n".join(out)
 
def analyze_sentiment_with_agy(news_text):
    prompt = (
        "你是一個資深的加密貨幣與總體經濟宏觀分析師。\n"
        "請仔細閱讀並分析以下最新的加密貨幣與總體經濟金融快訊（包含 Coindesk 與金十數據），加密恐懼與貪婪指數，以及幣安期貨大戶多空與持倉量統計數據（含資金費率）：\n\n"
        f"{news_text}\n\n"
        "請根據這些資訊評估當前對加密貨幣（特別是比特幣期貨）的整體市場宏觀情緒偏向（BULLISH / BEARISH / NEUTRAL）：\n"
        "1. 如果利多因素或地緣政治緩和、降息預期樂觀、美股及宏觀大勢偏多，或者幣安大戶持倉量顯著上升且多單佔優，請評估為 BULLISH。\n"
        "   特別注意：當市場價格探底，且伴隨【深度負值資金費率（Funding Rate < -0.005%）】或【加密恐懼與貪婪指數處於極度恐懼（Fear/Extreme Fear < 30）】時，空單高度擁擠，隨時可能發生【空頭擠壓（Short Squeeze）/ V型反彈】。在此狀況下即使新聞偏空，也應積極評估為 BULLISH（反向看漲機會）。\n"
        "2. 如果有嚴重的地緣政治緊張升級、加息恐慌、宏觀經濟衰退利空、重大監管打擊，或者期貨市場出現大規模空頭建倉、多单大戶退場，請評估為 BEARISH。\n"
        "   特別注意：當市場價格處於高位，且伴隨【恐懼與貪婪指數處於極度貪婪（Greed/Extreme Greed > 75）】時，市場可能過熱，存在【多頭擠壓（Long Squeeze）/ 快速拉回】風險，應積極評估為 BEARISH。\n"
        "3. 如果訊息多空交織或對加密市場無明顯影響，請評估為 NEUTRAL。\n\n"
        "請嚴格以 JSON 格式回傳，格式如下：\n"
        "{\n"
        '  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",\n'
        '  "high_confidence": true | false,\n'
        '  "reason": "你的簡短繁體中文分析原因摘要（50字以內，請結合宏觀、Fear/Greed 與幣安期貨數據）"\n'
        "}\n"
        "說明：\n"
        "- \"high_confidence\": 當利多/利空趨勢極度強烈、宏觀數據與大戶持倉強烈共振且極具勝算時，設為 true（許可進攻重倉加碼）；若訊號一般或有潛在風險，設為 false。\n"
        "注意：請只回傳 JSON 字串，不要回傳額外的 ```json ``` 標記或任何其他說明文字。"
    )
    
    def extract_json_object(raw_text):
        text = (raw_text or "").strip()
        if not text:
            raise ValueError("empty output")

        # 先嘗試擷取 fenced code block 內容
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()

        # 直接 parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 嘗試從混雜文字中抽取第一個可解析 JSON object
        decoder = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        raise ValueError("no valid JSON object found")

    last_error = "unknown error"
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            # Run agy with specific model, effort, and skip permissions flag
            res = subprocess.run(
                ["agy", "--model", "gemini-3.6-flash", "--effort", "low", "--print", prompt, "--dangerously-skip-permissions"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60
            )

            raw_stdout = (res.stdout or "").strip()
            raw_stderr = (res.stderr or "").strip()

            if res.returncode != 0:
                last_error = f"agy return code={res.returncode}; stderr={raw_stderr[:300]}"
            else:
                try:
                    parsed = extract_json_object(raw_stdout)
                    return {
                        "success": True,
                        "parsed": parsed,
                        "raw": raw_stdout
                    }
                except Exception as pe:
                    last_error = f"json parse failed: {pe}; stdout_head={raw_stdout[:300]}"

        except Exception as e:
            last_error = f"exception: {e}"

        if attempt < max_retries:
            time.sleep(1.2 * attempt)

    return {
        "success": False,
        "error": last_error
    }

def update_ai_sentiment_state():
    state = load_ai_sentiment_state()
    
    log.info("  📡 [AI Sentiment] 正在抓取 Coindesk 與金十數據最新快訊...")
    coindesk_news = get_latest_crypto_news()
    jin10_news = get_jin10_news()
    
    log.info("  📡 [AI Sentiment] 正在抓取 Alternative.me 恐懼與貪婪指數...")
    fng_index_text = get_fear_greed_index_text()
    
    log.info("  📡 [AI Sentiment] 正在抓取美股期貨與美元指數...")
    us_macro_text = get_us_stock_macro_text()
    
    log.info("  📡 [AI Sentiment] 正在抓取幣安期貨最新交易統計數據...")
    btc_futures_data = get_binance_futures_data_text("BTCUSDT")
    eth_futures_data = get_binance_futures_data_text("ETHUSDT")
    
    merged_news = (
        "=== COINDESK CRYPTO NEWS ===\n"
        f"{coindesk_news}\n\n"
        "=== JIN10 MACRO FINANCIAL NEWS ===\n"
        f"{jin10_news}\n\n"
        f"{fng_index_text}\n\n"
        f"{us_macro_text}\n\n"
        f"{btc_futures_data}\n\n"
        f"{eth_futures_data}"
    )
    
    log.info("  📡 [AI Sentiment] 正在調用 Antigravity CLI 進行綜合輿情分析...")
    agy_result = analyze_sentiment_with_agy(merged_news)
    if not agy_result.get("success"):
        # fallback：維持上一版可用狀態，避免短暫異常覆蓋既有情緒判斷
        log.error(f"  ❌ [AI Sentiment] AGY 分析失敗（已重試），沿用上次狀態。錯誤: {agy_result.get('error')}")
        return state, False

    try:
        parsed = agy_result.get("parsed", {})

        # Validate keys
        sentiment = str(parsed.get("sentiment", "NEUTRAL")).upper()
        if sentiment not in ["BULLISH", "BEARISH", "NEUTRAL"]:
            sentiment = "NEUTRAL"
        reason = str(parsed.get("reason", "分析失敗"))

        hc_raw = parsed.get("high_confidence", False)
        if isinstance(hc_raw, bool):
            high_confidence = hc_raw
        else:
            high_confidence = str(hc_raw).strip().lower() in ["true", "1", "yes", "y"]
        
        state["sentiment"] = sentiment
        state["reason"] = reason
        state["high_confidence"] = "true" if high_confidence else "false"
        
        # Taipei time
        tz_taipei = timezone(timedelta(hours=8))
        state["last_updated"] = datetime.now(tz_taipei).strftime("%Y-%m-%d %H:%M:%S")
        
        save_ai_sentiment_state(state)
        log.info(f"  ✅ [AI Sentiment] 分析成功！情緒: {sentiment}, 高信心: {high_confidence}, 原因: {reason}")
        
        # 發送 Telegram 通知 (僅當用戶勾選開啟 TG 通知時發送)
        if state.get("tg_notify_enabled", "false") == "true":
            try:
                from config import send_telegram
                emoji = "🚀 [看多 BULLISH]" if sentiment == "BULLISH" else ("📉 [看空 BEARISH]" if sentiment == "BEARISH" else "⚖️ [中性 NEUTRAL]")
                conf_str = "🔥 極高信心 (許可重倉加碼 2.5x)" if high_confidence else "⚡ 一般信心 (標準倉位)"
                tg_msg = (
                    f"🧠 <b>[AI 宏觀輿情狀態更新]</b>\n"
                    f"{'─' * 28}\n"
                    f"📅 時間: <b>{state['last_updated']}</b> (UTC+8)\n"
                    f"📊 市場情緒偏向: <b>{emoji}</b>\n"
                    f"🎯 進攻信號評估: <b>{conf_str}</b>\n"
                    f"{'─' * 28}\n"
                    f"💡 <b>分析原因摘要：</b>\n"
                    f"<i>{reason}</i>\n"
                    f"{'─' * 28}"
                )
                send_telegram(tg_msg)
            except Exception as tg_err:
                log.error(f"發送 AI 輿情 TG 通知失敗: {tg_err}")
        else:
            log.info("  ℹ️ [AI Sentiment] 用戶未勾選 TG 通知，跳過發送 Telegram 訊息。")
            
        return state, True
    except Exception as e:
        log.error(f"  ❌ [AI Sentiment] AGY 回傳解析失敗，沿用上次狀態。錯誤: {e}")
        return state, False
