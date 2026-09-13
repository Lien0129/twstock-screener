"""檢驗五個進場品質因子有沒有預測力。

跟這個專案裡其他因子用同一把尺，不另開後門：
  1. 每天做一次橫斷面 Spearman 相關（因子 vs 未來 20 日報酬）
  2. 切成樣本內（前一年）/ 樣本外（後一年）
  3. 採用條件：兩期同號，且樣本外 IC > 0.02

多做一件事：除了全市場橫斷面，另外算「只在候選池裡」的 IC。
理由是這個分數的用途只有一個——在已經通過硬性條件的股票之間排序、
或把其中一部分擋掉。全市場 IC 高但候選池內沒有鑑別力的因子，
對這個用途是沒用的。

    python3 src/validate_entry_score.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
import config as C
import strategy as S
from entry_quality import FACTORS, EXPECTED_SIGN, add_entry_quality
from run_backtest import load_panel

FWD = 20
MIN_NAMES = 8          # 一天至少要有這麼多檔才算得出有意義的橫斷面相關
OOS_GATE = 0.02


def forward_return(panel: pd.DataFrame, n: int = FWD) -> pd.DataFrame:
    """進場價 = 隔日開盤（跟策略假設一致），出場 = n 個交易日後收盤。

    open/high/low/close 是原始價、adjclose 已還原權息，混用會算出假報酬，
    所以先把還原比例套到 open 上。
    """
    d = panel.sort_values(["code", "date"]).copy()
    ratio = d["adjclose"] / d["close"]
    d["adj_open_"] = d["open"] * ratio
    entry = d.groupby("code")["adj_open_"].shift(-1)
    exit_ = d.groupby("code")["adjclose"].shift(-n)
    d["fwd"] = exit_ / entry - 1
    return d


def daily_ic(d: pd.DataFrame, factor: str) -> pd.Series:
    """每天一個橫斷面 Spearman 相關。"""
    out = {}
    for dt, g in d.groupby("date"):
        s = g[[factor, "fwd"]].dropna()
        if len(s) < MIN_NAMES or s[factor].nunique() < 3:
            continue
        out[dt] = s[factor].corr(s["fwd"], method="spearman")
    return pd.Series(out).dropna()


def candidate_mask(panel: pd.DataFrame) -> pd.Series:
    """標記「當天有通過任一池硬性條件」的個股——分數真正會被用到的地方。"""
    flags = []
    for dt, day in panel.groupby("date"):
        codes = set()
        for cfg, picker in ((C.SWING, S.swing_candidates),
                            (C.POSITION, S.position_candidates)):
            try:
                codes |= set(picker(day, cfg)["code"])
            except Exception:
                pass
        flags.append(pd.Series(day["code"].isin(codes).values, index=day.index))
    return pd.concat(flags).sort_index()


def report(d: pd.DataFrame, label: str, split: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for f in FACTORS:
        ic = daily_ic(d, f)
        if ic.empty:
            rows.append({"因子": f, "樣本內": np.nan, "樣本外": np.nan,
                         "天數": 0, "採用": "無資料"})
            continue
        is_ic, oos_ic = ic[ic.index < split], ic[ic.index >= split]
        i, o = is_ic.mean(), oos_ic.mean()
        exp = EXPECTED_SIGN[f]
        same = np.sign(i) == np.sign(o) and not (np.isnan(i) or np.isnan(o))
        # 因子是原始值，預期方向為 −1 時，通過的 IC 應該是負的
        passes = same and (o * exp) > OOS_GATE
        why = "✓ 採用" if passes else (
            "✗ 樣本內外符號相反" if not same else
            f"✗ 樣本外 {o*exp:+.3f} 未達 {OOS_GATE}")
        rows.append({"因子": f, "樣本內": round(i, 4), "樣本外": round(o, 4),
                     "方向調整後樣本外": round(o * exp, 4),
                     "天數": len(ic), "採用": why})
    t = pd.DataFrame(rows)
    print(f"\n【{label}】  IC = 每日橫斷面 Spearman（因子 vs 未來 {FWD} 日報酬）")
    print(f"  樣本內 < {split.date()} ≤ 樣本外")
    print(t.to_string(index=False))
    return t


def main() -> int:
    panel = load_panel()
    panel = add_entry_quality(panel, hard_stop=abs(C.SWING["hard_stop"]))
    d = forward_return(panel)

    dates = d["date"].sort_values().unique()
    split = pd.Timestamp(dates[len(dates) // 2])
    print("=" * 78)
    print(f"  進場品質因子檢驗　{pd.Timestamp(dates[0]).date()} ~ "
          f"{pd.Timestamp(dates[-1]).date()}　{d['code'].nunique()} 檔")
    print(f"  採用門檻：樣本內外同號 且 方向調整後樣本外 IC > {OOS_GATE}")
    print("=" * 78)

    full = report(d, "全市場橫斷面", split)

    mask = candidate_mask(panel)
    cand = d.loc[mask[mask].index] if mask.any() else d.iloc[0:0]
    print(f"\n候選池樣本數 {len(cand):,}（占全體 {len(cand)/len(d)*100:.1f}%）")
    sub = report(cand, "只看候選池（分數實際會被用到的地方）", split) \
        if len(cand) > 500 else None

    print("\n" + "=" * 78)
    keep = [r["因子"] for _, r in full.iterrows() if str(r["採用"]).startswith("✓")]
    keep_c = [r["因子"] for _, r in sub.iterrows()
              if str(r["採用"]).startswith("✓")] if sub is not None else []
    print(f"  全市場通過：{keep or '（無）'}")
    print(f"  候選池通過：{keep_c or '（無）'}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
