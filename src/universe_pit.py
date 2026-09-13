"""Point-in-time 觀察池：每個月用「當時」的成交值重新排名。

為什麼一定要這樣做
------------------
現有的 106 檔是用 2026 年 8 月的成交值挑的。拿它回測十年，等於拿今天的贏家
去跑十年前——川湖今天 14,180，2016 年是完全不同量級的公司。回測會非常漂亮，
而且完全是假的。

這裡改成：每個月底用「截至那一天」的近 60 個交易日日均成交值排名，取前 N 檔，
作為下個月的觀察池。2018 年的池子只會知道 2018 年的資訊。

修不掉的部分
------------
已下市／被合併的公司 Yahoo 沒有可靠歷史，所以殘留的存活偏誤仍在。
資料裡各年的股票數是 910（2016）→ 1088（2026），成長全部來自新上市，
沒有任何一檔「消失」——真實市場不是這樣。所以最後的數字仍有向上偏誤，
只是幅度比「用期末成交值挑股票」小得多。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDE = os.path.join(ROOT, "data", "wide", "twstock_wide.csv.gz")

TURNOVER_WINDOW = 60    # 近 60 個交易日日均成交值
MIN_HISTORY = 150       # 至少要有這麼多天，指標才算得出來（120MA + 緩衝）
TOP_N = 100


def load_wide(start: str | None = None) -> pd.DataFrame:
    d = pd.read_csv(WIDE, dtype={"code": str})
    d["date"] = pd.to_datetime(d["date"])
    if start:
        d = d[d["date"] >= pd.Timestamp(start)]
    return d.sort_values(["code", "date"]).reset_index(drop=True)


def build(wide: pd.DataFrame, top_n: int = TOP_N,
          cumulative: bool = False, floor: float | None = None,
          floor_months: int = 3) -> pd.DataFrame:
    """回傳 (date, code) 兩欄：每一天各有哪些股票在觀察池裡。

    排名用月底那一天決定，套用到下個月的每一天。這樣不會出現
    「用月底的資訊決定月初該買什麼」的偷看。

    cumulative=True：只進不出。進過池子就永遠留著。
      仍然是 point-in-time 的——一檔股票要到它**當時**真的擠進前 N 名
      才會被加進來，不會用未來的資訊提前納入。所以這不是作弊，
      是一個真的可以即時執行的規則。
      代價是池子會單調膨脹，而且會累積「曾經很熱、現在沒量」的股票。

    floor：絕對成交值下限（元）。配 cumulative 用——只進不出，但日均
      成交值連續 floor_months 個月低於這個數字就踢掉。這樣保留了
      「不因為別人變強就被擠掉」的精神，只清掉真的死掉的股票。
      None = 不設下限。
    """
    w = wide.copy()
    w["turnover"] = w["close"] * w["volume"]
    w["ma_turnover"] = (w.groupby("code")["turnover"]
                        .transform(lambda s: s.rolling(TURNOVER_WINDOW, min_periods=40).mean()))
    w["bars"] = w.groupby("code").cumcount() + 1
    w["ym"] = w["date"].dt.to_period("M")

    # 每個月的最後一個交易日 = 排名決定日
    last_of_month = w.groupby("ym")["date"].max()
    months = sorted(last_of_month.index)

    rows = []
    all_dates = np.sort(w["date"].unique())
    carried: set[str] = set()          # cumulative 模式用：已經進過池子的
    below: dict[str, int] = {}         # 連續幾個月低於 floor
    for i, ym in enumerate(months[:-1]):
        decide_on = last_of_month[ym]
        snap = w[(w["date"] == decide_on) & (w["bars"] >= MIN_HISTORY)]
        snap = snap.dropna(subset=["ma_turnover"])
        if snap.empty:
            continue
        top = snap.nlargest(top_n, "ma_turnover")["code"].tolist()

        if cumulative:
            carried |= set(top)
            if floor is not None:
                # 當月成交值。查不到的（停牌、下市）當作低於門檻。
                cur = snap.set_index("code")["ma_turnover"]
                for c in list(carried):
                    v = cur.get(c, 0.0)
                    below[c] = 0 if v >= floor else below.get(c, 0) + 1
                    if below[c] >= floor_months and c not in top:
                        carried.discard(c)
                        below.pop(c, None)
            picked = sorted(carried)
        else:
            picked = top

        # 套用到下個月
        nxt = months[i + 1]
        days = all_dates[(all_dates > decide_on.to_datetime64())
                         & (all_dates <= last_of_month[nxt].to_datetime64())]
        for d in days:
            for c in picked:
                rows.append((d, c))
    out = pd.DataFrame(rows, columns=["date", "code"])
    return out


def apply_to(wide: pd.DataFrame, pit: pd.DataFrame) -> pd.DataFrame:
    """只保留 (date, code) 在觀察池裡的列。

    注意順序：指標要先在**完整歷史**上算完再過濾，否則一檔股票剛進池子時
    120MA 會是空的。所以呼叫端必須先 build_panel 再 apply_to。
    """
    key = pd.MultiIndex.from_frame(pit[["date", "code"]])
    idx = pd.MultiIndex.from_frame(wide[["date", "code"]])
    return wide[idx.isin(key)].reset_index(drop=True)


def summary(pit: pd.DataFrame) -> pd.DataFrame:
    """每年觀察池的組成與換手率——用來確認它真的在變。"""
    p = pit.copy()
    p["year"] = p["date"].dt.year
    rows = []
    prev = None
    for y, g in p.groupby("year"):
        codes = set(g["code"])
        turn = (len(codes - prev) / len(prev) * 100) if prev else float("nan")
        rows.append({"年": y, "出現過的股票數": len(codes), "與前一年不同的比例": round(turn, 1)})
        prev = codes
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    w = load_wide()
    pit = build(w)
    print(f"觀察池涵蓋 {pit['date'].nunique():,} 個交易日，"
          f"用到 {pit['code'].nunique()} 檔不同股票\n")
    print(summary(pit).to_string(index=False))
    out = os.path.join(ROOT, "data", "wide", "universe_pit.csv.gz")
    pit.to_csv(out, index=False, compression="gzip")
    print(f"\n已寫入 {os.path.relpath(out, ROOT)}")
