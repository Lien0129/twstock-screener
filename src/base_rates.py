"""基準率：這類設定歷史上實際會發生什麼。

不預測、不推薦，只回答一個問題——
「照這套規則進場、照這套規則出場，過去這種條件的股票，最後長什麼樣？」

跟評分的差別很重要：
  評分  說的是「這檔比那檔好」——但實測顯示現有總分在候選池裡的
        秩相關是 −0.025 / −0.003，也就是幾乎沒有鑑別力。
  基準率 說的是「這類交易有多少比例會賺」——這個數字**不需要有預測力
        也成立**，因為它是描述，不是預測。

分桶用波動度（ATR%），理由是它跟結果的關係是機械性的、不是統計上的僥倖：
固定 −8% 硬停損之下，波動越大就越容易被掃到，勝率必然越低。正因為它接近
恆等式，才不會像那些死掉的因子一樣樣本外就消失。

但要同時看兩欄：**低波動的勝率高、平均報酬低；高波動的勝率低、平均報酬高。**
這是取捨，不是好壞。只看勝率會得到錯誤結論。

    python3 src/base_rates.py          # 重算並寫入 data/base_rates.json
"""
from __future__ import annotations

import json
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
from run_backtest import load_panel

OUT = os.path.join(SRC, "..", "data", "base_rates.json")
NBUCKET = 5


def _simulate(g: pd.DataFrame, entries: set, sleeve: str) -> list[dict]:
    """把一檔股票所有的候選日模擬過一遍，回傳每筆的實際結果。

    進場價 = 隔日開盤（跟策略假設一致）。出場條件直接照 strategy.py 的規則，
    不是「持有 N 天」——這正是重點：放著不動的 20 日勝率是 60%，
    裝上停損之後是 39%。
    """
    cfg = C.SWING if sleeve == "swing" else C.POSITION
    ratio = (g["adjclose"] / g["close"]).values
    ao = g["open"].values * ratio
    ac = g["adjclose"].values
    atr = g["atr14"].values
    dates = g["date"].values
    atrp = g["atr_pct"].values
    n = len(g)

    if sleeve == "swing":
        maf = g[f"ma{cfg['ma_fast']}"].values
        hs, tr, mh = cfg["hard_stop"], cfg["atr_trail"], cfg["max_hold_days"]
    else:
        mas = g[f"ma{cfg['ma_slow']}"].values
        tp, mh = cfg["trail_pct"], cfg["max_hold_days"]

    out = []
    for i in range(n - 1):
        if pd.Timestamp(dates[i]) not in entries:
            continue
        if not np.isfinite(ao[i + 1]) or ao[i + 1] <= 0 or not np.isfinite(atrp[i]):
            continue
        e = ao[i + 1]
        peak = e
        below = 0
        ret, why, k = None, None, i + 1
        for k in range(i + 1, min(i + 1 + mh, n)):
            c = ac[k]
            peak = max(peak, c)
            if sleeve == "swing":
                if c <= e * (1 + hs):
                    ret, why = hs, "硬停損"
                    break
                t = peak - tr * atr[k]
                if np.isfinite(t) and c < t:
                    ret, why = c / e - 1, "移動停損"
                    break
                if np.isfinite(maf[k]) and c < maf[k]:
                    below += 1
                    if below >= 2:
                        ret, why = c / e - 1, "跌破均線"
                        break
                else:
                    below = 0
            else:
                if c < peak * (1 - tp):
                    ret, why = c / e - 1, f"自高點回落{int(tp*100)}%"
                    break
                if np.isfinite(mas[k]) and c < mas[k]:
                    ret, why = c / e - 1, "跌破長期均線"
                    break
        truncated = False
        if ret is None:
            # 走到迴圈結束有兩種可能，必須分開：
            #   (a) 真的抱滿持有上限 —— 這是有效樣本
            #   (b) 資料到底了，還沒滿上限就被迫結算 —— 這是假的出場
            # 混在一起會嚴重灌水，長線池尤其明顯（上限 250 天，但資料只有兩年，
            # 最後一年進場的單子幾乎全部會被截斷，而且截斷的都是「還沒跌」的贏家）
            if k - i >= mh - 1:
                ret, why = ac[k] / e - 1, "抱滿上限"
            else:
                ret, why, truncated = ac[k] / e - 1, "資料截斷", True
        out.append({"date": pd.Timestamp(dates[i]), "ret": float(ret),
                    "days": int(k - i), "why": why, "atr_pct": float(atrp[i]),
                    "truncated": truncated})
    return out


