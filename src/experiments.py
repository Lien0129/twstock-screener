"""參數變體比較 + 樣本內/樣本外切分。

重點不是找出報酬最高的參數（那只是過度擬合），而是看：
  1. 在樣本內排名靠前的設定，到了樣本外還站得住嗎？
  2. 這套規則到底有沒有贏過「什麼都不做，直接買進持有」？
"""
import sys, os, copy
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import backtest as B
from run_backtest import load_panel, load_regime, pct

VARIANTS = {
    "1 純技術面": {"chips": {"enabled": False}, "fund": {"enabled": False}},
    "3 技術 + 基本面(cap1.5)": {"chips": {"enabled": False}, "fund": {"enabled": True}},
    "5 技術 + 籌碼(無當沖)": {"chips": {"enabled": True, "weights": {
        "big3_20d_ratio": 0.6, "foreign_5d_ratio": 0.5, "foreign_streak": 0.4,
        "short_margin_ratio": 0.3, "trust_streak": 0.2}}, "fund": {"enabled": False}},
    "8 三層，各 cap 0.75": {
        "chips": {"enabled": True, "score_cap": 0.75, "weights": {
            "big3_20d_ratio": 0.6, "foreign_5d_ratio": 0.5, "foreign_streak": 0.4,
            "short_margin_ratio": 0.3, "trust_streak": 0.2}},
        "fund": {"enabled": True, "score_cap": 0.75}},
    "9 三層，各 cap 0.75 + 當沖": {
        "chips": {"enabled": True, "score_cap": 0.75},
        "fund": {"enabled": True, "score_cap": 0.75}},
    "10 籌碼0.5 基本面1.0": {
        "chips": {"enabled": True, "score_cap": 0.5, "weights": {
            "big3_20d_ratio": 0.6, "foreign_5d_ratio": 0.5, "foreign_streak": 0.4,
            "short_margin_ratio": 0.3, "trust_streak": 0.2}},
        "fund": {"enabled": True, "score_cap": 1.0}},
}


def run_variant(panel, regime, spec, start=None, end=None):
    swing_bak, pos_bak = copy.deepcopy(C.SWING), copy.deepcopy(C.POSITION)
    chips_bak = copy.deepcopy(C.CHIPS)
    fund_bak = copy.deepcopy(C.FUNDAMENTALS)
    try:
        C.SWING.update(spec.get("swing", {}))
        C.POSITION.update(spec.get("position", {}))
        C.CHIPS.update(spec.get("chips", {}))
        C.FUNDAMENTALS.update(spec.get("fund", {}))
        p = panel
        if start is not None:
            p = p[p["date"] >= start]
        if end is not None:
            p = p[p["date"] <= end]
        rg = regime.loc[(regime.index >= p["date"].min()) & (regime.index <= p["date"].max())] \
            if regime is not None else None
        bt = B.Backtest(p, regime=rg, sleeve_weight=spec.get("sleeve_weight", (0.5, 0.5))).run()
        return bt.stats(), bt
    finally:
        C.SWING.clear(); C.SWING.update(swing_bak)
        C.POSITION.clear(); C.POSITION.update(pos_bak)
        C.CHIPS.clear(); C.CHIPS.update(chips_bak)
        C.FUNDAMENTALS.clear(); C.FUNDAMENTALS.update(fund_bak)


def bench(panel, start=None, end=None):
    p = panel
    if start is not None:
        p = p[p["date"] >= start]
    if end is not None:
        p = p[p["date"] <= end]
    ew = B.equal_weight_universe(p)
    tsmc = B.buy_and_hold(p, "2330")
    return {
        "等權重持有": (ew.iloc[-1] / ew.iloc[0] - 1, (ew / ew.cummax() - 1).min()),
        "單押台積電": (tsmc.iloc[-1] / tsmc.iloc[0] - 1, (tsmc / tsmc.cummax() - 1).min()),
    }


def main():
    panel = load_panel()
    regime, src = load_regime(panel)
    dates = sorted(panel["date"].unique())
    split = dates[len(dates) // 2]
    print(f"樣本內 {dates[0].date()} ~ {split.date()}｜樣本外 {split.date()} ~ {dates[-1].date()}")
    print(f"大盤濾網：{src}\n")

    rows = []
    for name, spec in VARIANTS.items():
        s_in, _ = run_variant(panel, regime, spec, end=split)
        s_out, _ = run_variant(panel, regime, spec, start=split)
        rows.append({
            "變體": name,
            "樣本內報酬": s_in["總報酬"], "樣本內回撤": s_in["最大回撤"],
            "樣本外報酬": s_out["總報酬"], "樣本外回撤": s_out["最大回撤"],
            "樣本外交易數": s_out["交易次數"],
            "樣本外勝率": s_out.get("勝率", float("nan")),
        })
    df = pd.DataFrame(rows)
    fmt = df.copy()
    for c in ["樣本內報酬", "樣本內回撤", "樣本外報酬", "樣本外回撤", "樣本外勝率"]:
        fmt[c] = df[c].map(pct)
    print(fmt.to_string(index=False))

    print("\n── 同期間「什麼都不做」的基準 " + "─" * 20)
    for period, (s, e) in {"樣本內": (None, split), "樣本外": (split, None)}.items():
        b = bench(panel, s, e)
        line = "  ".join(f"{k}: 報酬 {pct(v[0])} / 回撤 {pct(v[1])}" for k, v in b.items())
        print(f"{period}  {line}")


if __name__ == "__main__":
    main()
