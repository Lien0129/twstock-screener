"""資料完整性檢查 — 隨時可以跑，用來確認抓取有沒有真的完成。

`python3 src/verify_data.py`

每一項都會印出 OK 或問題描述，最後給總結。設計成「看不懂程式也能判斷資料對不對」。
"""
from __future__ import annotations

import os
import sys

import pandas as pd

import sys as _sys
# Windows 終端機預設 cp950，輸出重導到檔案時會因為 ✓ 這類字元整支掛掉。
# 這幾行必須在任何 print 之前執行。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

OK, BAD, WARN = "  ✓", "  ✗", "  ⚠"

# 觀察池改成每月重排之後，新進來的股票必然有一段時間沒有籌碼／基本面歷史
# （那些資料是逐日累積的，沒辦法回頭補）。如果照舊當成錯誤，每次重排完
# 儀表板都會拒跑好幾天。所以給一個寬限期：重排後 GRACE_DAYS 天內，新進
# 股票缺資料只是提醒；超過還缺，就是真的有問題，照樣擋。
GRACE_DAYS = 45


def _p(*parts) -> str:
    return os.path.join(DATA, *parts)


def _grace_codes() -> set[str]:
    """最近一次重排新進、還在寬限期內的股票。"""
    import json
    from datetime import date, datetime
    p = os.path.join(DATA, "universe_current.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        on = datetime.strptime(d["rebalanced_on"], "%Y-%m-%d").date()
        if (date.today() - on).days > GRACE_DAYS:
            return set()
        return {c["code"] for c in d.get("changes", {}).get("added", [])}
    except Exception:
        return set()


def _split(miss: list[str], grace: set[str]) -> tuple[list[str], list[str]]:
    """(真的有問題的, 只是剛進池子的)"""
    return [c for c in miss if c not in grace], [c for c in miss if c in grace]


def _coverage(label: str, uni: set, miss: list[str], grace: set[str],
              problems: list[str], kind: str) -> None:
    hard, soft = _split(miss, grace)
    flag = OK if not miss else (WARN if not hard else BAD)
    print(f"{flag} {label} {len(uni)-len(miss)}/{len(uni)}"
          + (f"，缺 {','.join(miss)}" if miss else ""))
    if soft:
        print(f"      其中 {len(soft)} 檔是本月剛進觀察池的，等每日抓取累積即可")
    if hard:
        problems.append(f"{kind}缺 {len(hard)} 檔")


def _load(path: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, dtype={"code": str})


def check() -> list[str]:
    uni = set(C.UNIVERSE)
    grace = _grace_codes() & uni
    problems: list[str] = []
    print(f"觀察池 {len(uni)} 檔"
          + (f"（基準日 {C.UNIVERSE_AS_OF}）" if C.UNIVERSE_AS_OF else "（fallback 名單）"))
    if grace:
        print(f"本月新進 {len(grace)} 檔，資料寬限期內：{','.join(sorted(grace))}")
    print("─" * 58)

    # 1. 股價
    px = _load(_p("prices", "twstock_history.csv"))
    if px is None:
        px = _load(_p("twstock_history.csv"))
    print("【股價】")
    if px is None:
        problems.append("找不到股價檔")
        print(BAD, "找不到檔案")
    else:
        n = px.groupby("code").size()
        miss = sorted(uni - set(px["code"]))
        short = [c for c in sorted(n[n < n.median() * 0.8].index.intersection(uni))
                 if c not in grace]
        print(f"{OK} {len(px):,} 筆、{px['code'].nunique()} 檔、"
              f"{px['date'].min()} ~ {px['date'].max()}")
        _coverage("觀察池覆蓋", uni, miss, grace, problems, "股價")
        if short:
            print(f"{BAD} 歷史明顯偏短的檔：{','.join(short[:8])}（可能是新上市）")

        # 個股資料落後：整體最後一天是 9/10，但某幾檔只到 8/31 的話，
        # 它們不會出現在當日訊號裡（signals 只取最後一天的橫斷面）。
        # 不擋，但要講出來，否則「今天怎麼只有 94 檔在跑」會很難查。
        last_all = px["date"].max()
        lag = sorted(px.groupby("code")["date"].max()
                     [lambda s: s < last_all].index.intersection(uni))
        if lag:
            print(f"{WARN} {len(lag)} 檔股價停在 {last_all} 之前，今天不會被選："
                  f"{','.join(lag[:8])}{' …' if len(lag) > 8 else ''}")
        dup = px.duplicated(subset=["code", "date"]).sum()
        print(f"{OK if dup == 0 else BAD} 重複列 {dup} 筆")
        if dup:
            problems.append(f"股價有 {dup} 筆重複")
        bad = px[(px["close"] <= 0) | px["close"].isna()]
        print(f"{OK if bad.empty else BAD} 收盤價異常 {len(bad)} 筆")

    # 2. 籌碼
    ch = _load(_p("chips", "twstock_chips.csv"))
    print("\n【籌碼】")
    if ch is None:
        problems.append("找不到籌碼檔")
        print(BAD, "找不到檔案")
    else:
        miss = sorted(uni - set(ch["code"]))
        print(f"{OK} {len(ch):,} 筆、{ch['code'].nunique()} 檔、{ch['date'].min()} ~ {ch['date'].max()}")
        _coverage("觀察池覆蓋", uni, miss, grace, problems, "籌碼")
        mb = ch["margin_bal"].notna().mean()
        print(f"{OK if mb > 0.9 else BAD} 融資餘額填充率 {mb:.0%}")

    # 3. 基本面（三個資料集要各自檢查）
    fu = _load(_p("fundamentals", "twstock_fundamentals.csv"))
    print("\n【基本面】")
    if fu is None:
        problems.append("找不到基本面檔")
        print(BAD, "找不到檔案")
    else:
        print(f"{OK} {len(fu):,} 筆")
        have = fu.groupby("code")["dataset"].apply(set)
        for ds, label in (("rev", "月營收"), ("fin", "財報"), ("per", "本益比")):
            got = {c for c, s in have.items() if ds in s} & uni
            _coverage(label, uni, sorted(uni - got), grace, problems, label)
        rev = fu[fu["dataset"] == "rev"]
        if not rev.empty:
            print(f"{OK} 月營收資料涵蓋 {rev['date'].min()} ~ {rev['date'].max()}")

    # 4. 當沖
    dt = _load(_p("daytrading", "twstock_daytrading.csv"))
    print("\n【當沖】")
    if dt is None:
        print("  － 沒有當沖檔（非必要）")
    else:
        miss = sorted(uni - set(dt["code"]))
        print(f"{OK} {len(dt):,} 筆、{dt['code'].nunique()} 檔")
        _coverage("觀察池覆蓋", uni, miss, grace, problems, "當沖")

    # 5. 大盤與國際
    mac = _load(_p("twstock_macro.csv"))
    print("\n【加權指數與國際指標】")
    if mac is None:
        problems.append("找不到指數檔")
        print(BAD, "找不到檔案")
    else:
        cnt = mac.groupby("symbol").size().to_dict()
        print(f"{OK} " + "、".join(f"{k} {v}" for k, v in sorted(cnt.items())))
        if px is not None:
            twii = mac[mac["symbol"] == "TWII"]
            cov = twii["date"].isin(px["date"].unique()).sum() / px["date"].nunique()
            flag = OK if cov > 0.95 else BAD
            print(f"{flag} 加權指數覆蓋交易日 {cov:.0%}"
                  + ("" if cov > 0.95 else " — 大盤濾網會在缺資料的日子失效！"))
            if cov <= 0.95:
                problems.append("加權指數覆蓋不足")

    print("\n" + "─" * 58)
    if problems:
        print(f"發現 {len(problems)} 個問題：")
        for x in problems:
            print("  ✗ " + x)
        print("\n補資料：python fetch_data.py --resume")
    else:
        print("全部通過，資料可以拿來跑回測與每日訊號。")
    return problems


if __name__ == "__main__":
    sys.exit(1 if check() else 0)
