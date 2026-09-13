"""每日訊號產生 + 持股檢查。

輸出一份 JSON 給儀表板用，內容包含：
  * 今日候選（波段池 / 長線池），含分數、目標價、建議停損
  * 現有持股的續抱 / 減碼 / 出清判斷與理由
  * 大盤與國際環境燈號
目標價說明：用「近 N 日波動度 + 前高」推估的合理區間，不是預言。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import sys as _sys
# Windows 終端機預設 cp950，輸出重導到檔案時會因為 ✓ 這類字元整支掛掉。
# 這幾行必須在任何 print 之前執行。
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


sys.path.insert(0, os.path.dirname(__file__))
import config as C
import indicators as I
import strategy as S
import chips as CH
import daily_ingest as DI
import fundamentals as FU
import events as EV
import news as NW

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
PORTFOLIO = os.path.join(ROOT, "portfolio", "holdings.json")


def next_trading_day(last: pd.Timestamp) -> tuple[str, bool]:
    """從最後一個交易日推下一個交易日。

    只跳過週末——國定假日沒有免費的行事曆可查，所以回傳 is_certain=False，
    介面上要標明「若逢假日順延」，不要假裝我們知道。
    """
    d = last + pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return str(d.date()), False


# ---------------------------------------------------------------- 榜單變化
def recent_tops(panel: pd.DataFrame, sleeve: str, cfg: dict, picker,
                lookback: int = 60) -> list[tuple[pd.Timestamp, list[str]]]:
    """回推最近幾個交易日的前 N 名，用來算「連續在榜天數」與「今天掉出去的」。"""
    dates = sorted(panel["date"].unique())[-lookback:]
    out = []
    for d in dates:
        day = panel[panel["date"] == d]
        try:
            c = picker(day, cfg)
        except Exception:
            c = None
        out.append((pd.Timestamp(d),
                    list(c.head(cfg["max_positions"])["code"]) if c is not None and len(c) else []))
    return out


def listing_history(tops: list, code: str) -> dict:
    """這檔在回看區間裡的上榜情況。

    只算「連續天數」是不夠的：一檔股票第 1 天上榜、第 2 天被擠掉、
    第 3 天又上榜，連續天數會重新歸 1，介面就會寫成「今天第一次上榜」——
    但那其實是第二次。所以同時回傳總次數與上一次出現的日期。

    「第一次」一律限定在回看區間內（LOOKBACK 個交易日），不宣稱
    「有史以來第一次」——那需要重跑整段歷史，而且沒有那麼有用。
    """
    streak = 0
    for _, codes in reversed(tops):
        if code in codes:
            streak += 1
        else:
            break
    hits = [d for d, codes in tops if code in codes]
    prev = None
    if streak and len(hits) > streak:
        # 這一段連續之前，上一次出現是哪天
        prev = hits[-(streak + 1)]
    return {
        "連續天數": streak,
        "區間內次數": len(hits),
        "上次上榜": str(prev.date()) if prev is not None else None,
        "回看天數": len(tops),
    }


def dropped_out(panel: pd.DataFrame, tops: list, sleeve: str, cfg: dict,
                news: dict | None = None, ret1: dict | None = None) -> list[dict]:
    """昨天在前 N 名、今天不在了的股票，以及卡在哪個條件。

    這個區塊存在的唯一理由是防止一個很貴的誤解：**掉出榜單不是賣出訊號**。
    榜單回答「明天可以買什麼」，持股由出場規則管，兩者互不相干。
    實測過「掉出榜就賣」：波段池平均報酬 +4.95% → −0.05%，
    賺超過 20% 的比例 14.1% → 0.5%，因為大贏家幾乎都在第 1~2 天就掉出榜了。
    """
    if len(tops) < 2:
        return []
    (_, prev), (last_d, now) = tops[-2], tops[-1]
    gone = [c for c in prev if c not in now]
    if not gone:
        return []
    day = panel[panel["date"] == last_d].set_index("code")
    out = []
    for code in gone:
        if code not in day.index:
            continue
        row = day.loc[code]
        try:
            fails = S.failed_conditions(row, sleeve, cfg)
        except Exception:
            fails = []
        item = {"code": code, "name": C.UNIVERSE.get(code, code),
                "現價": round(float(row["close"]), 2), "原因": fails}
        if ret1 and code in ret1 and ret1[code] == ret1[code]:
            item["當日漲跌"] = round(float(ret1[code]), 2)
        # 掉出榜單通常只是價格越過某條線，但偶爾背後有消息。
        # 把利空接上來，讓「為什麼掉出去」有機會補上條件式檢查看不到的原因。
        if news:
            bad = [n for n in NW.for_code(code, news) if n.get("direction") == "利空"]
            if bad:
                item["利空"] = [n["text"][:80] for n in bad[:2]]
        if not fails:
            # 條件還過得了，只是被別檔擠掉——這種要講清楚，不然更容易誤會
            item["原因"] = ["條件仍然通過，只是分數被其他股票擠下前 "
                            f"{cfg['max_positions']} 名"]
            item["仍在候選"] = True
        r1 = row["adjclose"] / day.loc[code, "adjclose"] - 1 if False else None
        out.append(item)
    return out


# ---------------------------------------------------------------- 基準率
def load_base_rates() -> dict:
    """讀 data/base_rates.json（由 src/base_rates.py 產生）。

    這是儀表板上唯一一個「告訴你會發生什麼」的數字，而且它刻意不做預測——
    它描述的是「照這套規則進出場，歷史上這類交易的結果分布」。
    每一池只有一個數字，不隨個股變動：實測顯示現有總分在候選池裡的排序能力
    接近零（秩相關 −0.025 / −0.003），依個股給不同機率是在假裝有鑑別力。
    """
    path = os.path.join(os.path.dirname(__file__), "..", "data", "base_rates.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def base_rate_line(base: dict, sleeve: str) -> dict | None:
    r = base.get(sleeve)
    if not r:
        return None
    return {
        "一句話": (f"歷史上這類進場 {r['win_rate']:.0f}% 會賺，"
                   f"中位數 {r['median']:+.1f}%，平均持有 {r['hold_days']:.0f} 天"),
        "勝率": r["win_rate"],
        "誤差": r["win_rate_ci"],
        "中位數": r["median"],
        "平均": r["mean"],
        "持有天數": r["hold_days"],
        "大賺比例": r["big_win_rate"],
        "最差一成": r["p10"],
        "最好一成": r["p90"],
        "樣本數": r["n"],
        "樣本內勝率": r["win_rate_is"],
        "樣本外勝率": r["win_rate_oos"],
        "期間": f"{base.get('generated_from', {}).get('start')} ~ "
                f"{base.get('generated_from', {}).get('end')}",
    }


# ---------------------------------------------------------------- 目標價
def price_targets(row: pd.Series, sleeve: str) -> dict:
    """目標價與停損價。

    上檔：以前高與 ATR 推估的兩段目標（保守 / 樂觀）。
    下檔：波段用 ATR 停損，長線用長期均線。
    """
    px, atr = row["close"], row["atr14"] * (row["close"] / row["adjclose"])
    prior_high = row["high_60"] * (row["close"] / row["adjclose"])
    if sleeve == "swing":
        t1 = max(prior_high, px + 2 * atr)
        t2 = px + 4 * atr
        stop = min(px - C.SWING["atr_trail"] * atr, px * (1 + C.SWING["hard_stop"]))
    else:
        # 長線不設短期目標：以「前波高點」為第一參考，第二目標用長期波動推估
        prior_high_120 = row["high_120"] * (row["close"] / row["adjclose"])
        t1 = max(prior_high_120, px * 1.12)
        t2 = px + 10 * atr
        stop = row[f"ma{C.POSITION['ma_slow']}"] * (row["close"] / row["adjclose"])
    return {
        "現價": round(px, 2),
        "目標價_保守": round(t1, 2),
        "目標價_樂觀": round(t2, 2),
        "建議停損": round(stop, 2),
        "風險報酬比": round((t1 - px) / max(px - stop, 1e-9), 2),
    }


def _chip_reasons(row: pd.Series) -> list[str]:
    """籌碼面的白話說明。缺資料就跳過那一行。"""
    r = []
    v = row.get("foreign_5d_ratio")
    if pd.notna(v):
        r.append(f"外資近 5 日{'買超' if v >= 0 else '賣超'} {abs(v):.2f} 個 20 日均量")
    st = row.get("foreign_streak")
    if pd.notna(st) and abs(st) >= 3:
        r.append(f"外資連續{'買超' if st > 0 else '賣超'} {int(abs(st))} 天")
    ts = row.get("trust_streak")
    if pd.notna(ts) and abs(ts) >= 3:
        r.append(f"投信連續{'買超' if ts > 0 else '賣超'} {int(abs(ts))} 天")
    vz = row.get("vol_zscore_20d")
    if pd.notna(vz) and abs(vz) >= 1.5:
        r.append(f"成交量{'異常放大' if vz > 0 else '明顯萎縮'}（z={vz:+.1f}）")
    sm = row.get("short_margin_ratio")
    if pd.notna(sm) and sm > 0.05:
        r.append(f"券資比 {sm*100:.1f}%（僅供參考，此因子樣本內外不穩定）")
    return r


def _fund_reasons(row: pd.Series) -> list[str]:
    r = []
    y = row.get("rev_yoy")
    if pd.notna(y):
        r.append(f"最新月營收年增 {y*100:+.1f}%")
    y3 = row.get("rev_yoy_3m")
    if pd.notna(y3):
        r.append(f"近三月營收年增 {y3*100:+.1f}%")
    om = row.get("op_margin")
    if pd.notna(om):
        r.append(f"營益率 {om*100:.1f}%")
    gc = row.get("gross_margin_chg")
    if pd.notna(gc) and abs(gc) > 0.005:
        r.append(f"毛利率較上季{'提升' if gc > 0 else '下滑'} {abs(gc)*100:.1f} 個百分點")
    e = row.get("eps_ttm_yoy")
    if pd.notna(e):
        r.append(f"近四季淨利年增 {e*100:+.1f}%")
    pp = row.get("per_pctile")
    if pd.notna(pp):
        r.append(f"本益比位於自身兩年區間的 {pp*100:.0f}% 分位（此因子樣本內外不穩定，僅供參考）")
    return r


def valuation_target(row: pd.Series, per_hist: dict) -> dict | None:
    """本益比河流目標價：EPS(近四季) × 該股自身的本益比區間。"""
    per, px = row.get("PER"), row.get("close")
    hist = per_hist.get(row["code"])
    if not per or not np.isfinite(per) or per <= 0 or hist is None or len(hist) < 60:
        return None
    eps = px / per
    lo, hi = float(np.quantile(hist, 0.5)), float(np.quantile(hist, 0.8))
    return {
        "近四季EPS": round(float(eps), 2),
        "目前本益比": round(float(per), 1),
        "本益比區間": [round(lo, 1), round(hi, 1)],
        "估值目標_中位": round(float(eps * lo), 2),
        "估值目標_樂觀": round(float(eps * hi), 2),
    }


def _reasons(row: pd.Series, sleeve: str) -> list[str]:
    r = []
    if sleeve == "swing":
        r.append(f"站上 {C.SWING['ma_fast']}MA 且 {C.SWING['ma_fast']}MA>{C.SWING['ma_slow']}MA")
        r.append(f"距 60 日高點 {(1-row['near_high'])*100:.1f}%")
        r.append(f"20 日漲幅 {row['mom_short']*100:+.1f}%")
        r.append(f"5 日均量 / 60 日均量 = {row['vol_ratio']:.2f}")
        r.append(f"RSI14 {row['rsi14']:.0f}")
    else:
        r.append(f"站上 {C.POSITION['ma_slow']}MA，中期均線在長期均線之上")
        r.append(f"120 日漲幅 {row['mom_long']*100:+.1f}%")
        r.append(f"距 120 日高點 {row['dd_from_high120']*100:.1f}%")
    return r


# ---------------------------------------------------------------- 持股
def long_term_review() -> dict:
    """長期持有部位（ETF）。**只算損益，不給進出建議。**

    為什麼不套用選股規則：那套規則是為 1~4 週的波段設計的
    （−8% 硬停損、2.5×ATR 移動停損、連兩日跌破 20MA、60 日上限）。
    套在「放 3~5 年」的部位上，會叫你每個月出場好幾次，跟持有計畫直接衝突。
    混在一起顯示，遲早有一天會照錯的規則做錯的決定。

    槓桿型 ETF 還有一件事值得知道：它追蹤的是**單日**兩倍報酬，
    每天重新平衡。在來回震盪的行情裡，即使指數回到原點，這種商品也會
    因為路徑相依而耗損。所以這裡把「同期大盤漲跌」一起列出來對照——
    不是要你據此買賣，是讓你看得到兩者的差距。
    """
    path = os.path.join(ROOT, "portfolio", "long_term.json")
    hist = os.path.join(DATA, "etf", "etf_history.csv")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            hs = json.load(f).get("holdings", [])
    except Exception:
        return {}
    if not hs:
        return {}

    px = pd.DataFrame()
    if os.path.exists(hist):
        px = pd.read_csv(hist, dtype={"code": str})
        px["date"] = pd.to_datetime(px["date"])

    rows, total_cost, total_value = [], 0.0, 0.0
    as_of = None
    for h in hs:
        code, sh, cost = str(h["code"]), float(h["shares"]), float(h["avg_cost"])
        item = {"code": code, "name": h.get("name", code),
                "股數": int(sh), "均價": cost, "成本": round(cost * sh, 0)}
        g = px[px["code"] == code].sort_values("date") if len(px) else pd.DataFrame()
        if len(g):
            last = g.iloc[-1]
            now = float(last["close"])
            as_of = max(as_of, last["date"]) if as_of is not None else last["date"]
            item.update({"現價": round(now, 2), "市值": round(now * sh, 0),
                         "損益": round((now - cost) * sh, 0),
                         "報酬率": round((now / cost - 1) * 100, 2)})
            total_value += now * sh
        else:
            item["現價"] = None
            item["備註"] = "還沒有報價——跑一次 daily_update.py 就會抓進來"
            total_value += cost * sh
        total_cost += cost * sh
        rows.append(item)

    return {
        "部位": rows,
        "總成本": round(total_cost, 0),
        "總市值": round(total_value, 0),
        "總損益": round(total_value - total_cost, 0),
        "總報酬率": round((total_value / total_cost - 1) * 100, 2) if total_cost else None,
        "報價日": str(as_of.date()) if as_of is not None else None,
    }


def universe_status() -> dict:
    """觀察池現況與本月異動。

    池子從 2026-09 起改成每月依當時成交值重排（見 src/universe_live.py）。
    這一區存在的理由：名單會自己動，不講清楚的話「昨天有的股票今天不見了」
    會被誤會成程式壞掉——那跟「條件不符被踢出榜單」是完全不同的兩件事。
    """
    import json
    p = os.path.join(ROOT, "data", "universe_current.json")
    if not os.path.exists(p):
        return {"檔數": len(C.UNIVERSE), "fallback": True,
                "說明": "還沒有重排紀錄，用的是 config 裡寫死的名單"}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"檔數": len(C.UNIVERSE), "fallback": True, "說明": "重排紀錄讀不到"}
    ch = d.get("changes", {})
    return {
        "檔數": len(d.get("universe", {})),
        "基準日": d.get("as_of"),
        "重排日": d.get("rebalanced_on"),
        "規則": d.get("rule", ""),
        "新進": ch.get("added", []),
        "剔除": ch.get("removed", []),
        "持股保留": ch.get("kept_for_holding", []),
        "fallback": False,
    }


def load_holdings() -> list[dict]:
    if not os.path.exists(PORTFOLIO):
        return []
    with open(PORTFOLIO, encoding="utf-8") as f:
        return json.load(f).get("holdings", [])


def review_holdings(holdings: list[dict], panel: pd.DataFrame) -> list[dict]:
    """對每一檔持股給出建議。買進日之後的最高價用來算移動停損。"""
    out = []
    last_date = panel["date"].max()
    for h in holdings:
        code = str(h["code"]).zfill(4)
        g = panel[panel["code"] == code].sort_values("date")
        if g.empty:
            out.append({"code": code, "name": h.get("name", code), "建議": "無法判斷",
                        "理由": [f"資料庫裡沒有 {code} 的股價，算不出任何訊號",
                               "要追蹤它：把代號加進 src/config.py 的 UNIVERSE，"
                               "再跑 python fetch_data.py --resume 補資料"]})
            continue
        row = g.iloc[-1]
        entry_date = pd.to_datetime(h["entry_date"])
        since = g[g["date"] >= entry_date]
        peak_adj = since["adjclose"].max() if len(since) else row["adjclose"]
        ratio = row["close"] / row["adjclose"]
        sleeve = h.get("sleeve", "swing")
        cfg = C.SWING if sleeve == "swing" else C.POSITION

        pnl = row["close"] / h["entry_price"] - 1
        pos_state = {"entry_price": h["entry_price"] * (row["adjclose"] / row["close"]),
                     "peak": peak_adj, "held_days": len(since),
                     "below_ma": 1 if row["adjclose"] < row[f"ma{C.SWING['ma_fast']}"] else 0}
        exit_reason = (S.swing_exit if sleeve == "swing" else S.position_exit)(pos_state, row, cfg)

        trail = (peak_adj - cfg.get("atr_trail", 2.5) * row["atr14"]) * ratio if sleeve == "swing" \
            else peak_adj * (1 - cfg["trail_pct"]) * ratio
        ma_key = cfg.get("ma_fast", cfg.get("ma_slow"))
        stance = "站上" if row["adjclose"] > row[f"ma{ma_key}"] else "跌破"
        reasons = [
            f"報酬 {pnl*100:+.1f}%，持有 {len(since)} 個交易日",
            f"目前價 {row['close']:.2f}，移動停損位 {trail:.2f}",
            f"{stance} {ma_key}MA",
        ]
        if exit_reason:
            advice = "出清"
            reasons.insert(0, f"觸發出場規則：{exit_reason}")
        elif pnl > 0.25 and row["rsi14"] > 80:
            advice = "減碼一半"
            reasons.insert(0, f"獲利 {pnl*100:.0f}% 且 RSI {row['rsi14']:.0f} 過熱，落袋一部分")
        else:
            advice = "續抱"
            reasons.insert(0, "未觸發任何出場條件")
        # 距停損還有多少空間、離目標價走了幾成——這兩個數字比「現在賺多少」更能決定要不要動
        px_now = float(row["close"])
        stop_gap = (px_now - trail) / px_now if px_now else float("nan")
        tgt = price_targets(row, sleeve)
        span = tgt["目標價_保守"] - h["entry_price"]
        progress = (px_now - h["entry_price"]) / span if span > 0 else float("nan")
        reasons.append(f"距移動停損還有 {stop_gap*100:.1f}% 的空間")
        if pd.notna(progress):
            reasons.append(f"往保守目標 {tgt['目標價_保守']:.1f} 走了 {progress*100:.0f}%")

        out.append({
            "新聞": NW.for_code(code, NW.load_flags()),
            "事件": EV.upcoming(code, datetime.now().date()),
            "code": code, "name": h.get("name", C.UNIVERSE.get(code, code)),
            "sleeve": sleeve, "entry_date": str(entry_date.date()),
            "entry_price": h["entry_price"], "shares": h.get("shares"),
            "現價": round(px_now, 2), "報酬率": round(float(pnl), 4),
            "移動停損": round(float(trail), 2),
            "距停損": round(float(stop_gap), 4),
            "目標價": tgt["目標價_保守"],
            "目標進度": round(float(progress), 3) if pd.notna(progress) else None,
            "持有天數": len(since),
            "建議": advice, "理由": reasons,
        })
    return out


# ---------------------------------------------------------------- 主流程
def generate(panel: pd.DataFrame, regime_df: pd.DataFrame | None, macro: dict | None = None,
             per_hist: dict | None = None) -> dict:
    per_hist = per_hist or {}
    news = NW.load_flags()
    exdiv = EV.load_exdividends()
    today = datetime.now().date()
    last_date = panel["date"].max()
    day = panel[panel["date"] == last_date].copy()
    # 當日漲跌：用還原股價算，除權息日才不會出現假跌幅
    _p = panel.sort_values(["code", "date"])
    _p["_r1"] = _p.groupby("code")["adjclose"].pct_change()
    ret1 = (_p[_p["date"] == last_date].set_index("code")["_r1"] * 100).to_dict()

    breadth = float(day["above_ma60"].mean())
    regime_note, swing_ok, position_ok = "觀察池廣度替代", breadth > 0.5, breadth > 0.5
    if regime_df is not None and last_date in regime_df.index:
        swing_ok = bool(regime_df.loc[last_date, "swing_ok"])
        position_ok = bool(regime_df.loc[last_date, "position_ok"])
        regime_note = "加權指數"

    base = load_base_rates()

    picks = {}
    churn = {}
    for sleeve, cfg, picker in (("swing", C.SWING, S.swing_candidates),
                                ("position", C.POSITION, S.position_candidates)):
        tops = recent_tops(panel, sleeve, cfg, picker)
        churn[sleeve] = dropped_out(panel, tops, sleeve, cfg, news, ret1)
        c = picker(day, cfg)
        rows = []
        for _, r in c.head(cfg["max_positions"]).iterrows():
            item = {"code": r["code"], "name": C.UNIVERSE.get(r["code"], r["code"]),
                    "score": round(float(r["score"]), 2),
                    "技術分": round(float(r.get("tech_score", float("nan"))), 2),
                    "籌碼分": round(float(r.get("chip_score", 0.0)), 2),
                    "基本分": round(float(r.get("fund_score", 0.0)), 2),
                    "理由": _reasons(r, sleeve), "籌碼": _chip_reasons(r),
                    "基本面": _fund_reasons(r)}
            item.update(price_targets(r, sleeve))
            _v = ret1.get(r["code"])
            if _v is not None and _v == _v:
                item["當日漲跌"] = round(float(_v), 2)
            val = valuation_target(r, per_hist)
            if val:
                item["估值"] = val
            item["上榜紀錄"] = listing_history(tops, r["code"])
            rec = (base.get(sleeve, {}).get("per_code") or {}).get(r["code"])
            if rec:
                item["歷史紀錄"] = rec
            item["新聞"] = NW.for_code(r["code"], news)
            item["事件"] = EV.upcoming(r["code"], today, ex=exdiv)
            rows.append(item)
        picks[sleeve] = rows

    return {
        # 雲端容器跑在 UTC，但使用者在台灣，一律換算成台北時間再輸出
        "generated_at": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M") + " (台北時間)",
        "data_date": str(last_date.date()),
        "訊號適用日": next_trading_day(last_date)[0],
        "觀察池檔數": len(C.UNIVERSE),
        "觀察池": universe_status(),
        "regime": {
            "來源": regime_note,
            "波段可進場": swing_ok,
            "長線可進場": position_ok,
            "觀察池站上60MA比例": round(breadth, 3),
        },
        "macro": macro or {},
        "資料狀態": DI.freshness(),
        "資料警告": DI.assert_fresh(),
        "news_meta": {"scanned_at": news.get("scanned_at"), "stale": news.get("stale", True),
                      "bias": NW.bias_summary(news)},
        "picks": picks,
        "掉出榜單": churn,
        "基準率": {s: base_rate_line(base, s) for s in ("swing", "position")},
        "holdings": review_holdings(load_holdings(), panel),
        "長期持有": long_term_review(),
        "disclaimer": "本報告為程式化篩選結果，非投資建議。回測顯示此規則在 2024-2026 大多頭期間並未勝過買進持有。",
    }


def main():
    # 跟 run_backtest 用同一個解析規則：優先 data/prices/，找不到才退回根目錄。
    # 兩邊必須一致，否則回測與每日訊號會讀到不同的資料。
    from run_backtest import price_csv
    raw = pd.read_csv(price_csv(), dtype={"code": str})
    panel = FU.add_fundamental_factors(CH.add_chip_factors(I.build_panel(raw, C.SWING, C.POSITION)))
    regime_df = None
    mp = os.path.join(DATA, "twstock_macro.csv")
    macro = {}
    if os.path.exists(mp):
        m = pd.read_csv(mp)
        m["date"] = pd.to_datetime(m["date"])
        twii = m[m["symbol"] == "TWII"].sort_values("date").set_index("date")["close"]
        regime_df = pd.DataFrame({
            "swing_ok": twii > twii.rolling(C.REGIME["swing_ma"]).mean(),
            "position_ok": twii > twii.rolling(C.REGIME["position_ma"]).mean(),
        }).fillna(True)
        for sym in ["TWII", "SOX", "IXIC", "GSPC", "VIX", "DXY", "USDTWD", "US10Y"]:
            s = m[m["symbol"] == sym].sort_values("date")
            if len(s) < 6:
                continue
            macro[sym] = {
                "close": round(float(s["close"].iloc[-1]), 2),
                "日變動": round(float(s["close"].iloc[-1] / s["close"].iloc[-2] - 1), 4),
                "五日變動": round(float(s["close"].iloc[-1] / s["close"].iloc[-6] - 1), 4),
                "date": str(s["date"].iloc[-1].date()),
            }
    # 只有快照（沒有完整歷史）的國際指標
    sp = os.path.join(DATA, "macro_snapshot.json")
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                macro.setdefault(k, v)
    raw_fund = FU.load_raw()
    per_hist = {}
    if not raw_fund.empty:
        pf = raw_fund[(raw_fund["dataset"] == "per") & (raw_fund["key"] == "PER")]
        for code, g in pf.groupby("code"):
            v = g.sort_values("date")["value"].dropna()
            v = v[v > 0]
            if len(v) >= 60:
                per_hist[code] = v.tail(500).to_numpy()
    report = generate(panel, regime_df, macro, per_hist)
    out = os.path.join(ROOT, "output", "daily_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:2500])
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
