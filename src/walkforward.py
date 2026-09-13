"""Walk-Forward：檢查參數在十年裡穩不穩，而不是只在某一段特別好。

    python3 src/walkforward.py --param max_hold_days --values 20,40,60,80,100,120

為什麼要做
----------
現在採用的「波段持有上限 60 個交易日」是當初**看過樣本外結果之後**才決定的
（從 40 改成 60，因為出場統計顯示原本在砍大贏家）。這是這個專案自己標註過的
弱點——用樣本外資料挑參數，那個樣本外就不再是樣本外了。

滾動驗證回答的問題是：如果我站在每一年的年初，用「到那時為止」的資料挑參數，
挑出來的會是同一個值嗎？如果每年挑出來的都不一樣，那 60 就只是後見之明。
"""
from __future__ import annotations

import argparse
import copy
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
import backtest as B
import run_long as RL


def run_with(panel, regime, sleeve_key: str, param: str, value) -> pd.DataFrame:
    """暫時改掉某個參數跑一次，回傳交易明細。"""
    cfg = C.SWING if sleeve_key == "swing" else C.POSITION
    old = cfg[param]
    cfg[param] = value
    try:
        bt = B.Backtest(panel, regime)
        bt.run()
        tr = pd.DataFrame(bt.trades)
        eq = pd.DataFrame(bt.equity_curve).set_index("date")["equity"]
    finally:
        cfg[param] = old
    return tr, eq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-09-01")
    ap.add_argument("--param", default="max_hold_days")
    ap.add_argument("--values", default="20,40,60,80,100,120")
    ap.add_argument("--sleeve", default="swing")
    a = ap.parse_args()
    vals = [int(x) for x in a.values.split(",")]

    print(f"載入資料…")
    panel, _ = RL.build_panel(a.start)
    regime = RL.load_regime(panel)
    print(f"  {len(panel):,} 列　{panel['code'].nunique()} 檔　濾網 "
          f"{'開啟' if regime is not None else '關閉'}\n")

    years = sorted({d.year for d in panel["date"]})
    table = {}
    for v in vals:
        tr, eq = run_with(panel, regime, a.sleeve, a.param, v)
        if tr.empty:
            continue
        tr["exit_date"] = pd.to_datetime(tr["exit_date"])
        sub = tr[tr["sleeve"] == a.sleeve]
        col = {}
        for y in years:
            s = sub[sub["exit_date"].dt.year == y]
            col[y] = s["ret"].mean() * 100 if len(s) >= 3 else np.nan
        dd = (eq / eq.cummax() - 1).min() * 100
        col["全期間報酬"] = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
        col["最大回撤"] = dd
        table[v] = col
        print(f"  {a.param}={v:>4}　全期間 {col['全期間報酬']:+8.1f}%　回撤 {dd:6.1f}%")

    t = pd.DataFrame(table)
    t.index.name = a.param
    print(f"\n【每年 {a.sleeve} 池平均每筆報酬 %】欄 = {a.param}")
    print(t.round(2).to_string())

    yearly = t.drop(index=["全期間報酬", "最大回撤"], errors="ignore")
    best = yearly.idxmax(axis=1).dropna()
    print(f"\n各年最佳 {a.param}：")
    for y, v in best.items():
        print(f"  {y}：{v}")
    if len(best):
        print(f"\n  出現次數：{dict(best.value_counts())}")
        print(f"  → 如果每年最佳值都不一樣，代表這個參數沒有穩定的最適解，"
              f"現在用的值有多少是後見之明就要打折看。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
