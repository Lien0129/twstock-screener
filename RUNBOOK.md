# 每日執行手冊

給「接手這個專案的下一個 session」看的。那個 session 沒有任何記憶，所以每一步都寫清楚。

## 環境事實（先讀，別重新試錯）

- **雲端容器沒有外網**（只有套件庫通）。Python 不能直接抓行情，`requests` 一律失敗。
- 雲端唯一的對外管道是 **WebFetch / WebSearch**。Yahoo 的 API 對 WebFetch 是 `ROBOTS_DISALLOWED`，抓不到。
- **真正的資料抓取全部在使用者的 Windows 電腦上跑**（`daily_update.py`），那裡 `requests` 正常。
- 專案位置：`C:\Users\User\Desktop\claude work\台股`，透過桌面版連線讀寫。
- 桌面橋接會頻繁斷線重連，斷了等 30~45 秒再試，不要連續重試。
- 儀表板網址固定，重新發布會覆蓋同一頁：`.../artifact/5c71b5f3-...`
- 使用手冊是另一個 Artifact：`.../artifact/9ee6c080-...`

---

## 路徑 A：使用者電腦（主要，每晚 20:30 自動跑）

`run_daily.bat` → `daily_update.py` → `src/refresh.py`

`daily_update.py` 的執行順序**有意義，不要調換**：

```
1. update_market_all()    全市場當日成交值（TWSE STOCK_DAY_ALL，一天一個請求）
2. rebalance_universe()   每月重排觀察池 ← 必須在抓價之前，重排完才知道要抓哪些股票
3. update_prices()        Yahoo 還原股價（歷史不足 200 根的自動改抓 2 年）
4. update_chips/margin()  三大法人、融資融券（可自動回補最多 15 個交易日）
5. update_macro()         費半、那斯達克、S&P、VIX、美元指數、美元兌台幣、美債 10Y
6. update_events()        處置股、注意股、現金增資、重大訊息
7. update_etf()           長期持有的 ETF 報價
8. (--monthly) 月營收
```

跑完自動做資料檢查，通過才重算訊號與本機儀表板。

```bash
python daily_update.py              # 收盤後，建議 18:00 之後
python daily_update.py --monthly    # 每月 10 號之後加這個，順便更新月營收
python daily_update.py --no-report  # 只更新資料，不重算
```

### 觀察池是動態的

名單**不在 `config.py` 裡寫死**。`src/universe_live.py` 每月最後一個交易日
依近 60 日日均成交值取前 100 名，聯集目前持股，寫進 `data/universe_current.json`；
`config.UNIVERSE` 開機時讀那個檔。讀不到才退回 `FALLBACK_UNIVERSE`（舊的 106 檔）。

- 基準日沒變就自動不動，每天跑不會有副作用
- 手動重排：`python src/universe_live.py`，加 `--force` 強制
- **剛進池子的股票必然缺籌碼與基本面歷史**（那些是逐日累積的，補不回來）。
  `verify_data` 對它們有 **45 天寬限期**，期內只提醒不擋；超過還缺才當成錯誤。

---

## 路徑 B：雲端（使用者電腦關著時的備援）

雲端抓不到 Yahoo，但**證交所 OpenAPI 可以用 WebFetch**。已驗證可用的端點：

| 端點 | 內容 |
|---|---|
| `/v1/exchangeReport/STOCK_DAY_ALL` | 全市場當日 OHLC + `TradeValue`（日期是民國 7 碼，如 `1150909`） |
| `/v1/announcement/punish` | 處置股 |
| `/v1/announcement/notice` | 注意股 |
| `/v1/opendata/t187ap04_L` | **重大訊息**（第 12 / 26 款 = 法說會） |
| `/v1/opendata/t187ap38_L` | 股東會 + 現金增資 |
| `/v1/opendata/t187ap03_L` | 上市公司清單 |

**`TWT48U` / `TWT49U`（除權息預告）是 404，不要再試。** 舊版文件寫過，那是錯的。

法人與融券日報（`T86`、`MI_MARGN`）整份 1,000 多檔丟給 WebFetch **會靜默漏資料**
（實測要 20 檔只回 7 檔，而且不會告訴你它漏了）。一定要按產業別分開抓，每份 40~100 筆。
收到後交給 `src/daily_ingest.py` 併入。

**重大訊息的坑**：這幾個條款類型的 `主旨` 欄位 API **一律是空的**，
內容只在 `說明` 裡。判斷法說會要用**條款代碼**（第 12 / 26 款），不能用字串比對主旨——
第一版就是這樣壞掉的，永遠 match 不到。