def collect(panel: pd.DataFrame, sleeve: str) -> pd.DataFrame:
    picker = S.swing_candidates if sleeve == "swing" else S.position_candidates
    cfg = C.SWING if sleeve == "swing" else C.POSITION
    by_code: dict[str, set] = {}
    for dt, day in panel.groupby("date"):
        try:
            c = picker(day, cfg)
        except Exception:
            continue
        for code in c["code"]:
            by_code.setdefault(code, set()).add(pd.Timestamp(dt))
    rows = []
    for code, g in panel.groupby("code", sort=False):
        if code not in by_code:
            continue
        rows += [dict(r, code=code)
                 for r in _simulate(g.reset_index(drop=True), by_code[code], sleeve)]
    return pd.DataFrame(rows)


def first_of_episode(s: pd.DataFrame, gap_days: int = 5) -> pd.DataFrame:
    """把「同一檔連續好幾天都符合條件」壓成一筆。

    為什麼一定要做：一檔股票在一段趨勢裡可能連續 80 天都是候選，
    那 80 筆講的其實是**同一件事**。全部算進去會讓樣本數看起來有一萬多筆、
    信心區間窄得不合理，但真正獨立的事件只有幾百個。
    這裡只取每一段的第一天（相隔 5 個交易日以上才算新的一段）。
    """
    out = []
    for code, g in s.sort_values(["code", "date"]).groupby("code", sort=False):
        prev = None
        for _, r in g.iterrows():
            if prev is None or (r["date"] - prev).days > gap_days * 1.6:
                out.append(r)
            prev = r["date"]
    return pd.DataFrame(out)


def profile(s: pd.DataFrame) -> dict:
    if s.empty:
        return {}
    return {
        "n": int(len(s)),
        "win_rate": round(float((s["ret"] > 0).mean()) * 100, 1),
        "median": round(float(s["ret"].median()) * 100, 2),
        "mean": round(float(s["ret"].mean()) * 100, 2),
        "hold_days": round(float(s["days"].mean()), 1),
        "p10": round(float(s["ret"].quantile(0.10)) * 100, 1),
        "p90": round(float(s["ret"].quantile(0.90)) * 100, 1),
        "big_win_rate": round(float((s["ret"] > 0.20).mean()) * 100, 1),
    }


