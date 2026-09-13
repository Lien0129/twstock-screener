"""正式版觀察池：每個月依「當時」的成交值重新排名。

為什麼要改成這樣
----------------
原本的 106 檔是 2026 年 8 月一次挑完就寫死在 config.py 裡。問題有兩個：

1. 它會越來越過時。名單挑好之後不管市場怎麼變都不會動，一年後可能有
   三成的股票早就不是成交值前段班了。
2. 更嚴重的是它跟十年回測跑在不同的池子上。回測用的是 universe_pit.py
   （每月重排、只用當時可得的資訊），dashboard 卻用固定 106 檔。
   所以回測算出來的 +374.7% 嚴格講**不是在描述 dashboard 的行為**。

這一支把 dashboard 的池子改成跟回測同一套建構方式，兩邊才對得起來。

規則
----
* 每個月最後一個交易日排名，套用到下一個月（跟 universe_pit.build 一致）。
* 排名依據：近 60 個交易日的日均成交值。
* 至少要有 MIN_HISTORY 天歷史，否則 120MA 之類的指標算不出來。
* 最終池 = 前 100 名 ∪ **目前持股**。
  持股就算掉出前 100 也繼續追蹤——先前實測過「掉出榜就賣」會把波段策略
  的報酬從 +4.95% 打到 −0.05%，所以池子的變動不該連動出場決策。
  （長期持有的 ETF 走 long_term.json，不套用選股規則，不納入這裡。）

資料來源
--------
* 種子：data/wide/twstock_wide.csv.gz（1,088 檔十年歷史，close×volume）
* 增量：data/market/stock_day_all.csv（每天由 daily_update.py 抓 TWSE
  STOCK_DAY_ALL 累積，直接有 TradeValue 欄位）
兩邊有重疊日期時以種子為準，並算出比值存進輸出檔——如果兩個來源的
成交值量級對不上（例如單位不同），那個比值會偏離 1，一眼看得出來。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDE = os.path.join(ROOT, "data", "wide", "twstock_wide.csv.gz")
MARKET = os.path.join(ROOT, "data", "market", "stock_day_all.csv")
NAMES = os.path.join(ROOT, "data", "reference", "listed_codes.csv")
OUT = os.path.join(ROOT, "data", "universe_current.json")
HOLDINGS = os.path.join(ROOT, "portfolio", "holdings.json")

TURNOVER_WINDOW = 60
MIN_HISTORY = 150
TOP_N = 100


# ------------------------------------------------------------------ 資料
def _seed() -> pd.DataFrame:
    """十年歷史的日成交值。回傳 code / date / turnover。"""
    if not os.path.exists(WIDE):
        return pd.DataFrame(columns=["code", "date", "turnover"])
    d = pd.read_csv(WIDE, dtype={"code": str}, usecols=["code", "date", "close", "volume"])
    d["date"] = pd.to_datetime(d["date"])
    d["turnover"] = d["close"] * d["volume"]
    return d[["code", "date", "turnover"]]


def _live() -> pd.DataFrame:
    """每日累積的全市場成交值。TWSE 的 TradeValue 本身就是元，不用再乘。"""
    if not os.path.exists(MARKET):
        return pd.DataFrame(columns=["code", "date", "turnover"])
    d = pd.read_csv(MARKET, dtype={"code": str})
    if d.empty:
        return pd.DataFrame(columns=["code", "date", "turnover"])
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["turnover"] = pd.to_numeric(d["turnover"], errors="coerce")
    d = d.dropna(subset=["date", "turnover", "code"])
    d["code"] = d["code"].astype(str).str.strip()
    # 只留 4 碼純數字的普通股，排除權證／ETF／債券那些
    d = d[d["code"].str.fullmatch(r"\d{4}")]
    return d[["code", "date", "turnover"]]


def _names() -> dict[str, str]:
    if not os.path.exists(NAMES):
        return {}
    d = pd.read_csv(NAMES, dtype=str)
    return dict(zip(d["code"].astype(str).str.strip(), d["name"].astype(str).str.strip()))


def held_codes() -> list[str]:
    """目前持股。這些不論排名如何都留在池子裡。"""
    if not os.path.exists(HOLDINGS):
        return []
    try:
        with open(HOLDINGS, encoding="utf-8") as f:
            data = json.load(f)
        return [str(h["code"]).strip() for h in data.get("holdings", []) if h.get("code")]
    except Exception:
        return []


def turnover_panel() -> tuple[pd.DataFrame, float]:
    """合併兩個來源，回傳長表與「來源比值」（重疊日的 live/seed 中位數）。"""
    s, l = _seed(), _live()
    ratio = float("nan")
    if not s.empty and not l.empty:
        both = s.merge(l, on=["code", "date"], suffixes=("_s", "_l"))
        both = both[(both["turnover_s"] > 0) & (both["turnover_l"] > 0)]
        if len(both) >= 50:
            ratio = float((both["turnover_l"] / both["turnover_s"]).median())
    # 重疊處以種子為準：同一天兩邊都有就丟掉 live 那筆
    if not s.empty and not l.empty:
        key = set(zip(s["code"], s["date"]))
        l = l[[k not in key for k in zip(l["code"], l["date"])]]
    panel = pd.concat([s, l], ignore_index=True)
    panel = panel.dropna(subset=["turnover"]).sort_values(["code", "date"])
    return panel.reset_index(drop=True), ratio


# ------------------------------------------------------------------ 排名
def decide_date(panel: pd.DataFrame, today: date | None = None) -> pd.Timestamp | None:
    """排名決定日 = 「上個月」的最後一個交易日。

    基準是**今天**在哪個月，不是資料的最後一天在哪個月。這兩個很容易搞混：
    9/10 跑的時候八月已經走完，該用 8/31 的排名；如果拿「資料最後一個月」
    當判斷依據，8/31 會被當成未完成的當月而跳掉，退回用 7/31——白白丟掉
    一個月的新資訊。

    反過來，永遠不會用「本月」的排名，否則就變成用 9/10 的資料決定
    9/1~9/10 該看哪些股票，那是偷看未來。

    資料落後於今天（例如資料只到 7 月）時自然會退回可得的最後一個月底，
    這是正確行為：用得到的最新資訊，不是硬要湊出當月。
    """
    if panel.empty:
        return None
    today = today or date.today()
    cur = pd.Period(pd.Timestamp(today), freq="M")
    dates = pd.Series(sorted(panel["date"].unique()))
    prior = dates[dates.dt.to_period("M") < cur]
    if not len(prior):
        return None
    # 上個（或更早）月份裡最後一個交易日
    last_ym = prior.dt.to_period("M").iloc[-1]
    return prior[prior.dt.to_period("M") == last_ym].iloc[-1]


def rank_on(panel: pd.DataFrame, on: pd.Timestamp, top_n: int = TOP_N) -> list[str]:
    p = panel[panel["date"] <= on].copy()
    p["ma"] = (p.groupby("code")["turnover"]
               .transform(lambda s: s.rolling(TURNOVER_WINDOW, min_periods=40).mean()))
    p["bars"] = p.groupby("code").cumcount() + 1
    snap = p[(p["date"] == on) & (p["bars"] >= MIN_HISTORY)].dropna(subset=["ma"])
    return snap.nlargest(top_n, "ma")["code"].tolist()


# ------------------------------------------------------------------ 輸出
def load_current() -> dict | None:
    if not os.path.exists(OUT):
        return None
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fallback_universe() -> dict[str, str]:
    """第一次跑、還沒有 universe_current.json 時，拿 config 裡寫死的當前一版。"""
    import config
    return {str(k): v for k, v in config.FALLBACK_UNIVERSE.items()}


def rebalance(force: bool = False, top_n: int = TOP_N, verbose: bool = True,
              baseline: str = "current") -> dict:
    panel, ratio = turnover_panel()
    on = decide_date(panel)
    if on is None:
        raise RuntimeError("成交值資料不足，無法排名")

    cur = load_current()
    if cur and cur.get("as_of") == str(on.date()) and not force:
        if verbose:
            print(f"觀察池已是最新（基準日 {cur['as_of']}，{len(cur['universe'])} 檔），不動。")
        return cur

    # 「異動」是相對於誰算的。平常相對於上一版池子；baseline=fallback 是給
    # 第一次切換用的，讓異動相對於 config 裡那份手挑的 106 檔。
    if baseline == "fallback" or not cur:
        prev = _fallback_universe()
    else:
        prev = dict(cur["universe"])
    names = _names()

    top = rank_on(panel, on, top_n)
    held = [c for c in held_codes() if c not in top]
    codes = list(top) + held

    universe = {c: names.get(c, prev.get(c, c)) for c in codes}
    added = [c for c in codes if c not in prev]
    removed = [c for c in prev if c not in universe]

    out = {
        "as_of": str(on.date()),
        "rebalanced_on": str(date.today()),
        "top_n": top_n,
        "rule": f"上市普通股，近 {TURNOVER_WINDOW} 個交易日日均成交值前 {top_n} 名，"
                f"以每月最後交易日排名，套用到下個月；再聯集目前持股",
        "universe": universe,
        "changes": {
            "added": [{"code": c, "name": universe[c]} for c in added],
            "removed": [{"code": c, "name": prev.get(c, c)} for c in removed],
            "kept_for_holding": [{"code": c, "name": universe[c]} for c in held],
        },
        "previous_as_of": (cur or {}).get("as_of"),
        "source_ratio": None if pd.isna(ratio) else round(ratio, 4),
        "source_days": {
            "seed_last": str(pd.to_datetime(_seed()["date"]).max().date()) if os.path.exists(WIDE) else None,
            "live_rows": int(len(_live())),
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"基準日 {out['as_of']}　池子 {len(universe)} 檔"
              f"（前 {top_n} 名 {len(top)} + 持股保留 {len(held)}）")
        print(f"新進 {len(added)} 檔：" + "、".join(f"{c} {universe[c]}" for c in added))
        print(f"剔除 {len(removed)} 檔：" + "、".join(f"{c} {prev.get(c, c)}" for c in removed))
        if held:
            print("因持股保留：" + "、".join(f"{c} {universe[c]}" for c in held))
        if out["source_ratio"] is not None and not (0.5 < out["source_ratio"] < 2.0):
            print(f"⚠️ 兩個成交值來源的量級對不上（比值 {out['source_ratio']}），"
                  f"排名可能失真，先檢查 stock_day_all.csv 的單位")
    return out


if __name__ == "__main__":
    import argparse
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="即使基準日沒變也重排")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--baseline", choices=["current", "fallback"], default="current",
                    help="異動相對於哪一份名單計算（第一次切換用 fallback）")
    a = ap.parse_args()
    rebalance(force=a.force, top_n=a.top, baseline=a.baseline)
