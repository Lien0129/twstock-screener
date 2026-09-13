"""抓「全上市」10 年日線 —— 長期回測用，在你自己的電腦執行。

    python fetch_universe.py            # 開始（可隨時 Ctrl+C，再跑會接續）
    python fetch_universe.py --years 10 # 預設就是 10 年
    python fetch_universe.py --status   # 只看進度，不抓

為什麼需要這支
--------------
現在的 106 檔觀察池是用「2026 年 8 月的成交值」挑出來的。拿它回測 10 年，
等於拿今天的贏家去跑十年前——川湖今天 14,180，2016 年是完全不同量級的公司。
數字會非常漂亮，而且完全是假的。

修法是 point-in-time 觀察池：每個月用「當時」的成交值重新排名取前 100。
要做到這件事，就必須先有全市場的歷史股價，不能只有這 106 檔。

抓不到的部分（先說清楚）
------------------------
已下市／被合併的公司 Yahoo 沒有可靠歷史，所以殘留的存活偏誤修不掉，
只能縮小。高成交值的股票很少下市，幅度應該不大，但報告裡會標註。

籌碼與基本面也不抓——1000 檔 × 10 年不是 FinMind 免費配額撐得住的量。
長期回測會是純技術面，這件事在比較結果時很重要。

斷點續傳
--------
進度直接從輸出檔推算，不用獨立的快取檔。快取會說謊：如果程式在「標記完成」
之後、「寫檔」之前被中斷，那些股票會被永久跳過——這個坑踩過一次了。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

try:
    import pandas as pd
    import requests
except ImportError:
    sys.exit("請先安裝套件：pip install requests pandas")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "prices", "twstock_wide.csv")
CODES = os.path.join(ROOT, "data", "reference", "listed_codes.csv")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SLEEP = 1.2          # 對 Yahoo 客氣一點，被擋比慢更麻煩
FLUSH_EVERY = 20     # 每 20 檔落盤一次，中斷最多只損失 20 檔


def listed_codes() -> pd.DataFrame:
    """全上市公司代號。證交所 OpenAPI，免 token。"""
    if os.path.exists(CODES):
        d = pd.read_csv(CODES, dtype={"code": str})
        print(f"沿用既有代號清單：{len(d)} 檔（要重抓就刪掉 {os.path.relpath(CODES, ROOT)}）")
        return d
    print("取得上市公司清單…")
    r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
                     headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for x in r.json():
        code = str(x.get("公司代號", "")).strip()
        # 只要 4 碼普通股；ETF、權證、特別股、TDR 都不是我們要的標的
        if len(code) == 4 and code.isdigit():
            rows.append({"code": code,
                         "name": x.get("公司簡稱", ""),
                         "industry": x.get("產業別", ""),
                         "listed": x.get("上市日期", "")})
    d = pd.DataFrame(rows).drop_duplicates("code").sort_values("code")
    os.makedirs(os.path.dirname(CODES), exist_ok=True)
    d.to_csv(CODES, index=False)
    print(f"  取得 {len(d)} 檔上市普通股 → {os.path.relpath(CODES, ROOT)}")
    return d


def done_codes() -> set:
    """已完成 = 輸出檔裡真的有資料的股票。不用獨立快取，快取會說謊。"""
    if not os.path.exists(OUT):
        return set()
    try:
        d = pd.read_csv(OUT, usecols=["code"], dtype={"code": str})
        return set(d["code"].unique())
    except Exception:
        return set()


def yahoo(code: str, years: int) -> pd.DataFrame | None:
    """Yahoo 日線。用 adjclose 是為了拿到還原股價——原始收盤價在除權息日
    會出現假跳空，長期均線與所有技術指標都會被污染。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"range": f"{years}y", "interval": "1d",
                                          "events": "div,split"},
                             headers=UA, timeout=25)
            if r.status_code != 200:
                time.sleep(2 * (attempt + 1))
                continue
            j = r.json()["chart"]["result"]
            if not j:
                return None
            res = j[0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
            d = pd.DataFrame({
                "date": [datetime.utcfromtimestamp(t).strftime("%Y-%m-%d") for t in ts],
                "open": q["open"], "high": q["high"], "low": q["low"],
                "close": q["close"], "adjclose": adj, "volume": q["volume"],
            })
            d = d.dropna(subset=["close"])
            if d.empty:
                return None
            d.insert(0, "code", code)
            return d
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--status", action="store_true", help="只看進度")
    a = ap.parse_args()

    codes = listed_codes()
    done = done_codes()
    todo = [c for c in codes["code"] if c not in done]

    print(f"\n全上市 {len(codes)} 檔　已完成 {len(done)}　待抓 {len(todo)}")
    if a.status:
        return 0
    if not todo:
        print("全部抓完了。把 data/prices/twstock_wide.csv 給我就可以進下一步。")
        return 0

    est = len(todo) * SLEEP / 60
    print(f"預估約 {est:.0f} 分鐘。可以隨時 Ctrl+C，再跑一次會從中斷的地方接續。\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    buf, ok, fail = [], 0, 0
    t0 = time.time()
    try:
        for i, code in enumerate(todo, 1):
            d = yahoo(code, a.years)
            if d is not None and len(d) > 60:
                buf.append(d)
                ok += 1
            else:
                fail += 1
            if i % FLUSH_EVERY == 0 or i == len(todo):
                if buf:
                    pd.concat(buf, ignore_index=True).to_csv(
                        OUT, mode="a", header=not os.path.exists(OUT), index=False)
                    buf = []
                el = time.time() - t0
                rate = i / el if el else 0
                left = (len(todo) - i) / rate / 60 if rate else 0
                print(f"  {i}/{len(todo)}　成功 {ok}　失敗 {fail}　"
                      f"剩約 {left:.0f} 分")
            time.sleep(SLEEP)
    except KeyboardInterrupt:
        if buf:
            pd.concat(buf, ignore_index=True).to_csv(
                OUT, mode="a", header=not os.path.exists(OUT), index=False)
        print("\n已中斷，抓到的都寫好了。再跑一次會接續。")
        return 0

    n = len(done_codes())
    print(f"\n完成。{os.path.relpath(OUT, ROOT)} 目前有 {n} 檔。")
    print("把這個檔案給我（或跟我說一聲，我從你電腦搬），就可以進下一步。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
