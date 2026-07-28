# 🧬 Antigravity Crypto Trading System

> 基於多重技術指標的加密貨幣掃描與自動交易系統，整合 Binance / Pionex 交易所 API，並提供即時 Web 儀表板與 Telegram 通知。

---

## 📌 功能特色

- **多指標掃描**：Vegas Channel、Fibonacci、Order Block、FVG、RSI、Bollinger Band、支撐壓力位等
- **BTC 總體趨勢過濾**：自動判斷大盤狀態，過濾反趨勢訊號
- **AI 情緒策略**：整合市場情緒（恐懼/貪婪指數）輔助方向判斷
- **自動交易**：支援 Binance 及 Pionex 現貨／合約下單、止盈止損管理
- **即時 Web 儀表板**：內建 HTTP Server，Port 5000，無需額外框架
- **Telegram 通知**：交易訊號、持倉更新、異常警報即時推播
- **SQLite 持久化**：所有設定、持倉、歷史交易記錄於本地資料庫

---

## ⚙️ 系統需求

| 項目 | 最低版本 |
|------|---------|
| Python | 3.10+ |
| 作業系統 | Windows 10 / macOS 12 / Ubuntu 20.04+ |

---

## 🚀 快速開始

### 1. 下載專案

```bash
git clone https://github.com/your-username/crypto-trading-system-pure.git
cd crypto-trading-system-pure
```

### 2. 建立虛擬環境（強烈建議）

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. 安裝依賴套件

```bash
pip install -r requirements.txt
```

### 4. 設定 API 金鑰

開啟 `config.py`，填入你的 Telegram Bot Token 與 Chat ID：

```python
BOT_TOKEN = "你的 Telegram Bot Token"
CHAT_ID   = 你的 Chat ID（數字）
```

> **如何取得 Telegram Bot Token？**
> 1. 在 Telegram 搜尋 `@BotFather`
> 2. 傳送 `/newbot` 並依指示建立
> 3. 複製取得的 Token

如果需要自動交易功能，請於啟動後前往 Web 儀表板（`http://localhost:5000`）的「設定」頁面，填入交易所 API Key。

### 5. 啟動系統

```bash
python scanner.py
```

啟動後開啟瀏覽器，前往：

```
http://localhost:5000
```

---

## 📁 專案結構

```
crypto-trading-system-pure/
├── scanner.py               # 主程式入口（掃描器 + Web 伺服器啟動）
├── config.py                # 全域設定（Token、幣種清單、掃描參數等）
├── db_manager.py            # SQLite 資料庫操作、交易所 API 呼叫
├── run_realtime.py          # 即時運行包裝器
├── requirements.txt         # Python 依賴套件清單
│
├── analyzers/               # 技術分析模組
│   ├── __init__.py
│   ├── strategy.py          # 主策略邏輯（多指標評分、方向判斷、TP/SL）
│   ├── indicators.py        # 技術指標計算（EMA、RSI、ATR、BB 等）
│   └── ai_sentiment.py      # AI 市場情緒分析模組
│
├── monitor/                 # 持倉監控模組
│   ├── __init__.py
│   ├── position_monitor.py  # 即時持倉追蹤與自動平倉邏輯
│   └── calibration.py       # 持倉校正與同步
│
├── server/                  # Web 儀表板伺服器
│   ├── __init__.py
│   └── web_server.py        # HTTP API + 儀表板路由
│
├── templates/
│   └── dashboard.html       # 前端儀表板頁面
│
├── logs/                    # 每日自動分檔日誌（自動建立）
├── trading_system.db        # SQLite 資料庫（自動建立）
├── active_trades.json       # 當前持倉狀態（自動建立）
└── ai_sentiment_state.json  # AI 情緒狀態快取（自動建立）
```

---

## ⚙️ 主要設定（`config.py`）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `COINS` | 16 個幣種 | 掃描的交易對清單（USDT 計價）|
| `PRIMARY_TF` | `4h` | 主要分析時框 |
| `SCAN_TIMEFRAMES` | `["1h", "4h"]` | 複合分析時框 |
| `SCAN_INTERVAL` | `60` 秒 | 掃描間隔 |
| `MIN_SCORE` | `5` | 觸發訊號的最低評分 |
| `MAX_CONCURRENT_TRADES` | `3` | 最大同時持倉數 |
| `WEB_PORT` | `5000` | 儀表板 Web Port |
| `TZ_OFFSET` | `8` | 時區偏移（台北 UTC+8）|

---

## 🖥️ Web 儀表板功能

啟動後前往 `http://localhost:5000`，可存取以下功能：

- **即時訊號面板**：查看目前掃描結果與評分
- **持倉管理**：查看當前持倉、手動平倉
- **設定頁面**：填入交易所 API Key、Telegram 設定
- **歷史紀錄**：查閱歷史交易績效
- **AI 情緒控制**：開啟/關閉 AI 情緒策略、調整參數

---

## 📡 支援的交易所

| 交易所 | 功能 |
|--------|------|
| **Binance** | 合約交易（USDⓈ-M Futures）|
| **Pionex** | 合約交易 |
| 無 API（僅掃描）| 純訊號掃描 + Telegram 通知 |

> ⚠️ **注意**：API Key 請開啟「交易」權限即可，**勿開啟提幣權限**，降低資安風險。

---

## 🔔 Telegram 通知設定

1. 建立 Telegram Bot（透過 `@BotFather`）
2. 取得你的 Chat ID（可對 `@userinfobot` 傳任意訊息取得）
3. 填入 `config.py` 的 `BOT_TOKEN` 與 `CHAT_ID`
4. 或啟動後於儀表板「設定」頁面填入（設定會儲存至資料庫）

---

## 🛑 常見問題

**Q：啟動時出現 `NameError: name 'TelegramCHAT_ID' is not defined`**
> A：請打開 `config.py`，將第 15 行的 `TelegramCHAT_ID` 替換為你的實際 Chat ID 數字，或改為 `""` 以停用 Telegram 功能。

**Q：掃描器啟動後沒有任何訊號**
> A：正常情況下需等待 1-2 個掃描週期（約 1-2 分鐘）才會出現第一批結果。若長時間無訊號，請檢查網路是否可連線至 Binance API。

**Q：如何新增掃描的幣種？**
> A：編輯 `config.py` 的 `COINS` 列表，加入 USDT 計價的交易對即可（例如 `"DOGEUSDT"`）。

**Q：自動交易功能如何啟用？**
> A：前往儀表板「設定」頁面，填入 API Key 並開啟「自動交易」開關。建議先在模擬模式（Mock Mode）下測試。

---

## ⚠️ 免責聲明

本系統僅供學術研究與技術探索使用。加密貨幣交易存在高度風險，過去績效不代表未來結果。使用者需自行承擔所有交易風險，作者不對任何資金損失負責。

---

## 📜 License

MIT License
