"""每日增量更新：把當天抓到的資料併進歷史庫，並檢查資料完不完整。

為什麼需要這支
--------------
雲端環境沒有對外網路，Python 自己抓不到資料——每日資料是由 Claude 用 WebFetch
按產業別逐份取回（見 RUNBOOK.md），這支負責「收下來、併進去、檢查有沒有缺」。

設計重點是**寧可大聲說缺資料，也不要安靜地用舊資料算出推薦**。
先前踩過一次坑：加權指數少了 17 個月，程式預設放行，結果大盤濾網靜默失效。
所以這裡的每個合併函式都會回報覆蓋率，signals 也會把新鮮度帶到儀表板上。
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

def _first_existing(*paths: str) -> str:
    """歷史庫可能放在 data/ 或 data/prices/，兩處都找。"""
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


PRICE_CSV = _first_existing(os.path.join(DATA, "prices", "twstock_history.csv"),
                            os.path.join(DATA, "twstock_history.csv"))
CHIP_CSV = os.path.join(DATA, "chips", "twstock_chips.csv")
MACRO_CSV = os.path.join(DATA, "twstock_macro.csv")

# 觀察池涵蓋的證交所產業別（T86 / MI_INDEX 的 selectType）
INDUSTRY_CODES = ["02", "03", "05", "08", "10", "12", "15", "17", "20",
                  "21", "22", "23", "24", "25", "26", "27", "28", "29", "31"]

# 上櫃股不在證交所報表內，每日更新拿不到，只能靠 fetch_data.py 補
TPEX_CODES = {"5274", "8069"}


def _num(x) -> float:
    """把 '1,234' 或 '--' 這類欄位轉成數字。"""
    if x is None:
        return float("nan")
    s = str(x).replace(",", "").replace("--", "").strip()
    if s in ("", "-", "X"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def roc_to_iso(s: str) -> str | None:
    """115/08/21 或 115年08月21日 → 2026-08-21"""
    s = str(s).strip().replace("年", "/").replace("月", "/").replace("日", "")
    parts = [p for p in s.split("/") if p]
    if len(parts) != 3:
        return None
    try:
        y = int(parts[0])
        y = y + 1911 if y < 1911 else y
        return f"{y}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except ValueError:
        return None


# ------------------------------------------------------------------ 併入
def _merge(path: str, new: pd.DataFrame, keys: list[str]) -> dict:
    if new.empty:
        return {"新增": 0, "更新": 0, "總筆數": _rowcount(path)}
    if "code" in new.columns:
        new["code"] = new["code"].astype(str).str.zfill(4)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"code": str})
        before = len(old)
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates(subset=keys, keep="last")
        merged.to_csv(path, index=False)
        return {"新增": len(merged) - before, "更新": len(new) - (len(merged) - before),
                "總筆數": len(merged)}
    new.to_csv(path, index=False)
    return {"新增": len(new), "更新": 0, "總筆數": len(new)}


def _rowcount(path: str) -> int:
    return sum(1 for _ in open(path, encoding="utf-8")) - 1 if os.path.exists(path) else 0


def ingest_chips(csv_text: str, date: str) -> dict:
    """收下 T86 的資料。每行格式：代號,外資買賣超,投信買賣超,三大法人買賣超"""
    rows = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        foreign, trust, big3 = _num(parts[1]), _num(parts[2]), _num(parts[3])
        rows.append({"code": parts[0], "date": date, "foreign_net": foreign,
                     "trust_net": trust, "dealer_net": big3 - foreign - trust,
                     "margin_bal": "", "short_bal": ""})
    return _merge(CHIP_CSV, pd.DataFrame(rows), ["code", "date"])


def ingest_prices(csv_text: str, date: str) -> dict:
    """收下 MI_INDEX 的資料。每行格式：代號,開,高,低,收,成交股數"""
    rows = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        close = _num(parts[4])
        if pd.isna(close):
            continue
        rows.append({"code": parts[0], "market": "TW", "date": date,
                     "open": _num(parts[1]), "high": _num(parts[2]), "low": _num(parts[3]),
                     "close": close, "volume": _num(parts[5]),
                     # 當日不會有除權息還原問題，還原價暫時等於收盤價；
                     # 真正的還原序列由 fetch_data.py 從 Yahoo 定期覆蓋修正
                     "adjclose": close})
    return _merge(PRICE_CSV, pd.DataFrame(rows), ["code", "date"])


def ingest_margin(csv_text: str, date: str) -> dict:
    """收下 MI_MARGN 的資料。每行格式：代號,融資今日餘額,融券今日餘額"""
    rows = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append({"code": parts[0], "date": date,
                     "margin_bal": _num(parts[1]), "short_bal": _num(parts[2])})
    new = pd.DataFrame(rows)
    if new.empty or not os.path.exists(CHIP_CSV):
        return {"新增": 0, "更新": 0, "總筆數": _rowcount(CHIP_CSV)}
    # 融資券要併進既有的籌碼列，不是新增列
    old = pd.read_csv(CHIP_CSV, dtype={"code": str})
    new["code"] = new["code"].str.zfill(4)
    old = old.merge(new, on=["code", "date"], how="left", suffixes=("", "_new"))
    for col in ("margin_bal", "short_bal"):
        old[col] = old[f"{col}_new"].combine_first(old[col])
        old = old.drop(columns=[f"{col}_new"])
    old.to_csv(CHIP_CSV, index=False)
    return {"新增": 0, "更新": int(new.shape[0]), "總筆數": len(old)}


# ------------------------------------------------------------------ 新鮮度
def freshness(universe: list[str] | None = None) -> dict:
    """回報每一類資料的最新日期與覆蓋率。

    這是每日跑完後的驗收單，也是儀表板上「資料可不可信」的依據。
    """
    import config as C
    universe = universe or list(C.UNIVERSE)
    uni = set(universe)
    report: dict = {}

    for label, path, datecol in (("股價", PRICE_CSV, "date"), ("籌碼", CHIP_CSV, "date")):
        if not os.path.exists(path):
            report[label] = {"狀態": "缺檔案"}
            continue
        df = pd.read_csv(path, dtype={"code": str})
        df = df[df["code"].isin(uni)]
        latest = df[datecol].max()
        have = set(df.loc[df[datecol] == latest, "code"])
        stale = sorted(uni - have - TPEX_CODES)
        report[label] = {
            "最新日期": latest,
            "覆蓋": f"{len(have)}/{len(uni)}",
            "缺漏": stale[:12],
            "缺漏檔數": len(stale),
        }

    if os.path.exists(MACRO_CSV):
        m = pd.read_csv(MACRO_CSV)
        twii = m[m["symbol"] == "TWII"]
        report["加權指數"] = {"最新日期": twii["date"].max(), "總筆數": len(twii)}

    return report


def assert_fresh(max_lag_days: int = 4) -> list[str]:
    """回傳警告訊息清單。空清單代表資料是新的。

    max_lag_days 預設 4 天：連假加上週末最多會落後這麼多。
    """
    warns = []
    f = freshness()
    today = datetime.now().date()
    for label in ("股價", "籌碼"):
        info = f.get(label, {})
        if "最新日期" not in info:
            warns.append(f"{label}：找不到資料檔")
            continue
        lag = (today - datetime.strptime(str(info["最新日期"]), "%Y-%m-%d").date()).days
        if lag > max_lag_days:
            warns.append(f"{label}資料停在 {info['最新日期']}，已落後 {lag} 天")
        if info.get("缺漏檔數", 0) > 0:
            warns.append(f"{label}最新交易日缺 {info['缺漏檔數']} 檔："
                         f"{'、'.join(info['缺漏'][:6])}{' 等' if info['缺漏檔數'] > 6 else ''}")
    return warns


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(freshness(), ensure_ascii=False, indent=2))
    w = assert_fresh()
    print("\n警告：" + ("無" if not w else ""))
    for x in w:
        print(" ⚠️ " + x)
