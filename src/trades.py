"""交易紀錄：記下每一筆真實成交，累積出「你自己的」勝率與賺賠比。

    python3 src/trades.py add 6213 聯茂 --in 2026-08-27 --entry 598 --out 2026-08-27 --exit 570 \
        --shares 1000 --net -29353 --sleeve swing --note "追兩根漲停後高點進場"
    python3 src/trades.py list
    python3 src/trades.py stats

為什麼要記
----------
回測告訴你「這套規則過去會怎樣」，交易紀錄告訴你「你實際做得到什麼」。
兩者常常差很多——差距通常出在進場價、出場紀律、以及有沒有真的照規則走。
累積 20~30 筆之後，這份紀錄比任何回測都更能預測你未來的績效。

淨損益一律填實際對帳單上的數字，不要用理論值。券商手續費折扣、零股、
分批成交都會讓實際數字跟公式算的不一樣，而重要的是實際的那個。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import pandas as pd

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "portfolio", "closed_trades.csv")

COLUMNS = ["entry_date", "exit_date", "code", "name", "sleeve", "shares",
           "entry_price", "exit_price", "net_pnl", "ret_pct", "hold_days",
           "followed_rules", "note"]


def load() -> pd.DataFrame:
    if not os.path.exists(PATH):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(PATH, dtype={"code": str})


def save(df: pd.DataFrame):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    df.to_csv(PATH, index=False)


def add(a) -> int:
    df = load()
    cost = a.entry * a.shares
    net = a.net if a.net is not None else None
    ret = (net / cost) if net is not None else (a.exit / a.entry - 1)
    hold = (datetime.strptime(a.out, "%Y-%m-%d") - datetime.strptime(a.inn, "%Y-%m-%d")).days
    row = {
        "entry_date": a.inn, "exit_date": a.out, "code": a.code, "name": a.name,
        "sleeve": a.sleeve, "shares": a.shares,
        "entry_price": a.entry, "exit_price": a.exit,
        "net_pnl": round(net, 0) if net is not None else "",
        "ret_pct": round(ret * 100, 2), "hold_days": hold,
        "followed_rules": a.followed, "note": a.note or "",
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    save(df)
    print(f"已記錄：{a.code} {a.name}　{a.inn} {a.entry} → {a.out} {a.exit}"
          f"　{row['ret_pct']:+.2f}%　淨損益 {row['net_pnl']:+,.0f}")
    return 0


def show_list() -> int:
    df = load()
    if df.empty:
        print("還沒有任何紀錄。")
        return 0
    print(f"{'進場':<12}{'出場':<12}{'代號':<7}{'名稱':<9}{'池':<8}"
          f"{'張數':>6}{'進':>8}{'出':>8}{'淨損益':>11}{'報酬':>8}{'照規則':>7}")
    print("-" * 100)
    for _, r in df.iterrows():
        lots = r["shares"] / 1000
        pnl = f"{r['net_pnl']:+,.0f}" if pd.notna(r["net_pnl"]) and r["net_pnl"] != "" else "—"
        print(f"{r['entry_date']:<12}{r['exit_date']:<12}{r['code']:<7}{str(r['name']):<9}"
              f"{str(r['sleeve']):<8}{lots:>6.1f}{r['entry_price']:>8.1f}{r['exit_price']:>8.1f}"
              f"{pnl:>11}{r['ret_pct']:>7.2f}%{str(r['followed_rules']):>7}")
    return 0


def stats() -> int:
    df = load()
    if df.empty:
        print("還沒有任何紀錄。")
        return 0
    n = len(df)
    wins = df[df["ret_pct"] > 0]
    losses = df[df["ret_pct"] <= 0]
    pnl = pd.to_numeric(df["net_pnl"], errors="coerce")

    print(f"\n累積 {n} 筆交易")
    print("─" * 46)
    print(f"  勝率            {len(wins)/n*100:>8.1f}%   （{len(wins)} 勝 {len(losses)} 敗）")
    if len(wins):
        print(f"  平均獲利        {wins['ret_pct'].mean():>+8.2f}%")
    if len(losses):
        print(f"  平均虧損        {losses['ret_pct'].mean():>+8.2f}%")
    if len(wins) and len(losses) and losses["ret_pct"].mean() != 0:
        rr = abs(wins["ret_pct"].mean() / losses["ret_pct"].mean())
        print(f"  賺賠比          {rr:>8.2f}")
        # 期望值：勝率 × 平均獲利 − 敗率 × 平均虧損
        ev = len(wins)/n * wins["ret_pct"].mean() + len(losses)/n * losses["ret_pct"].mean()
        print(f"  每筆期望值      {ev:>+8.2f}%   ← 這個數字為負就代表方法有問題，不是運氣不好")
    if pnl.notna().any():
        print(f"  累積淨損益      {pnl.sum():>+8,.0f}")
    print(f"  平均持有        {df['hold_days'].mean():>8.1f} 天")

    if "followed_rules" in df and df["followed_rules"].notna().any():
        for flag, label in ((True, "照規則走"), (False, "沒照規則")):
            sub = df[df["followed_rules"].astype(str).str.lower().isin(
                ["true", "yes", "y", "1"] if flag else ["false", "no", "n", "0"])]
            if len(sub):
                print(f"\n  {label} {len(sub)} 筆：平均 {sub['ret_pct'].mean():+.2f}%")
        print("  （這個對照最有價值——如果照規則走的績效明顯比較好，那問題不在策略）")

    if n < 20:
        print(f"\n  註：只有 {n} 筆，樣本太小，這些數字現在還不能當結論。"
              f"累積到 20~30 筆之後才開始有參考價值。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="新增一筆已平倉交易")
    a.add_argument("code"); a.add_argument("name")
    a.add_argument("--in", dest="inn", required=True, help="進場日 YYYY-MM-DD")
    a.add_argument("--out", required=True, help="出場日 YYYY-MM-DD")
    a.add_argument("--entry", type=float, required=True)
    a.add_argument("--exit", type=float, required=True)
    a.add_argument("--shares", type=int, required=True)
    a.add_argument("--net", type=float, default=None, help="實際淨損益（對帳單上的數字）")
    a.add_argument("--sleeve", default="", help="swing / position / daytrade / 其他")
    a.add_argument("--followed", default="", help="有沒有照系統規則走：true / false")
    a.add_argument("--note", default="")
    sub.add_parser("list", help="列出所有交易")
    sub.add_parser("stats", help="統計勝率、賺賠比、期望值")
    args = ap.parse_args()
    return {"add": lambda: add(args), "list": show_list, "stats": stats}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
