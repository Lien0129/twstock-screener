"""觀察池規則對比：每月前 100（會剔除） vs 累積只進不出。

    python3 src/compare_universe.py --start 2016-09-01

要回答三個問題
--------------
1. 只進不出會不會傷害績效？（報酬 / 回撤 / Sharpe / 勝率）
2. 池子裡累積的「曾經很熱、現在沒量」的股票，真的會被選中嗎？
   → 統計每一筆交易在**進場當下**的成交值排名
3. 流動性到底有沒有在傷害績效？
   → 依進場當下的 60 日均成交值分組，看各組報酬

第 3 題比前兩題重要：如果低流動性的交易其實賺得跟高流動性一樣多，
那「只進不出會納入爛股票」這個擔憂就是空的，不需要設任何下限。
"""
from __future__ import annotations

import argparse
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
import indicators as I
import backtest as B
import universe_pit as U
from run_long import load_regime, stats

ROOT = os.path.dirname(SRC)
OUT = os.path.join(ROOT, "output")


def monthly_ranks(wide: pd.DataFrame) -> pd.DataFrame:
    """每個月底每檔股票的成交值排名與絕對金額。

    回傳 code / eff_from / eff_to / rank / ma_turnover，
    eff_from~eff_to 是這個排名生效的期間（下個月）。
    """
    w = wide.copy()
    w["turnover"] = w["close"] * w["volume"]
    w["ma_turnover"] = (w.groupby("code")["turnover"]
                        .transform(lambda s: s.rolling(U.TURNOVER_WINDOW,
                                                       min_periods=40).mean()))
    w["bars"] = w.groupby("code").cumcount() + 1
    w["ym"] = w["date"].dt.to_period("M")
    last = w.groupby("ym")["date"].max()
    months = sorted(last.index)

    out = []
    for i, ym in enumerate(months[:-1]):
        d = last[ym]
        snap = w[(w["date"] == d) & (w["bars"] >= U.MIN_HISTORY)].dropna(subset=["ma_turnover"])
        if snap.empty:
            continue
        snap = snap.copy()
        snap["rank"] = snap["ma_turnover"].rank(ascending=False, method="min")
        nxt_end = last[months[i + 1]]
        for _, r in snap.iterrows():
            out.append((r["code"], d, nxt_end, r["rank"], r["ma_turnover"]))
    return pd.DataFrame(out, columns=["code", "eff_from", "eff_to", "rank", "ma_turnover"])


def attach_rank(tr: pd.DataFrame, ranks: pd.DataFrame) -> pd.DataFrame:
    """把每一筆交易接上「進場當下」的成交值排名。

    用 merge_asof 對齊到最近一次**已經生效**的月底排名，不會用到未來。
    """
    if tr.empty:
        return tr
    t = tr.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"])
    t = t.sort_values("entry_date")
    r = ranks.sort_values("eff_from")
    merged = pd.merge_asof(t, r[["code", "eff_from", "rank", "ma_turnover"]],
                           left_on="entry_date", right_on="eff_from",
                           by="code", direction="backward")
    return merged


