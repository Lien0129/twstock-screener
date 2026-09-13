"""把新抓到的日線資料併入歷史庫，自動去重。

用法：
    python3 src/ingest.py new_rows.csv
新檔需含欄位 code,date,open,high,low,close,volume（adjclose 沒有就用 close 補）。
"""
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "data", "twstock_history.csv")

COLS = ["code", "market", "date", "open", "high", "low", "close", "volume", "adjclose"]


def roc_to_iso(s: str) -> str:
    """民國年 115/08/24 → 2026-08-24；已是西元格式就原樣回傳。"""
    s = str(s).strip()
    if "/" in s:
        y, m, d = s.split("/")
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
    return s


def ingest(new_path: str) -> tuple[int, int]:
    new = pd.read_csv(new_path, dtype={"code": str})
    new["code"] = new["code"].str.zfill(4)
    new["date"] = new["date"].map(roc_to_iso)
    if "adjclose" not in new:
        new["adjclose"] = new["close"]
    if "market" not in new:
        new["market"] = "TW"
    new = new[COLS]

    if os.path.exists(HIST):
        old = pd.read_csv(HIST, dtype={"code": str})
        merged = pd.concat([old, new], ignore_index=True)
    else:
        old, merged = pd.DataFrame(columns=COLS), new

    before = len(merged)
    merged = (merged.drop_duplicates(subset=["code", "date"], keep="last")
                    .sort_values(["code", "date"]).reset_index(drop=True))
    merged.to_csv(HIST, index=False)
    return len(merged) - len(old), before - len(merged)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python3 src/ingest.py <新資料檔.csv>")
    added, dropped = ingest(sys.argv[1])
    print(f"新增 {added} 筆，去除重複 {dropped} 筆 → {HIST}")
