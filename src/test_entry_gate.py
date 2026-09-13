"""第 4 關：把進場品質因子當成門檻，實際回測有沒有變好。

前面幾關測的都是「因子跟未來報酬有沒有相關」。相關不等於有用——這個專案
已經有過一次教訓：基本面因子的 IC 是所有因子裡最高的（樣本外 0.105），
加進評分之後報酬卻從 228.7% 掉到 158.7%。原因是 IC 衡量的是整個橫斷面的
排序能力，而策略每天只買前 5 名。

所以這裡直接把門檻裝上去跑完整回測，比較總報酬 / 最大回撤 / 勝率。
沒有全面變好就不採用。

    python3 src/test_entry_gate.py
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
import backtest as B
from entry_quality import add_entry_quality
from run_backtest import load_panel, load_regime

ORIG_SWING = S.swing_candidates
ORIG_POS = S.position_candidates


def make_gate(col: str, lo: float | None, hi: float | None):
    """回傳一組被門檻包過的候選函式。lo/hi 是絕對數值門檻，None 表示不設限。"""
    def wrap(orig):
        def inner(day, cfg):
            c = orig(day, cfg)
            if c.empty or col not in c:
                return c
            m = pd.Series(True, index=c.index)
            if lo is not None:
                m &= (c[col] >= lo) | c[col].isna()
            if hi is not None:
                m &= (c[col] <= hi) | c[col].isna()
            return c[m]
        return inner
    return wrap(ORIG_SWING), wrap(ORIG_POS)


def run(panel, regime, label: str) -> dict:
    bt = B.Backtest(panel, regime)
    bt.run()
    eq = pd.DataFrame(bt.equity_curve).set_index("date")["equity"]
    tr = pd.DataFrame(bt.trades)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    rets = eq.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() else float("nan")
    win = (tr["ret"] > 0).mean() if len(tr) else float("nan")
    return {"設定": label, "總報酬": total, "最大回撤": dd, "Sharpe": sharpe,
            "勝率": win, "交易筆數": len(tr)}


def main() -> int:
    panel = add_entry_quality(load_panel(), hard_stop=abs(C.SWING["hard_stop"]))
    regime, _ = load_regime(panel)
    dates = np.sort(panel["date"].unique())
    split = pd.Timestamp(dates[len(dates) // 2])

    # 門檻取自樣本內分位數，不是全期間 —— 否則等於偷看未來
    cand_is = panel[panel["date"] < split]
    q = {c: cand_is[c].quantile([0.1, 0.2, 0.8, 0.9]).to_dict()
         for c in ("stop_atr_fit", "ext_ma20")}
    print("樣本內分位數門檻：")
    for k, v in q.items():
        print(f"  {k}: " + "  ".join(f"{int(p*100)}%={x:.3f}" for p, x in v.items()))

    GATES = [
        ("基準（不設門檻）", None, None, None),
        ("擋掉停損最窄的 10%", "stop_atr_fit", q["stop_atr_fit"][0.1], None),
        ("擋掉停損最窄的 20%", "stop_atr_fit", q["stop_atr_fit"][0.2], None),
        ("擋掉最乖離的 10%", "ext_ma20", None, q["ext_ma20"][0.9]),
        ("擋掉最乖離的 20%", "ext_ma20", None, q["ext_ma20"][0.8]),
        ("只留乖離最高的 20%", "ext_ma20", q["ext_ma20"][0.8], None),
    ]

    for label, split_period in (("全期間", None), ("樣本外", split)):
        p = panel if split_period is None else panel[panel["date"] >= split_period]
        r = regime if split_period is None else regime[regime.index >= split_period]
        rows = []
        for name, col, lo, hi in GATES:
            if col is None:
                S.swing_candidates, S.position_candidates = ORIG_SWING, ORIG_POS
            else:
                S.swing_candidates, S.position_candidates = make_gate(col, lo, hi)
            try:
                rows.append(run(p, r, name))
            except Exception as e:
                rows.append({"設定": name, "總報酬": float("nan"), "錯誤": str(e)[:40]})
        S.swing_candidates, S.position_candidates = ORIG_SWING, ORIG_POS
        t = pd.DataFrame(rows)
        for c in ("總報酬", "最大回撤", "勝率"):
            if c in t:
                t[c] = (t[c] * 100).round(1)
        if "Sharpe" in t:
            t["Sharpe"] = t["Sharpe"].round(2)
        print(f"\n=== {label} ===")
        print(t.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
