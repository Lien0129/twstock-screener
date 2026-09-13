"""長期回測：point-in-time 觀察池 + 純技術面 + 分盤勢報告。

    python3 src/run_long.py --start 2016-09-01          # 十年（長線池）
    python3 src/run_long.py --start 2021-09-01          # 五年（波段池）
    python3 src/run_long.py --no-regime                 # 不開大盤濾網

跟 run_backtest.py 的差別
-------------------------
1. 觀察池是 point-in-time 的（每月重排），不是固定的 106 檔
2. 只有技術面。1088 檔 × 10 年拿不到籌碼與基本面，FinMind 配額撐不住
3. 輸出多一份分盤勢報告：多頭暴漲 / 空頭暴跌 / 箱型盤整 / 陰跌 各自的表現

所以這裡的數字**不能直接跟 228.7% 比**——那個含籌碼面、固定觀察池、只有兩年。
要對齊得看 --bridge 跑出來的橋接數字。
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

ROOT = os.path.dirname(SRC)
PIT = os.path.join(ROOT, "data", "wide", "universe_pit.csv.gz")
TWII = os.path.join(ROOT, "data", "twii_10y.csv")


def build_panel(start: str | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    pit = pd.read_csv(PIT, dtype={"code": str})
    pit["date"] = pd.to_datetime(pit["date"])
    if start:
        pit = pit[pit["date"] >= pd.Timestamp(start)]
    codes = set(pit["code"])

    wide = U.load_wide()
    # 只留進過觀察池的股票，但保留它們的**完整歷史**——
    # 指標必須先在完整序列上算完，否則剛進池子的股票 120MA 會是空的
    wide = wide[wide["code"].isin(codes)].copy()
    wide["market"] = "TWSE"
    panel = I.build_panel(wide, C.SWING, C.POSITION)
    panel = U.apply_to(panel, pit)
    return panel, pit


def load_regime(panel: pd.DataFrame) -> pd.DataFrame | None:
    if not os.path.exists(TWII):
        return None
    m = pd.read_csv(TWII)
    m["date"] = pd.to_datetime(m["date"])
    tw = m.sort_values("date").set_index("date")["close"]
    cov = tw.index.isin(panel["date"].unique()).sum() / panel["date"].nunique()
    if cov < 0.95:
        print(f"⚠️  加權指數只覆蓋 {cov:.0%} 的交易日，濾網會在缺資料的日子失效")
    return pd.DataFrame({
        "swing_ok": tw > tw.rolling(C.REGIME["swing_ma"]).mean(),
        "position_ok": tw > tw.rolling(C.REGIME["position_ma"]).mean(),
    }).dropna()


def classify_regime(tw: pd.Series) -> pd.Series:
    """把每一天歸到四種盤勢。

    第一版壞掉了：定義是「60 日報酬 < −12% 且低波動」才算陰跌磨盤，
    結果 2022 整年的下跌波動並不低，全被歸到「空頭暴跌」，
    陰跌磨盤只分到 1 天——四分類實際只有三類在運作。

    改用三個維度，而且拿已知事件驗證（2018Q4、2020Q1、2022 全年）：
      dd    ：距一年高點的回檔幅度 —— 判斷有沒有在空頭裡
      trend ：指數在不在 120MA 之上 —— 判斷方向
      speed ：20 日已實現波動的百分位 —— 判斷是急殺還是慢磨
    """
    ma120 = tw.rolling(120).mean()
    dd = tw / tw.rolling(250, min_periods=60).max() - 1
    r60 = tw.pct_change(60)
    vol = tw.pct_change().rolling(20).std()
    fast = vol > vol.rolling(500, min_periods=120).quantile(0.75)

    out = pd.Series("箱型盤整", index=tw.index)
    out[(tw > ma120) & (r60 > 0.10)] = "多頭暴漲"
    out[(dd < -0.10) & fast] = "空頭暴跌"
    out[(dd < -0.10) & ~fast] = "陰跌磨盤"
    return out


def stats(eq: pd.Series, tr: pd.DataFrame) -> dict:
    total = eq.iloc[-1] / eq.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    rets = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    return {
        "總報酬": total * 100, "年化": cagr * 100, "最大回撤": dd * 100,
        "Sharpe": (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() else np.nan,
        "勝率": (tr["ret"] > 0).mean() * 100 if len(tr) else np.nan,
        "交易筆數": len(tr),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-09-01")
    ap.add_argument("--no-regime", action="store_true")
    a = ap.parse_args()

    print(f"載入資料（起點 {a.start}）…")
    panel, pit = build_panel(a.start)
    print(f"  panel {len(panel):,} 列　{panel['code'].nunique()} 檔　"
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}")

    regime = None if a.no_regime else load_regime(panel)
    print(f"  大盤濾網：{'關閉' if regime is None else '開啟（加權指數）'}")

    print("\n跑回測…")
    bt = B.Backtest(panel, regime)
    bt.run()
    eq = pd.DataFrame(bt.equity_curve).set_index("date")["equity"]
    tr = pd.DataFrame(bt.trades)

    s = stats(eq, tr)
    print(f"\n{'='*70}")
    print(f"  總報酬 {s['總報酬']:+.1f}%　年化 {s['年化']:+.1f}%　"
          f"最大回撤 {s['最大回撤']:.1f}%　Sharpe {s['Sharpe']:.2f}")
    print(f"  勝率 {s['勝率']:.1f}%　交易 {s['交易筆數']} 筆")

    # 買進持有對照：用**完整股價**建，不能用 PIT 過濾後的 panel——
    # 那份資料在股票離開觀察池時就沒有列了，dropna 之後只會剩下少數
    # 「全程都在池子裡」的股票，那本身就是存活者樣本（跑出 +1153% 那次）。
    wide_all = U.load_wide(start=a.start)
    px = wide_all.pivot_table(index="date", columns="code", values="adjclose")
    px = px.dropna(axis=1, how="any")     # 全程都有報價 = 期初就上市且沒下市
    if px.shape[1] >= 50:
        bh_eq = (px / px.iloc[0]).mean(axis=1)
        print(f"  對照：全市場等權重買進持有（{px.shape[1]} 檔，不再平衡）"
              f"{(bh_eq.iloc[-1]-1)*100:+.1f}%　回撤 {((bh_eq/bh_eq.cummax()-1).min())*100:.1f}%")
        print(f"        註：此對照仍排除了期間內下市的公司，數字偏高")
    print(f"{'='*70}")

    if os.path.exists(TWII):
        m = pd.read_csv(TWII); m["date"] = pd.to_datetime(m["date"])
        tw = m.sort_values("date").set_index("date")["close"]
        reg = classify_regime(tw).reindex(eq.index).ffill()
        tr["exit_date"] = pd.to_datetime(tr["exit_date"])
        tr["盤勢"] = reg.reindex(tr["exit_date"]).values
        print("\n【分盤勢表現】以出場日所處的盤勢歸類")
        print(f"  {'盤勢':<10}{'交易日數':>8}{'交易筆數':>9}{'勝率':>8}{'平均報酬':>10}{'中位':>9}")
        print("  " + "-" * 56)
        for g in ("多頭暴漲", "箱型盤整", "空頭暴跌", "陰跌磨盤"):
            days = int((reg == g).sum())
            sub = tr[tr["盤勢"] == g]
            if days == 0:
                continue
            if len(sub):
                print(f"  {g:<10}{days:>8}{len(sub):>9}{(sub['ret']>0).mean()*100:>7.1f}%"
                      f"{sub['ret'].mean()*100:>+9.2f}%{sub['ret'].median()*100:>+8.2f}%")
            else:
                print(f"  {g:<10}{days:>8}{0:>9}{'—':>8}{'—':>10}{'—':>9}")

    out = os.path.join(ROOT, "output", "long_equity.csv")
    eq.to_csv(out)
    tr.to_csv(os.path.join(ROOT, "output", "long_trades.csv"), index=False)
    print(f"\n權益曲線 → {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
