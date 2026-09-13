"""產生儀表板要用的圖表與驗證資料（權益曲線、出場統計、因子檢定）。"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import backtest as B
import chips as CH
import fundamentals as FU
from run_backtest import load_panel, load_regime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")

# 因子是否採用的判定：樣本內外 IC 同號且樣本外 IC > 0.02 才進評分
ADOPT_NOTE = {
    "rev_yoy_3m": ("近三月營收年增", "採用"),
    "rev_yoy": ("單月營收年增", "採用"),
    "op_margin": ("營益率", "採用"),
    "gross_margin_chg": ("毛利率季變化", "採用"),
    "eps_ttm_yoy": ("近四季 EPS 年增", "採用"),
    "per_pctile": ("本益比位階", "只顯示"),
    "pbr_pctile": ("股價淨值比位階", "只顯示"),
    "gross_margin": ("毛利率", "剔除"),
    "dividend_yield": ("現金殖利率", "剔除"),
    "foreign_5d_ratio": ("外資 5 日買超比", "採用"),
    "foreign_streak": ("外資連買天數", "採用"),
    "big3_20d_ratio": ("三大法人 20 日", "採用"),
    "vol_zscore_20d": ("成交量異常（熱度代理）", "採用"),
    "trust_streak": ("投信連買天數", "採用"),
    "short_margin_ratio": ("券資比", "只顯示"),
    "margin_chg_5d": ("融資 5 日變化", "只顯示"),
    "turnover_spike": ("量能倍數", "剔除"),
    "trust_5d_ratio": ("投信 5 日買超比", "剔除"),
    "dealer_5d_ratio": ("自營商 5 日買超比", "剔除"),
}


def main():
    panel = load_panel()
    regime, _ = load_regime(panel)
    bt = B.Backtest(panel, regime=regime).run()
    bt.equity.to_csv(os.path.join(OUT, "equity_curve.csv"))
    bt.trade_log.to_csv(os.path.join(OUT, "trades.csv"), index=False)

    eq = bt.equity["equity"]
    ew = B.equal_weight_universe(panel).reindex(eq.index).ffill()
    tw = B.buy_and_hold(panel, "2330").reindex(eq.index).ffill()
    idx = eq.index
    sample = sorted(set(list(range(0, len(idx), 3)) + [len(idx) - 1]))

    out = {
        "dates": [str(idx[i].date()) for i in sample],
        "strategy": [round(eq.iloc[i] / eq.iloc[0], 4) for i in sample],
        "equalweight": [round(ew.iloc[i] / ew.iloc[0], 4) for i in sample],
        "tsmc": [round(tw.iloc[i] / tw.iloc[0], 4) for i in sample],
    }

    t = bt.trade_log
    out["exit_reasons"] = [{"reason": k, "n": int(len(v)), "avg": round(float(v["ret"].mean()), 4)}
                           for k, v in t.groupby("reason")]
    out["sleeve"] = [{"sleeve": k, "n": int(len(v)), "win": round(float((v["ret"] > 0).mean()), 3),
                      "avg": round(float(v["ret"].mean()), 4), "pnl": round(float(v["pnl"].sum()))}
                     for k, v in t.groupby("sleeve")]

    chip_rep = CH.factor_report(panel, horizon=20)
    # 基本面資料覆蓋率不足時不出報表，免得用 44% 的樣本得出看似精確的結論
    fund_cols = [f for f in FU.FACTORS if f in panel.columns]
    cov = panel["rev_yoy"].notna().mean() if "rev_yoy" in panel.columns else 0
    reports = [(chip_rep, "籌碼")]
    if fund_cols and cov >= 0.8:
        reports.append((CH.factor_report(panel, factors=fund_cols, horizon=20), "基本面"))
    else:
        print(f"    基本面覆蓋率只有 {cov:.0%}，因子檢定暫不列入")
    rows = []
    for rep, group in reports:
        for _, r in rep.iterrows():
            if pd.isna(r["樣本外IC"]):
                continue
            label, verdict = ADOPT_NOTE.get(r["因子"], (r["因子"], "—"))
            rows.append({"factor": label, "group": group, "is_ic": round(float(r["樣本內IC"]), 4),
                         "oos_ic": round(float(r["樣本外IC"]), 4), "verdict": verdict})
    out["factors"] = sorted(rows, key=lambda x: -x["oos_ic"])

    with open(os.path.join(OUT, "chart_data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"chart_data.json：{len(sample)} 個時間點、{len(out['factors'])} 個因子")


if __name__ == "__main__":
    main()
