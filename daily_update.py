"""每日更新 — 收盤後在你自己的電腦上執行。

    python daily_update.py                # 更新到今天
    python daily_update.py --date 20260825   # 指定日期
    python daily_update.py --monthly      # 順便更新月營收（每月 10 號之後跑一次）
    python daily_update.py --no-report    # 只更新資料，不重算訊號

跟 fetch_data.py 的差別
-----------------------
fetch_data.py 是「建歷史庫」，一次抓兩年、幾百次請求、會被流量限制卡住。
這支是「補今天這一天」，因為證交所的日報表**一次回傳全市場**，
所以籌碼與融資券各只要 1 次請求，不需要 FinMind、不需要 token。

股價走 Yahoo 是為了拿到還原股價（除權息調整過的價格）。
證交所的收盤價沒有還原，直接存進去會讓長期均線在除權息日出現假跳空。

執行時機
--------
建議 18:00 之後。三大法人日報大約 16:00~18:00 公告，太早跑會抓到空的。
非交易日（週末、國定假日）會直接告訴你今天沒有資料，不會寫入任何東西。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import pandas as pd
    import requests
except ImportError:
    sys.exit("請先安裝套件：pip install requests pandas")

import sys as _sys
# Windows 終端機預設 cp950，輸出重導到檔案時會因為 ✓ 這類字元整支掛掉。
# 這幾行必須在任何 print 之前執行。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

PRICE_CSV = os.path.join(DATA, "prices", "twstock_history.csv")
CHIP_CSV = os.path.join(DATA, "chips", "twstock_chips.csv")
MACRO_CSV = os.path.join(DATA, "twstock_macro.csv")
FUND_CSV = os.path.join(DATA, "fundamentals", "twstock_fundamentals.csv")
MARKET_CSV = os.path.join(DATA, "market", "stock_day_all.csv")

MACRO_SYMBOLS = {"^TWII": "TWII", "^SOX": "SOX", "^IXIC": "IXIC", "^GSPC": "GSPC",
                 "^VIX": "VIX", "DX-Y.NYB": "DXY", "TWD=X": "USDTWD", "^TNX": "US10Y"}


def universe() -> list[str]:
    import config as C
    return list(C.UNIVERSE)


def _num(x) -> float:
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "X", "None", "nan"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def merge(path: str, new: pd.DataFrame, keys: list[str], label: str):
    if new.empty:
        print(f"    {label}：沒有新資料")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if "code" in new.columns:
        new["code"] = new["code"].astype(str).str.zfill(4)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"code": str})
        before = len(old)
        out = pd.concat([old, new], ignore_index=True).drop_duplicates(subset=keys, keep="last")
        out.to_csv(path, index=False)
        added = len(out) - before
        print(f"    {label}：+{added} 筆（總計 {len(out):,}）")
        return added
    new.to_csv(path, index=False)
    print(f"    {label}：新建 {len(new)} 筆")
    return len(new)


# ------------------------------------------------------------------ 證交所
def twse_json(url: str, params: dict) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=25)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def twse_open(path: str) -> list | None:
    """證交所 OpenAPI（無參數的純 JSON 端點）。"""
    for attempt in range(3):
        try:
            r = requests.get(f"https://openapi.twse.com.tw/v1{path}",
                             headers=UA, timeout=25)
            if r.status_code == 200:
                j = r.json()
                return j if isinstance(j, list) else None
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def update_market_all(date_str: str) -> int:
    """全市場當日成交值，一天一個請求。

    這是每月重排觀察池的燃料。用 TWSE 的 STOCK_DAY_ALL：它一次給所有上市
    股票當天的 TradeValue（成交金額，單位是元），不用去 Yahoo 逐檔抓 1,088 次。
    只有「今天」的資料，抓不到歷史——所以歷史部分由 twstock_wide.csv.gz
    當種子，這裡只負責往後累積。

    漏掉幾天不要緊：排名用的是 60 日均值，少幾天不會改變前 100 名的組成。
    """
    rows = twse_open("/exchangeReport/STOCK_DAY_ALL")
    if not rows:
        print("    全市場成交值：抓不到（證交所端點沒回應）")
        return 0
    out = []
    for r in rows:
        code = str(r.get("Code", "")).strip()
        if not code.isdigit() or len(code) != 4:
            continue          # 排除 ETF、權證、公司債那些非普通股
        val = _num(r.get("TradeValue"))
        close = _num(r.get("ClosingPrice"))
        if not (val > 0):
            continue          # 當天沒成交（停牌、無量）就不記，別讓 0 拉低均值
        out.append({"code": code, "date": _iso(r.get("Date") or date_str),
                    "close": close, "turnover": val})
    df = pd.DataFrame(out)
    return merge(MARKET_CSV, df, ["code", "date"], "全市場成交值")


def _iso(d) -> str:
    """1150910 / 20260910 → 2026-09-10。"""
    s = str(d).strip().replace("/", "").replace("-", "")
    try:
        if len(s) == 7:                      # 民國
            return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except Exception:
        pass
    return str(d)


def rebalance_universe() -> None:
    """每月重排觀察池。基準日沒變就什麼都不做。

    刻意放在 update_prices() 之前：重排完才知道今天要抓哪些股票的價格。
    """
    try:
        sys.path.insert(0, SRC)
        import universe_live
        import config as C
        before = set(C.UNIVERSE)
        info = universe_live.rebalance(verbose=False)
        C.refresh_universe()
        after = set(C.UNIVERSE)
        if before != after:
            add, drop = after - before, before - after
            print(f"    觀察池重排（基準日 {info['as_of']}）："
                  f"{len(after)} 檔，新進 {len(add)}、剔除 {len(drop)}")
            if add:
                print("      新進：" + "、".join(f"{c} {C.UNIVERSE[c]}" for c in sorted(add)))
        else:
            print(f"    觀察池：{len(after)} 檔，基準日 {info.get('as_of')}，無異動")
    except Exception as e:
        print(f"    觀察池重排失敗（沿用現有名單）：{e}")


def update_etf() -> int:
    """長期持有的 ETF 報價。

    跟觀察池那 106 檔完全分開：ETF 不進選股流程、不套用出場規則，
    這裡抓價格只是為了在儀表板上顯示損益。標的清單來自
    portfolio/long_term.json，你增減部位時改那個檔案就好，不用動程式。
    """
    import json
    lt = os.path.join(ROOT, "portfolio", "long_term.json")
    if not os.path.exists(lt):
        return 0
    try:
        with open(lt, encoding="utf-8") as f:
            codes = [h["code"] for h in json.load(f).get("holdings", [])]
    except Exception:
        return 0
    if not codes:
        return 0
    print("\n[6/6] 長期持有 ETF 報價")
    rows = []
    for c in codes:
        d = yahoo(f"{c}.TW", "1y")
        if d:
            df = pd.DataFrame(d)
            df.insert(0, "code", c)
            rows.append(df)
        else:
            print(f"    {c}：抓不到")
        time.sleep(1.0)
    if not rows:
        return 0
    out = pd.concat(rows, ignore_index=True)
    return merge(os.path.join(DATA, "etf", "etf_history.csv"), out,
                 ["code", "date"], "ETF 報價")


def update_events() -> int:
    """風險事件：處置股、注意股、重大訊息、現金增資。

    這些不是選股因子，是「你的停損保護不了你」的提醒：
      * 被處置 = 分盤交易（約每 2~25 分鐘才撮合一次）+ 預收款券，
        你的交易方式直接改變，停損單不一定來得及成交。
      * 注意股 = 處置的前一步，連續達標就會被處置。
      * 重大訊息 = 公司依法必須公告的事件。這是唯一能自動抓到「利空」的來源：
        新聞掃描找到的幾乎全是利多（媒體對漲的股票寫多頭稿），但重大訊息
        是法定揭露，好壞都得公告。法說會也是走這個管道（符合條款第 26 款）。
      * 現金增資 = 股本稀釋，且認購期間股價通常受壓。

    全部來自證交所 OpenAPI，一次請求拿全市場，不需要任何 token。
    每個端點只給「當前狀態」，沒有歷史，所以這裡採累積寫入
    （merge 會依 keys 去重），跑久了才會累積出歷史。
    """
    print("\n[5/5] 風險事件（證交所 OpenAPI）")
    uni = set(universe())
    total = 0

    j = twse_open("/announcement/punish")
    if j:
        rows = [{"code": str(x.get("Code", "")).zfill(4),
                 "name": x.get("Name", ""),
                 "date": x.get("Date", ""),
                 "period": x.get("DispositionPeriod", ""),
                 "measure": x.get("DispositionMeasures", ""),
                 "reason": x.get("ReasonsOfDisposition", "")}
                for x in j if str(x.get("Code", "")).strip()]
        df = pd.DataFrame([r for r in rows if r["code"] in uni])
        total += merge(os.path.join(DATA, "events", "punish.csv"), df,
                       ["code", "period"], "處置股")
        print(f"    （全市場 {len(rows)} 檔被處置，其中 {len(df)} 檔在觀察池）")

    j = twse_open("/announcement/notice")
    if j:
        rows = [{"code": str(x.get("Code", "")).zfill(4),
                 "name": x.get("Name", ""),
                 "date": x.get("Date", ""),
                 "reason": x.get("TradingInfoForAttention", ""),
                 "close": x.get("ClosingPrice", ""), "per": x.get("PE", "")}
                for x in j if str(x.get("Code", "")).strip()]
        df = pd.DataFrame([r for r in rows if r["code"] in uni])
        total += merge(os.path.join(DATA, "events", "notice.csv"), df,
                       ["code", "date"], "注意股")

    j = twse_open("/opendata/t187ap04_L")
    if j:
        rows = []
        for x in j:
            code = str(x.get("公司代號", "")).zfill(4)
            if code not in uni:
                continue
            rows.append({"code": code, "name": x.get("公司名稱", ""),
                         "date": x.get("發言日期", ""),
                         "happened": x.get("事實發生日", ""),
                         "subject": (x.get("主旨", "") or "").replace("\r\n", " ").strip(),
                         "clause": x.get("符合條款", ""),
                         "detail": (x.get("說明", "") or "").replace("\r\n", " ")[:400]})
        total += merge(os.path.join(DATA, "events", "material.csv"),
                       pd.DataFrame(rows), ["code", "date", "subject"], "重大訊息")

    j = twse_open("/opendata/t187ap38_L")
    if j:
        rows = []
        for x in j:
            code = str(x.get("公司代號", "")).zfill(4)
            if code not in uni:
                continue
            amt = str(x.get("擬現金增資金額(元)", "") or "").strip()
            if not amt or amt == "0":
                continue
            rows.append({"code": code, "name": x.get("公司名稱", ""),
                         "date": x.get("股東常(臨時)會日期-日期", ""),
                         "amount": amt,
                         "rate": x.get("現金增資認購率(%)", ""),
                         "announced": x.get("公告日期", "")})
        total += merge(os.path.join(DATA, "events", "capital.csv"),
                       pd.DataFrame(rows), ["code", "announced"], "現金增資")
    return total


def update_chips(date_str: str) -> int:
    """三大法人買賣超。一次請求拿全市場，不需要 FinMind。"""
    print("\n[2/4] 三大法人（證交所，全市場一次取得）")
    j = twse_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                  {"date": date_str, "selectType": "ALL", "response": "json"})
    if not j or j.get("stat") != "OK" or not j.get("data"):
        print(f"    這一天沒有法人資料（{j.get('stat') if j else '連線失敗'}）")
        return 0
    fields = j["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    ci = idx.get("證券代號")
    fi = next((i for n, i in idx.items() if n.startswith("外陸資買賣超股數")), None)
    ti = next((i for n, i in idx.items() if n.startswith("投信買賣超")), None)
    bi = next((i for n, i in idx.items() if n.startswith("三大法人買賣超")), None)
    if None in (ci, fi, ti, bi):
        print("    欄位名稱跟預期不同，請檢查證交所是否改格式：", fields[:6])
        return 0
    uni = set(universe())
    iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    rows = []
    for r in j["data"]:
        code = str(r[ci]).strip()
        if code not in uni:
            continue
        f, t, b = _num(r[fi]), _num(r[ti]), _num(r[bi])
        rows.append({"code": code, "date": iso, "foreign_net": f, "trust_net": t,
                     "dealer_net": b - f - t, "margin_bal": "", "short_bal": ""})
    print(f"    命中觀察池 {len(rows)}/{len(uni)} 檔")
    return merge(CHIP_CSV, pd.DataFrame(rows), ["code", "date"], "籌碼")


def update_margin(date_str: str) -> int:
    """融資融券餘額。同樣一次請求拿全市場，併進既有的籌碼列。"""
    print("\n[3/4] 融資融券（證交所）")
    j = twse_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                  {"date": date_str, "selectType": "ALL", "response": "json"})
    if not j or j.get("stat") != "OK":
        print("    這一天沒有融資券資料")
        return 0
    tables = j.get("tables") or []
    # 個股表的欄位長這樣（證交所實際格式）：
    #   代號,名稱, [融資]買進,賣出,現金償還,前日餘額,今日餘額,次一營業日限額,
    #             [融券]買進,賣出,現券償還,前日餘額,今日餘額,次一營業日限額, 資券互抵, 註記
    # 注意「今日餘額」出現兩次，第一次是融資、第二次是融券，所以要用位置而不是名稱。
    tbl = next((t for t in tables
                if "代號" in (t.get("fields") or []) and len(t.get("fields") or []) >= 14), None)
    if tbl is None:
        print("    找不到個股融資券表格")
        return 0
    fields = tbl["fields"]
    ci = fields.index("代號")
    bal_idx = [i for i, n in enumerate(fields) if n == "今日餘額"]
    if len(bal_idx) < 2:
        print("    欄位格式跟預期不同，證交所可能改版了：", fields)
        return 0
    mi, si = bal_idx[0], bal_idx[1]
    uni = set(universe())
    iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    rows = [{"code": str(r[ci]).strip(), "date": iso,
             "margin_bal": _num(r[mi]), "short_bal": _num(r[si])}
            for r in tbl["data"] if str(r[ci]).strip() in uni]
    if not rows or not os.path.exists(CHIP_CSV):
        print("    沒有可併入的資料")
        return 0
    new = pd.DataFrame(rows)
    old = pd.read_csv(CHIP_CSV, dtype={"code": str})
    old = old.merge(new, on=["code", "date"], how="left", suffixes=("", "_n"))
    for col in ("margin_bal", "short_bal"):
        old[col] = old[f"{col}_n"].combine_first(old[col])
        old = old.drop(columns=[f"{col}_n"])
    old.to_csv(CHIP_CSV, index=False)
    print(f"    融資券：更新 {len(rows)} 檔")
    return len(rows)


# ------------------------------------------------------------------ Yahoo
def yahoo(symbol: str, rng: str = "3mo") -> list[dict]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"range": rng, "interval": "1d", "events": "div,split"},
                             headers=UA, timeout=20)
            if r.status_code != 200:
                time.sleep(1.5); continue
            res = (r.json().get("chart") or {}).get("result")
            if not res or not res[0].get("timestamp"):
                return []
            res = res[0]
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            out = []
            for i, ts in enumerate(res["timestamp"]):
                if q["close"][i] is None:
                    continue
                out.append({"date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                            "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
                            "close": q["close"][i], "volume": q["volume"][i],
                            "adjclose": adj[i] if adj else q["close"][i]})
            return out
        except Exception:
            time.sleep(1.5)
    return []


def update_prices() -> int:
    """走 Yahoo 是為了還原股價。證交所的收盤價沒有除權息調整，
    直接存會讓均線在除權息日出現假跳空。"""
    print("\n[1/4] 股價（Yahoo，含還原股價）")
    uni = universe()

    # 已經有多少天歷史？剛被重排進池子的股票一天都沒有，抓 3 個月（約 60 根）
    # 不夠算 120MA，指標會整排是空的，那檔等於白抓。這些補抓兩年。
    bars: dict[str, int] = {}
    if os.path.exists(PRICE_CSV):
        try:
            old = pd.read_csv(PRICE_CSV, dtype={"code": str}, usecols=["code"])
            bars = old["code"].value_counts().to_dict()
        except Exception:
            bars = {}
    fresh = [c for c in uni if bars.get(c, 0) < 200]
    if fresh:
        print(f"    {len(fresh)} 檔歷史不足，改抓 2 年："
              f"{'、'.join(fresh[:8])}{' …' if len(fresh) > 8 else ''}")

    rows, failed = [], []
    for i, code in enumerate(uni, 1):
        rng = "2y" if code in fresh else "3mo"
        got = None
        for sfx in (".TW", ".TWO"):
            d = yahoo(code + sfx, rng)
            if d:
                got = (sfx[1:], d); break
        if not got:
            failed.append(code); continue
        market, data = got
        for d in data:
            rows.append({"code": code, "market": market, **d})
        if i % 30 == 0:
            print(f"    ...{i}/{len(uni)}")
        time.sleep(0.1)
    if failed:
        print(f"    抓不到 {len(failed)} 檔：{','.join(failed[:8])}")
    df = pd.DataFrame(rows)
    if not df.empty:
        for c in ("open", "high", "low", "close", "adjclose"):
            df[c] = df[c].round(2)
    return merge(PRICE_CSV, df, ["code", "date"], "股價")


def update_macro() -> int:
    print("\n[4/4] 加權指數與國際指標（Yahoo）")
    rows = []
    for sym, name in MACRO_SYMBOLS.items():
        for d in yahoo(sym):
            rows.append({"symbol": name, "date": d["date"], "open": d["open"], "high": d["high"],
                         "low": d["low"], "close": d["close"], "volume": d["volume"]})
        time.sleep(0.1)
    return merge(MACRO_CSV, pd.DataFrame(rows), ["symbol", "date"], "指數")


def update_monthly_revenue() -> int:
    """月營收。每月 10 號之後跑一次就好，需要 FinMind token（放在 finmind_token.txt）。"""
    print("\n[月營收] FinMind")
    token = os.environ.get("FINMIND_TOKEN")
    tp = os.path.join(ROOT, "finmind_token.txt")
    if not token and os.path.exists(tp):
        token = open(tp, encoding="utf-8").read().strip()
    start = (datetime.now(TPE) - timedelta(days=200)).strftime("%Y-%m-%d")
    rows, fails = [], 0
    for i, code in enumerate(universe(), 1):
        params = {"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start}
        if token:
            params["token"] = token
        try:
            r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=25)
            j = r.json()
            if j.get("status") != 200:
                fails += 1
                if fails >= 3:
                    print(f"    額度用盡，停在第 {i} 檔。明天再跑一次即可。")
                    break
                continue
            fails = 0
            for x in j.get("data", []):
                rows.append({"dataset": "rev", "code": code, "date": x["date"],
                             "key": f"{x['revenue_year']}-{str(x['revenue_month']).zfill(2)}",
                             "value": x["revenue"]})
        except Exception:
            fails += 1
        time.sleep(0.25)
    return merge(FUND_CSV, pd.DataFrame(rows), ["dataset", "code", "date", "key"], "月營收")


def missing_dates(today: str, max_days: int = 15) -> list[str]:
    """從籌碼檔的最後一天推算還缺哪些交易日。

    為什麼需要這個：如果排程排在 20:30 但電腦 21:00 才開機，那次就錯過了。
    Windows 的「錯過就盡快執行」會補跑，但補跑時如果只抓當天，
    錯過的那幾天就永遠是洞。所以一律從資料的最後一天往後補到今天。
    """
    if not os.path.exists(CHIP_CSV):
        return [today]
    df = pd.read_csv(CHIP_CSV, usecols=["date"])
    last = pd.to_datetime(df["date"]).max()
    end = pd.to_datetime(today, format="%Y%m%d")
    days = []
    d = last + pd.Timedelta(days=1)
    while d <= end and len(days) < max_days:
        if d.weekday() < 5:                    # 國定假日抓不到資料，證交所會回空，跳過就好
            days.append(d.strftime("%Y%m%d"))
        d += pd.Timedelta(days=1)
    return days


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期 YYYYMMDD，預設今天（台北時間）")
    ap.add_argument("--monthly", action="store_true", help="順便更新月營收")
    ap.add_argument("--no-report", action="store_true", help="只更新資料，不重算訊號與儀表板")
    args = ap.parse_args()

    date_str = args.date or datetime.now(TPE).strftime("%Y%m%d")
    now = datetime.now(TPE)
    print(f"更新目標日期：{date_str}　現在時間：{now:%Y-%m-%d %H:%M}（台北）")
    if not args.date and now.hour < 18:
        print("提醒：三大法人日報大約 16:00~18:00 才公告，現在跑可能抓不到今天的籌碼。")

    t0 = time.time()
    # 順序有意義：先抓全市場成交值 → 重排池子 → 才知道要抓哪些股票的價格
    update_market_all(date_str)
    rebalance_universe()
    update_prices()

    days = [date_str] if args.date else missing_dates(date_str)
    if not days:
        print("\n籌碼資料已經是最新的，不需要補。")
    elif len(days) > 1:
        print(f"\n偵測到 {len(days)} 個交易日還沒抓："
              f"{days[0]} ~ {days[-1]}（可能是前幾天沒開機），逐日補齊")
    for d in days:
        update_chips(d)
        update_margin(d)
    update_macro()
    update_events()
    update_etf()
    if args.monthly:
        update_monthly_revenue()

    print("\n" + "─" * 58)
    import verify_data
    problems = verify_data.check()

    if not args.no_report and not problems:
        print("\n重算訊號與儀表板…")
        for script in ("signals.py", "dashboard.py"):
            subprocess.run([sys.executable, os.path.join(SRC, script)], check=False)
        print("完成：output/dashboard.html")
    elif problems:
        print("\n資料有問題，先不重算訊號。修好再跑：python daily_update.py --no-report")

    print(f"\n總耗時 {int(time.time()-t0)} 秒")


if __name__ == "__main__":
    main()