def run_one(label: str, pit: pd.DataFrame, wide: pd.DataFrame,
            use_regime: bool) -> tuple[dict, pd.DataFrame]:
    codes = set(pit["code"])
    w = wide[wide["code"].isin(codes)].copy()
    w["market"] = "TWSE"
    # 指標先在完整歷史上算，再套 PIT 過濾——順序反了的話剛進池的股票沒有均線
    panel = I.build_panel(w, C.SWING, C.POSITION)
    panel = U.apply_to(panel, pit)
    regime = load_regime(panel) if use_regime else None

    bt = B.Backtest(panel, regime)
    bt.run()
    eq = pd.DataFrame(bt.equity_curve).set_index("date")["equity"]
    tr = pd.DataFrame(bt.trades)
    s = stats(eq, tr)
    s["觀察池檔數"] = pit["code"].nunique()
    s["label"] = label
    return s, tr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-09-01")
    ap.add_argument("--no-regime", action="store_true")
    ap.add_argument("--floor", default=None,
                    help="累積模式的絕對成交值下限（億元），可給多個：0.5,1,3")
    a = ap.parse_args()
    floors = [float(x) for x in str(a.floor).split(",")] if a.floor else []

    print(f"載入十年日線（起點 {a.start}）…")
    wide = U.load_wide()
    print(f"  {len(wide):,} 列　{wide['code'].nunique()} 檔")

    print("建兩種觀察池…")
    pit_rot = U.build(wide)
    pit_cum = U.build(wide, cumulative=True)
    pits = [("每月前100（會剔除）", pit_rot), ("累積只進不出", pit_cum)]
    for fl in floors:
        pits.append((f"累積＋下限{fl}億", U.build(wide, cumulative=True, floor=fl * 1e8)))

    for label, p in pits:
        p2 = p[p["date"] >= pd.Timestamp(a.start)]
        per_day = p2.groupby("date").size()
        print(f"  {label:<18} 用到 {p2['code'].nunique():>3} 檔　"
              f"每日池子 {per_day.min():>3}~{per_day.max():>3} 檔（末期 {per_day.iloc[-1]}）")

    print("\n算月底排名表…")
    ranks = monthly_ranks(wide)

    results, trades = [], {}
    for label, p in pits:
        p2 = p[p["date"] >= pd.Timestamp(a.start)]
        print(f"\n跑回測：{label} …")
        s, tr = run_one(label, p2, wide, not a.no_regime)
        results.append(s)
        trades[label] = attach_rank(tr, ranks)
        print(f"  總報酬 {s['總報酬']:+.1f}%　回撤 {s['最大回撤']:.1f}%　"
              f"Sharpe {s['Sharpe']:.2f}　勝率 {s['勝率']:.1f}%　{s['交易筆數']} 筆")

    print("\n" + "=" * 78)
    print(f"  {'觀察池規則':<20}{'檔數':>5}{'總報酬':>10}{'年化':>8}{'回撤':>8}"
          f"{'Sharpe':>8}{'勝率':>7}{'交易':>6}")
    print("  " + "-" * 74)
    for s in results:
        print(f"  {s['label']:<20}{s['觀察池檔數']:>5}{s['總報酬']:>+9.1f}%"
              f"{s['年化']:>+7.1f}%{s['最大回撤']:>7.1f}%{s['Sharpe']:>8.2f}"
              f"{s['勝率']:>6.1f}%{s['交易筆數']:>6}")
    print("=" * 78)

    # ---------------- 被選中的標的，進場當下排名在哪 ----------------
    print("\n【被選中的標的，進場當下的成交值排名】")
    print(f"  {'觀察池規則':<20}{'≤100':>7}{'101-300':>9}{'301-500':>9}{'>500':>7}{'查無':>6}")
    print("  " + "-" * 60)
    for label, t in trades.items():
        if t.empty or "rank" not in t:
            continue
        r = t["rank"]
        bins = [(r <= 100).sum(), ((r > 100) & (r <= 300)).sum(),
                ((r > 300) & (r <= 500)).sum(), (r > 500).sum(), r.isna().sum()]
        print(f"  {label:<20}" + "".join(f"{b:>7}" if i in (0, 3) else f"{b:>9}"
                                         for i, b in enumerate(bins[:4]))
              + f"{bins[4]:>6}")

    # ---------------- 流動性分組報酬 ----------------
    print("\n【依進場當下 60 日均成交值分組：流動性有沒有在傷害績效】")
    edges = [0, 0.3e8, 0.5e8, 1e8, 3e8, 10e8, np.inf]
    names = ["<0.3億", "0.3–0.5億", "0.5–1億", "1–3億", "3–10億", ">10億"]
    for label, t in trades.items():
        if t.empty or "ma_turnover" not in t:
            continue
        print(f"\n  ◆ {label}")
        print(f"    {'成交值級距':<12}{'筆數':>6}{'勝率':>8}{'平均報酬':>10}{'中位':>9}{'總貢獻':>10}")
        print("    " + "-" * 55)
        tt = t.dropna(subset=["ma_turnover"])
        tt = tt.assign(bucket=pd.cut(tt["ma_turnover"], edges, labels=names))
        for n in names:
            sub = tt[tt["bucket"] == n]
            if not len(sub):
                continue
            print(f"    {n:<12}{len(sub):>6}{(sub['ret']>0).mean()*100:>7.1f}%"
                  f"{sub['ret'].mean()*100:>+9.2f}%{sub['ret'].median()*100:>+8.2f}%"
                  f"{sub['ret'].sum()*100:>+9.1f}%")

    # ---------------- 差距是真的還是雜訊 ----------------
    # 這個專案吃過虧：好幾次「看起來明顯」的差距，bootstrap 一跑信賴區間就重疊。
    # 對每筆交易報酬做重抽，看兩組的平均報酬差是不是穩定同號。
    print("\n【差距是真的嗎：對交易報酬做 2000 次 bootstrap】")
    base = trades.get("每月前100（會剔除）")
    if base is not None and not base.empty:
        rng = np.random.default_rng(0)
        b = base["ret"].values
        for label, t in trades.items():
            if label == "每月前100（會剔除）" or t.empty:
                continue
            o = t["ret"].values
            diffs = np.array([rng.choice(b, len(b), True).mean()
                              - rng.choice(o, len(o), True).mean() for _ in range(2000)])
            lo, hi = np.percentile(diffs, [2.5, 97.5])
            same = "是" if (lo > 0) == (hi > 0) else "否（區間跨 0，分不出好壞）"
            print(f"  每月前100 − {label}：平均每筆差 {diffs.mean()*100:+.2f}%"
                  f"　95% 區間 [{lo*100:+.2f}%, {hi*100:+.2f}%]　方向穩定：{same}")

    os.makedirs(OUT, exist_ok=True)
    pd.DataFrame(results).to_csv(os.path.join(OUT, "universe_compare.csv"), index=False)
    for label, t in trades.items():
        safe = label.replace("/", "_").replace("（", "_").replace("）", "")
        t.to_csv(os.path.join(OUT, f"trades_{safe}.csv"), index=False)
    print(f"\n明細 → output/universe_compare.csv、output/trades_*.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
