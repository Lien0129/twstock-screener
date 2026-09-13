"""個股體檢：把一檔股票在系統眼中的樣子完整攤開。

    python3 src/inspect_stock.py 2327

印出技術面、籌碼面、基本面的原始數值與跨股排名，並說明它今天有沒有進入
兩池的候選、卡在哪一個條件。用來回答「為什麼這檔沒被選上」或
「系統怎麼看這檔」，比只看儀表板卡片清楚得多。
"""
from __future__ import annotations

import os
import sys

import pandas as pd

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
import config as C
import strategy as S
from strategy import failed_conditions   # 與儀表板共用同一份條件檢查
from run_backtest import load_panel, load_regime


def pct_rank(day: pd.DataFrame, col: str, code: str) -> str:
    """這檔在當天所有觀察池個股裡排第幾百分位。"""
    s = day[col].dropna()
    if code not in day.index or pd.isna(day.loc[code, col]) or len(s) < 5:
        return "—"
    r = (s < day.loc[code, col]).mean()
    return f"{r*100:.0f}%"


def line(label: str, value: str, rank: str = "", note: str = ""):
    print(f"  {label:<22}{value:>14}   {rank:>5}  {note}")


def main(code: str):
    panel = load_panel()
    if code not in set(panel["code"]):
        print(f"觀察池裡沒有 {code}，資料庫也沒有它的股價。")
        print("要納入的話：把代號加進 src/config.py 的 UNIVERSE，再跑 python fetch_data.py --resume")
        return 1

    last = panel["date"].max()
    day = panel[panel["date"] == last].set_index("code")
    g = panel[panel["code"] == code].sort_values("date")
    row = day.loc[code]
    name = C.UNIVERSE.get(code, code)

    print(f"\n{'='*72}")
    print(f"  {code} {name}　　收盤資料 {last.date()}")
    print(f"{'='*72}")

    px = row["close"]
    r1 = g["adjclose"].iloc[-1] / g["adjclose"].iloc[-2] - 1
    print(f"\n  現價 {px:,.2f}　單日 {r1*100:+.2f}%　"
          f"20日 {row['mom_short']*100:+.1f}%　120日 {row['mom_long']*100:+.1f}%")
    print(f"\n  {'指標':<22}{'數值':>14}   {'排名':>5}  說明")
    print(f"  {'-'*70}")

    print("\n  【技術面】")
    for label, col, fmt, note in [
        ("收盤 vs 20MA", None, None, None),
        ("收盤 vs 60MA", None, None, None),
        ("收盤 vs 120MA", None, None, None),
    ]:
        pass
    for ma in (20, 60, 120):
        key = f"ma{ma}"
        if key in row and pd.notna(row[key]):
            diff = row["adjclose"] / row[key] - 1
            state = "站上" if diff > 0 else "跌破"
            line(f"vs {ma}MA", f"{diff*100:+.1f}%", "", state)
    line("距 60 日高點", f"{(1-row['near_high'])*100:.1f}%", pct_rank(day, "near_high", code))
    line("距 120 日高點", f"{row['dd_from_high120']*100:.1f}%", pct_rank(day, "dd_from_high120", code))
    line("5日量/60日量", f"{row['vol_ratio']:.2f}", pct_rank(day, "vol_ratio", code))
    line("RSI14", f"{row['rsi14']:.0f}", pct_rank(day, "rsi14", code),
         "過熱" if row["rsi14"] > 80 else "")
    line("ATR14 / 現價", f"{row['atr_pct']*100:.1f}%", pct_rank(day, "atr_pct", code), "波動度")

    print("\n  【籌碼面】")
    for label, col, unit in [
        ("外資5日買超/均量", "foreign_5d_ratio", "倍"),
        ("外資連買天數", "foreign_streak", "天"),
        ("投信連買天數", "trust_streak", "天"),
        ("三大法人20日", "big3_20d_ratio", ""),
        ("券資比", "short_margin_ratio", ""),
    ]:
        if col in row and pd.notna(row[col]):
            v = row[col]
            txt = f"{v:+.2f}{unit}" if unit != "天" else f"{int(v):+d}{unit}"
            line(label, txt, pct_rank(day, col, code))

    print("\n  【基本面】（只顯示，不進排序）")
    for label, col, unit in [
        ("最新月營收年增", "rev_yoy", "%"),
        ("近三月營收年增", "rev_yoy_3m", "%"),
        ("近四季淨利年增", "eps_ttm_yoy", "%"),
        ("營益率", "op_margin", "%"),
        ("毛利率季變化", "gross_margin_chg", "pp"),
        ("本益比", "PER", ""),
        ("本益比位階", "per_pctile", "%"),
    ]:
        if col in row and pd.notna(row[col]):
            v = row[col]
            txt = f"{v*100:+.1f}{unit}" if unit in ("%", "pp") and col != "PER" else f"{v:.1f}"
            if col == "per_pctile":
                txt = f"{v*100:.0f}%"
            line(label, txt, pct_rank(day, col, code))

    # 有沒有進候選
    print("\n  【今天的篩選結果】")
    regime, src_name = load_regime(panel)
    for sleeve, cfg, picker, label in (("swing", C.SWING, S.swing_candidates, "波段池"),
                                       ("position", C.POSITION, S.position_candidates, "長線池")):
        ok = True
        if regime is not None and last in regime.index:
            ok = bool(regime.loc[last, f"{sleeve}_ok"])
        cand = picker(panel[panel["date"] == last], cfg)
        in_pool = code in set(cand["code"])
        rank = None
        if in_pool:
            cand = cand.reset_index(drop=True)
            rank = cand.index[cand["code"] == code][0] + 1
        picked = in_pool and rank <= cfg["max_positions"]
        gate = "大盤濾網通過" if ok else "大盤濾網擋住，今天不開新倉"
        if picked:
            sc = cand.loc[cand["code"] == code].iloc[0]
            print(f"    {label}：入選第 {rank} 名（共 {len(cand)} 檔通過條件）"
                  f"　總分 {sc['score']:+.2f}　{gate}")
        elif in_pool:
            print(f"    {label}：通過條件但排第 {rank} 名，只取前 {cfg['max_positions']} 名　{gate}")
        else:
            print(f"    {label}：未通過硬性條件　{gate}")
            fails = failed_conditions(row, sleeve, cfg)
            for f in fails:
                print(f"        ✗ {f}")
    print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python3 src/inspect_stock.py <股票代號>")
        sys.exit(1)
    sys.exit(main(sys.argv[1].strip().zfill(4)))