def build() -> dict:
    panel = load_panel()
    res = {"generated_from": {
        "start": str(panel["date"].min().date()),
        "end": str(panel["date"].max().date()),
        "codes": int(panel["code"].nunique())}}

    for sleeve in ("swing", "position"):
        raw = collect(panel, sleeve)
        if raw.empty:
            continue
        n_all = len(raw)
        raw = raw[~raw["truncated"]].copy()     # 被資料尾端截斷的單子不能算數
        s = first_of_episode(raw)               # 再把重複計算的同一段趨勢壓成一筆
        if s.empty:
            continue

        dts = np.sort(s["date"].unique())
        split = pd.Timestamp(dts[len(dts) // 2])
        p = profile(s)
        w = p["win_rate"] / 100
        ci = round(float(np.sqrt(w * (1 - w) / p["n"]) * 1.96 * 100), 1)

        res[sleeve] = {
            **p,
            "per_code": per_code(s),
            "win_rate_ci": ci,
            "win_rate_is": profile(s[s["date"] < split]).get("win_rate"),
            "win_rate_oos": profile(s[s["date"] >= split]).get("win_rate"),
            "split": str(split.date()),
            "exit_mix": {k: int(v) for k, v in s["why"].value_counts().items()},
            "n_overlapping": int(len(raw)),
            "n_truncated_dropped": int(n_all - len(raw)),
        }
    return res


def per_code(s: pd.DataFrame) -> dict:
    """每一檔自己的歷史成績單。

    刻意輸出**次數**而不是百分比。每檔的獨立事件中位數只有 5~8 次，
    在 n=8 上算勝率，95% 信賴區間大約是 ±35 個百分點——寫成「37.5%」
    會讓一個幾乎沒有資訊的數字看起來像機率估計。「8 次裡有 3 次賺」
    是同一件事，但讀者看得到分母。

    另外要注意：實測顯示一檔股票在這套系統裡的過去表現，
    對它的未來表現沒有預測力（波段池前後兩期勝率秩相關 −0.03，
    前段勝率高的那半後段是 38.5%，低的那半是 39.8%）。
    所以這是**紀錄**，不是機率。
    """
    out = {}
    for code, g in s.groupby("code"):
        g = g.sort_values("date")
        r = g["ret"]
        eps = [{"date": str(pd.Timestamp(x["date"]).date()),
                "ret": round(float(x["ret"]) * 100, 1),
                "days": int(x["days"]), "why": x["why"]}
               for _, x in g.tail(4).iloc[::-1].iterrows()]
        out[code] = {
            "n": int(len(g)),
            "wins": int((r > 0).sum()),
            "losses": int((r <= 0).sum()),
            "median": round(float(r.median()) * 100, 1),
            "best": round(float(r.max()) * 100, 1),
            "worst": round(float(r.min()) * 100, 1),
            "hold_days": round(float(g["days"].mean()), 1),
            "recent": eps,
        }
    return out


def sentence(r: dict, label: str) -> str:
    """卡片上要顯示的那一句話。"""
    return (f"{label}歷史上 {r['win_rate']:.0f}% 會賺"
            f"（±{r['win_rate_ci']:.0f}），中位數 {r['median']:+.1f}%，"
            f"平均持有 {r['hold_days']:.0f} 天")


def show(res: dict):
    g = res["generated_from"]
    print(f"\n樣本：{g['start']} ~ {g['end']}，{g['codes']} 檔")
    for sleeve, label in (("swing", "波段池"), ("position", "長線池")):
        if sleeve not in res:
            continue
        r = res[sleeve]
        print(f"\n{'='*80}\n  【{label}】獨立事件 {r['n']:,} 筆　照真實出場規則")
        print(f"{'='*80}")
        print(f"  {sentence(r, '這類設定')}")
        print(f"    勝率      {r['win_rate']:.1f}% ±{r['win_rate_ci']:.1f}"
              f"　（樣本內 {r['win_rate_is']:.1f}% / 樣本外 {r['win_rate_oos']:.1f}%，"
              f"切點 {r['split']}）")
        print(f"    中位數    {r['median']:+.2f}%　　平均 {r['mean']:+.2f}%"
              f"　　← 平均遠高於中位數，代表獲利集中在少數幾筆")
        print(f"    賺超過 20% 的比例 {r['big_win_rate']:.1f}%"
              f"　最差 10% {r['p10']:+.1f}%　最好 10% {r['p90']:+.1f}%")
        print(f"    平均持有  {r['hold_days']:.1f} 天")
        print(f"    出場原因：" + "　".join(f"{k} {v:,}" for k, v in r["exit_mix"].items()))
        print(f"    （原始候選-日 {r['n_overlapping']:,} 筆，壓成獨立事件後 {r['n']:,} 筆；"
              f"另有 {r['n_truncated_dropped']:,} 筆因資料尾端截斷而剔除）")


def main() -> int:
    res = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    show(res)
    print(f"\n已寫入 {os.path.relpath(OUT, os.path.dirname(SRC))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
