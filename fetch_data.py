"""台股資料擷取腳本 — 在你自己的電腦上執行。

為什麼要有這支腳本
------------------
雲端環境沒有對外網路，瀏覽器管道又受限於跨網域政策、下載權限與分頁記憶體，
大量歷史資料靠瀏覽器搬很容易失敗。這支腳本在你自己的 Windows 上跑，
直接連網抓完寫進專案資料夾，斷線可以續跑。

需要準備
--------
1. Python 3.9 以上
2. pip install requests pandas
3. 在這支腳本的同一層建立 finmind_token.txt，裡面只放你的 FinMind token（一行）
   或設定環境變數 FINMIND_TOKEN
   注意：token 不要放進版本控制，也不要貼給任何人（包含 AI 助理）

執行方式
--------
    python fetch_data.py                # 抓全部（第一次跑）
    python fetch_data.py --only prices  # 只抓某一類
    python fetch_data.py --resume       # 只補「輸出檔裡還沒有」的股票（可安全重複執行）

抓完會產生／更新：
    data/prices/twstock_history.csv        股價（含還原股價，來源 Yahoo）
    data/twstock_macro.csv                 加權指數與國際指標（來源 Yahoo）
    data/chips/twstock_chips.csv           三大法人與融資券（來源 FinMind）
    data/daytrading/twstock_daytrading.csv 當沖（來源 FinMind）
    data/fundamentals/twstock_fundamentals.csv  月營收／財報／本益比（來源 FinMind）

已存在的檔案會用「合併去重」的方式更新，不會直接覆蓋你原本的資料。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import pandas as pd
    import requests
except ImportError:
    sys.exit("請先安裝套件：pip install requests pandas")

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "_cache")

PRICE_START = "2024-08-01"      # 股價與籌碼起點
FUND_START = "2023-01-01"       # 月營收要多抓一年才能算年增率
YAHOO_RANGE = "2y"

# 觀察池：成交值前 100 大 + 原本就在追蹤、但掉出榜外的 6 檔
UNIVERSE = [
    "2330", "2408", "2454", "2327", "3037", "2344", "2303", "4958", "3481", "2308",
    "8046", "3711", "3189", "1303", "6770", "2337", "2383", "2317", "2449", "3008",
    "3443", "3017", "3231", "6213", "3661", "2409", "6669", "2313", "2382", "3026",
    "6239", "2345", "2368", "2301", "3665", "8150", "8039", "2059", "3042", "3653",
    "2360", "7769", "6139", "1326", "6446", "6271", "6442", "2357", "2379", "2634",
    "2484", "3450", "2492", "2603", "2891", "6531", "2882", "2324", "3034", "3006",
    "3532", "1301", "2881", "2404", "6515", "1802", "2376", "3167", "2481", "2455",
    "6451", "6257", "2609", "2472", "2377", "6505", "2356", "3533", "8996", "2486",
    "6805", "1717", "2887", "2412", "2618", "3036", "6285", "2615", "8261", "2464",
    "2395", "2451", "3406", "2441", "8033", "6415", "2375", "8028", "2885", "6672",
    "1216", "2002", "2207", "5274", "8069", "1590",
]

MACRO = {
    "^TWII": "TWII", "^SOX": "SOX", "^IXIC": "IXIC", "^GSPC": "GSPC",
    "^VIX": "VIX", "DX-Y.NYB": "DXY", "TWD=X": "USDTWD", "^TNX": "US10Y",
}

FIN_KEEP = {"Revenue", "GrossProfit", "OperatingIncome", "EPS",
            "IncomeAfterTaxes", "EquityAttributableToOwnersOfParent"}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


# ------------------------------------------------------------------ 基礎工具
def load_token() -> str | None:
    """token 只從本機讀，永遠不寫進輸出檔、不印在畫面上。"""
    env = os.environ.get("FINMIND_TOKEN")
    if env:
        return env.strip()
    path = os.path.join(ROOT, "finmind_token.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
            return tok or None
    return None


def ensure_dirs():
    for d in ["prices", "chips", "fundamentals", "daytrading", "_cache"]:
        os.makedirs(os.path.join(DATA, d), exist_ok=True)


def done_codes(path: str) -> set[str]:
    """已完成的股票 = 輸出檔裡真的有資料的股票。

    不用獨立的快取檔記錄進度：快取會說謊——如果程式在「標記完成」之後、
    「寫檔」之前被中斷，那些股票會被永久跳過。直接看檔案內容就不會有這個問題。
    """
    if not os.path.exists(path):
        return set()
    try:
        col = pd.read_csv(path, usecols=["code"], dtype={"code": str})
        return set(col["code"].dropna().astype(str))
    except Exception:
        return set()


FLUSH_EVERY = 10   # 每抓幾檔就落地一次


def fundamentals_done(path: str, datasets: set[str]) -> set[str]:
    """基本面是三個資料集共用一個檔案，要三者都有才算完成。"""
    if not os.path.exists(path):
        return set()
    try:
        d = pd.read_csv(path, usecols=["dataset", "code"], dtype={"code": str})
    except Exception:
        return set()
    have = d.groupby("code")["dataset"].apply(set)
    return {c for c, s in have.items() if datasets <= s}


def merge_csv(path: str, new: pd.DataFrame, keys: list[str], quiet: bool = False):
    """跟既有檔案合併去重，不直接覆蓋。新資料優先。"""
    if new.empty:
        if not quiet:
            print(f"    (沒有新資料，{os.path.basename(path)} 不變)")
        return
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"code": str})
        if "code" in new.columns:
            new["code"] = new["code"].astype(str)
        before = len(old)
        combined = pd.concat([old, new], ignore_index=True)
        combined = combined.drop_duplicates(subset=keys, keep="last")
        combined.to_csv(path, index=False)
        if not quiet:
            print(f"    {os.path.basename(path)}：{before} → {len(combined)} 筆")
    else:
        new.to_csv(path, index=False)
        if not quiet:
            print(f"    {os.path.basename(path)}：新建 {len(new)} 筆")


# ------------------------------------------------------------------ Yahoo 股價
def yahoo_chart(symbol: str, rng: str = YAHOO_RANGE) -> list[dict]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": "1d", "events": "div,split"}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code != 200:
                time.sleep(1.5 * (attempt + 1))
                continue
            res = r.json().get("chart", {}).get("result")
            if not res or not res[0].get("timestamp"):
                return []
            res = res[0]
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            out = []
            for i, ts in enumerate(res["timestamp"]):
                if q["close"][i] is None:
                    continue
                out.append({
                    "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                    "open": q["open"][i], "high": q["high"][i],
                    "low": q["low"][i], "close": q["close"][i],
                    "volume": q["volume"][i],
                    "adjclose": adj[i] if adj else q["close"][i],
                })
            return out
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return []


def fetch_prices():
    print("\n[1/5] 股價（Yahoo，不需要 token）")
    rows = []
    for i, code in enumerate(UNIVERSE, 1):
        got = None
        for sfx in (".TW", ".TWO"):
            data = yahoo_chart(code + sfx)
            if data:
                got = (sfx[1:], data)
                break
        if not got:
            print(f"    ✗ {code} 抓不到")
            continue
        market, data = got
        for d in data:
            rows.append({"code": code, "market": market, **d})
        if i % 20 == 0:
            print(f"    ...{i}/{len(UNIVERSE)}")
        time.sleep(0.15)
    df = pd.DataFrame(rows)
    if not df.empty:
        for c in ["open", "high", "low", "close", "adjclose"]:
            df[c] = df[c].round(2)
    merge_csv(os.path.join(DATA, "prices", "twstock_history.csv"), df, ["code", "date"])


def fetch_macro():
    print("\n[2/5] 加權指數與國際指標（Yahoo）")
    rows = []
    for sym, name in MACRO.items():
        data = yahoo_chart(sym)
        for d in data:
            rows.append({"symbol": name, "date": d["date"], "open": d["open"],
                         "high": d["high"], "low": d["low"], "close": d["close"],
                         "volume": d["volume"]})
        print(f"    {name}: {len(data)} 筆")
        time.sleep(0.2)
    merge_csv(os.path.join(DATA, "twstock_macro.csv"), pd.DataFrame(rows), ["symbol", "date"])


# ------------------------------------------------------------------ FinMind
class FinMind:
    """FinMind 取數器。token 只存在記憶體裡，不會被寫出去。"""

    BASE = "https://api.finmindtrade.com/api/v4/data"

    QUOTA_STOP_AFTER = 2   # 連續幾檔完全拿不到資料就判定額度用盡，停止該類別

    def __init__(self, token: str | None):
        self.token = token
        self.calls = 0
        self.consecutive_quota_fails = 0

    def get(self, dataset: str, data_id: str, start: str) -> list[dict] | None:
        params = {"dataset": dataset, "data_id": data_id, "start_date": start}
        if self.token:
            params["token"] = self.token
        for attempt in range(5):
            try:
                r = requests.get(self.BASE, params=params, timeout=30)
                self.calls += 1
                if r.status_code in (402, 429):
                    wait = 15 * (attempt + 1)
                    print(f"    額度限制，等 {wait} 秒後重試（{dataset}/{data_id}）")
                    time.sleep(wait)
                    continue
                j = r.json()
                if j.get("status") == 200:
                    self.consecutive_quota_fails = 0
                    return j.get("data", [])
                time.sleep(2)
            except Exception:
                time.sleep(3)
        self.consecutive_quota_fails += 1
        return None

    @property
    def quota_exhausted(self) -> bool:
        return self.consecutive_quota_fails >= self.QUOTA_STOP_AFTER


def fetch_chips(fm: FinMind, resume: bool):
    print("\n[3/5] 三大法人與融資券（FinMind）")
    out = os.path.join(DATA, "chips", "twstock_chips.csv")
    done = done_codes(out) if resume else set()
    todo = [c for c in UNIVERSE if c not in done]
    print(f"    已完成 {len(done)} 檔，待抓 {len(todo)} 檔")
    rows = []
    for i, code in enumerate(todo, 1):
        if fm.quota_exhausted:
            print(f"    額度用盡，停在第 {i-1}/{len(todo)} 檔。等一小時後重跑同一行指令即可續抓。")
            break
        rec: dict[str, dict] = {}
        inst = fm.get("TaiwanStockInstitutionalInvestorsBuySell", code, PRICE_START)
        if inst:
            for r in inst:
                d = rec.setdefault(r["date"], {"f": 0, "t": 0, "dl": 0, "mb": "", "sb": ""})
                net = r["buy"] - r["sell"]
                if r["name"] in ("Foreign_Investor", "Foreign_Dealer_Self"):
                    d["f"] += net
                elif r["name"] == "Investment_Trust":
                    d["t"] += net
                else:
                    d["dl"] += net
        mar = fm.get("TaiwanStockMarginPurchaseShortSale", code, PRICE_START)
        if mar:
            for r in mar:
                d = rec.setdefault(r["date"], {"f": 0, "t": 0, "dl": 0, "mb": "", "sb": ""})
                d["mb"] = r["MarginPurchaseTodayBalance"]
                d["sb"] = r["ShortSaleTodayBalance"]
        for date in sorted(rec):
            v = rec[date]
            rows.append({"code": code, "date": date, "foreign_net": v["f"],
                         "trust_net": v["t"], "dealer_net": v["dl"],
                         "margin_bal": v["mb"], "short_bal": v["sb"]})
        if i % FLUSH_EVERY == 0:
            merge_csv(out, pd.DataFrame(rows), ["code", "date"], quiet=True)
            rows = []
            print(f"    ...{i}/{len(todo)}（已落地，累計 API 呼叫 {fm.calls}）")
        time.sleep(0.25)
    merge_csv(out, pd.DataFrame(rows), ["code", "date"])


def fetch_daytrading(fm: FinMind, resume: bool):
    print("\n[4/5] 當沖（FinMind）")
    out = os.path.join(DATA, "daytrading", "twstock_daytrading.csv")
    done = done_codes(out) if resume else set()
    todo = [c for c in UNIVERSE if c not in done]
    print(f"    已完成 {len(done)} 檔，待抓 {len(todo)} 檔")
    rows = []
    for i, code in enumerate(todo, 1):
        if fm.quota_exhausted:
            print(f"    額度用盡，停在第 {i-1}/{len(todo)} 檔。等一小時後重跑同一行指令即可續抓。")
            break
        data = fm.get("TaiwanStockDayTrading", code, PRICE_START)
        for r in (data or []):
            rows.append({"code": code, "date": r["date"], "dt_volume": r.get("Volume"),
                         "buy_amount": r.get("BuyAmount"), "sell_amount": r.get("SellAmount")})
        if i % FLUSH_EVERY == 0:
            merge_csv(out, pd.DataFrame(rows), ["code", "date"], quiet=True)
            rows = []
            print(f"    ...{i}/{len(todo)}（已落地）")
        time.sleep(0.2)
    merge_csv(out, pd.DataFrame(rows), ["code", "date"])


def fetch_fundamentals(fm: FinMind, resume: bool, datasets: set[str] | None = None):
    print("\n[5/5] 月營收／財報／本益比（FinMind）")
    out = os.path.join(DATA, "fundamentals", "twstock_fundamentals.csv")
    datasets = datasets or {"rev", "fin", "per"}
    # 只有被指定的資料集都齊了，才算這檔完成
    done = fundamentals_done(out, datasets) if resume else set()
    todo = [c for c in UNIVERSE if c not in done]
    print(f"    抓取項目 {sorted(datasets)}｜已完成 {len(done)} 檔，待抓 {len(todo)} 檔")
    rows = []
    for i, code in enumerate(todo, 1):
        if fm.quota_exhausted:
            print(f"    額度用盡，停在第 {i-1}/{len(todo)} 檔。等一小時後重跑同一行指令即可續抓。")
            break
        rev = fm.get("TaiwanStockMonthRevenue", code, FUND_START) if "rev" in datasets else None
        for r in (rev or []):
            rows.append({"dataset": "rev", "code": code, "date": r["date"],
                         "key": f"{r['revenue_year']}-{str(r['revenue_month']).zfill(2)}",
                         "value": r["revenue"]})
        fin = fm.get("TaiwanStockFinancialStatements", code, FUND_START) if "fin" in datasets else None
        for r in (fin or []):
            if r["type"] in FIN_KEEP:
                rows.append({"dataset": "fin", "code": code, "date": r["date"],
                             "key": r["type"], "value": r["value"]})
        per = fm.get("TaiwanStockPER", code, PRICE_START) if "per" in datasets else None
        for r in (per or []):
            for k, v in (("PER", r.get("PER")), ("PBR", r.get("PBR")),
                         ("YIELD", r.get("dividend_yield"))):
                rows.append({"dataset": "per", "code": code, "date": r["date"],
                             "key": k, "value": v})
        if i % FLUSH_EVERY == 0:
            merge_csv(out, pd.DataFrame(rows), ["dataset", "code", "date", "key"], quiet=True)
            rows = []
            print(f"    ...{i}/{len(todo)}（已落地，累計 API 呼叫 {fm.calls}）")
        time.sleep(0.3)
    merge_csv(out, pd.DataFrame(rows), ["dataset", "code", "date", "key"])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["prices", "macro", "chips", "daytrading", "fundamentals"],
                    help="只抓某一類")
    ap.add_argument("--resume", action="store_true", help="只補輸出檔裡還沒有的股票，可安全重複執行")
    ap.add_argument("--datasets", default="rev,fin,per",
                    help="基本面要抓哪些：rev=月營收(最重要) fin=財報 per=本益比(目前沒有因子採用)。"
                         "額度吃緊時建議先跑 --datasets rev")
    args = ap.parse_args()

    ensure_dirs()
    token = load_token()
    if token:
        print(f"已讀到 FinMind token（長度 {len(token)}，內容不顯示）")
    else:
        print("沒有 token，會用匿名額度（比較容易被限流，建議建立 finmind_token.txt）")
    fm = FinMind(token)

    start = time.time()
    jobs = {
        "prices": fetch_prices,
        "macro": fetch_macro,
        "chips": lambda: fetch_chips(fm, args.resume),
        "daytrading": lambda: fetch_daytrading(fm, args.resume),
        "fundamentals": lambda: fetch_fundamentals(fm, args.resume,
                                                   set(args.datasets.split(","))),
    }
    for name, fn in jobs.items():
        if args.only and args.only != name:
            continue
        try:
            fn()
        except KeyboardInterrupt:
            print("\n中斷。每 10 檔會落地一次，最多損失最後幾檔；"
                  "下次加 --resume 會自己從檔案裡算出還缺哪些。")
            return
        except Exception as e:
            print(f"    ✗ {name} 失敗：{e}")

    print(f"\n完成，耗時 {int(time.time()-start)} 秒，FinMind 呼叫 {fm.calls} 次。")
    print("接下來把 data 資料夾的內容交給 Claude，或直接跑：")
    print("    python src/run_backtest.py")


if __name__ == "__main__":
    main()