---

## 使用者說「更新」的時候做什麼

他的電腦每晚會自己更新，但**線上那份 Artifact 只能由 Claude 發布**。四個動作：

**1. 用 `device_stage_files` 把這些檔案從他電腦搬進雲端**（照原目錄結構）：

```
data/prices/twstock_history.csv      ← 同時複製一份到 data/twstock_history.csv
data/chips/twstock_chips.csv
data/twstock_macro.csv
data/etf/etf_history.csv
data/events/material.csv
data/events/punish.csv
data/market/stock_day_all.csv
data/universe_current.json           ← 別漏掉，漏了雲端會用舊名單
```

**2.** `python3 src/refresh.py` — 一行跑完資料檢查、圖表資料、訊號、儀表板

**3.** Artifact 工具發布 `output/dashboard.html`，**傳入既有網址**（不要建新的）

**4.** 口頭回報：資料日期、兩池各幾檔與前三名、持股建議、任何資料警告。
持股有觸發出場規則的話放最前面講。

---

## 使用者回報持股時

寫進 `portfolio/holdings.json`：

```json
{"holdings": [
  {"code": "2330", "name": "台積電", "sleeve": "position",
   "entry_date": "2026-08-25", "entry_price": 2380, "shares": 1000}
]}
```

- `sleeve`：`swing`（波段）或 `position`（長線），決定用哪一套出場規則
- `entry_price` 用實際成交均價，`shares` 是股數（一張 = 1000 股），零股就填實際股數
- **不在觀察池裡的股票不用改 `config.py`** ——`universe_live` 會自動把持股聯集進池子。
  寫完 `holdings.json` 後跑一次 `python src/universe_live.py --force` 就會納入，
  下次 `daily_update` 會自動補抓它的兩年歷史。
- 長期持有的 ETF 走 `portfolio/long_term.json`，**不套用選股系統的出場規則**——
  那套是為 1–4 週設計的，套在 3–5 年的計畫上只會叫你不停出場。

平倉後用 `src/trades.py` 記錄，**務必標記 `followed_rules`**：
沒照規則走的那幾筆要標 `false`，否則之後檢討會把「人為偏離」算進「規則的績效」。

---

## 資料檢查

```bash
python3 src/verify_data.py
```

回傳非空清單就代表有問題，儀表板會顯示黃色警告橫幅。
**不要因為警告就把它藏起來**——寧可讓使用者看到「今天資料不完整」，
也不要讓他拿到一份用舊資料算出來、看起來很正常的推薦。

會檢查：股價／籌碼／基本面／當沖的觀察池覆蓋率、重複列、
**個股資料落後**（某幾檔停在更早的日期 → 它們今天不會出現在訊號裡）、加權指數覆蓋率。

---

## 不要做的事

- **不要為了讓回測好看而反覆調參數。** 要改規則，先在樣本外驗證，而且驗證完不要再回頭改。
- **不要因為某個因子「聽起來合理」就加進評分。** 已經有 14 個直覺上很合理的東西被資料打臉，
  清單在 `README.md`。其中兩個是我自己提出、又被自己的數據推翻的。
- **不要把整份 `ALLBUT0999` 丟給 WebFetch**，它會靜默漏資料。
- **不要把「掉出榜單」講成賣出訊號。** 實測「掉出榜就賣」會讓波段池平均報酬
  從 +4.95% 掉到 −0.05%，賺 >20% 的比例從 14.1% 崩到 0.5%——大贏家幾乎都在第 1~2 天就掉出榜了。
  觀察池的「剔除」也一樣，那只代表成交值排名掉出前 100，跟技術面好壞無關。
- **不要在儀表板上寫「預測」「保證」。** 目標價是區間參考，而且 `swing_exit()` 裡根本沒有目標價條件。
- **不要略過「今天沒有訊號」的情況**——空手是正常結果，不要為了有東西交差而放寬條件。
- **不要給買賣建議。** 可以報系統的機械排名與事實，不做投資建議。
- **不要經手 API token。** 使用者給過 FinMind token 三次，三次都要拒絕；
  現在的流程完全不需要 token（證交所 OpenAPI 免註冊、Yahoo 走使用者電腦）。
- **刪除、覆蓋、重新命名任何檔案之前，先列出會變動的東西並等確認。**
  多步驟任務開始前先列步驟等確認。沒授權的資料夾不要碰。
