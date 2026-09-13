"""選股規則：波段池與長線池。

設計原則
  1. 只用當日收盤前可得的資訊，訊號一律隔日開盤才成交，避免未來函數。
  2. 先過硬性條件（趨勢、量能、風險），再用綜合分數排序取前幾名。
  3. 大盤環境不好時直接不開新倉——出場規則比選股規則重要。
"""
import numpy as np
import pandas as pd


def _zscore(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def chip_score(c: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """籌碼加分。因子缺漏時當作 0，不會因為某天沒資料就整檔被淘汰。"""
    import config as C
    cfg = cfg if cfg is not None else C.CHIPS
    score = pd.Series(0.0, index=c.index)
    if not cfg.get("enabled"):
        return score
    for f, w in cfg["weights"].items():
        if f in c.columns:
            z = _zscore(c[f].fillna(c[f].median())).fillna(0.0)
            score = score + w * z.clip(-2, 2)      # 單一因子不讓它一票定生死
    cap = cfg.get("score_cap")
    return score.clip(-cap, cap) if cap else score


def chip_veto(c: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """回傳 True 表示「可以留下」。外資大幅站賣方時直接排除。"""
    import config as C
    cfg = cfg if cfg is not None else C.CHIPS
    if not cfg.get("enabled") or "foreign_5d_ratio" not in c.columns:
        return pd.Series(True, index=c.index)
    thr = cfg.get("veto_foreign_below")
    if thr is None:
        return pd.Series(True, index=c.index)
    return ~(c["foreign_5d_ratio"] < thr)


def fund_score(c: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """基本面加分。"""
    import config as C
    cfg = cfg if cfg is not None else C.FUNDAMENTALS
    score = pd.Series(0.0, index=c.index)
    if not cfg.get("enabled"):
        return score
    for f, w in cfg["weights"].items():
        if f in c.columns:
            z = _zscore(c[f].fillna(c[f].median())).fillna(0.0)
            score = score + w * z.clip(-2, 2)
    cap = cfg.get("score_cap")
    return score.clip(-cap, cap) if cap else score


def fund_veto(c: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """營收大幅衰退就排除。資料缺漏時不否決（不因為沒資料就淘汰）。"""
    import config as C
    cfg = cfg if cfg is not None else C.FUNDAMENTALS
    if not cfg.get("enabled") or "rev_yoy" not in c.columns:
        return pd.Series(True, index=c.index)
    thr = cfg.get("veto_rev_yoy_below")
    if thr is None:
        return pd.Series(True, index=c.index)
    return ~(c["rev_yoy"] < thr)


def swing_candidates(day: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """波段池：趨勢 + 動能 + 突破 + 量能。"""
    f, s = cfg["ma_fast"], cfg["ma_slow"]
    d = day.dropna(subset=[f"ma{f}", f"ma{s}", "mom_short", "vol_ratio", "rsi14", "near_high"])
    ok = (
        (d["adjclose"] > d[f"ma{f}"])
        & (d[f"ma{f}"] > d[f"ma{s}"])
        & (d["near_high"] >= cfg["breakout_ratio"])
        & (d["vol_ratio"] >= cfg["vol_ratio_min"])
        & (d["rsi14"] < cfg["rsi_max"])
        & (d["mom_short"] > 0)
    )
    c = d[ok].copy()
    c = c[chip_veto(c) & fund_veto(c)]
    if c.empty:
        return c
    c["tech_score"] = (
        1.0 * _zscore(c["mom_short"])
        + 0.8 * _zscore(c["near_high"])
        + 0.5 * _zscore(c["vol_ratio"].clip(upper=3))
        - 0.4 * _zscore(c["atr_pct"])          # 同分時偏好波動小的
    )
    c["chip_score"] = chip_score(c)
    c["fund_score"] = fund_score(c)
    c["score"] = c["tech_score"] + c["chip_score"] + c["fund_score"]
    return c.sort_values("score", ascending=False)


def position_candidates(day: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """長線池：站上長期均線、長期動能為正、回檔幅度可控。"""
    m, s = cfg["ma_mid"], cfg["ma_slow"]
    d = day.dropna(subset=[f"ma{m}", f"ma{s}", "mom_long", "dd_from_high120"])
    ok = (
        (d["adjclose"] > d[f"ma{s}"])
        & (d[f"ma{m}"] > d[f"ma{s}"])
        & (d["mom_long"] > 0)
        & (d["dd_from_high120"] > -cfg["max_drawdown_from_high"])
    )
    c = d[ok].copy()
    c = c[chip_veto(c) & fund_veto(c)]
    if c.empty:
        return c
    c["tech_score"] = (
        1.0 * _zscore(c["mom_long"])
        + 0.5 * _zscore(c["dd_from_high120"])
        - 0.3 * _zscore(c["atr_pct"])
    )
    c["chip_score"] = chip_score(c)
    c["fund_score"] = fund_score(c)
    c["score"] = c["tech_score"] + c["chip_score"] + c["fund_score"]
    return c.sort_values("score", ascending=False)


def swing_exit(pos: dict, row: pd.Series, cfg: dict) -> str | None:
    """回傳出場原因，None 表示續抱。以收盤價判斷，隔日開盤執行。"""
    c = row["adjclose"]
    if c <= pos["entry_price"] * (1 + cfg["hard_stop"]):
        return "停損 -8%"
    trail = pos["peak"] - cfg["atr_trail"] * row["atr14"]
    if np.isfinite(trail) and c < trail:
        return f"移動停損 {cfg['atr_trail']}×ATR"
    if (not cfg.get("ma_fast_exit_off")) and c < row[f"ma{cfg['ma_fast']}"] and pos.get("below_ma", 0) >= 1:
        return f"連兩日跌破 {cfg['ma_fast']}MA"
    if pos["held_days"] >= cfg["max_hold_days"]:
        return "持有滿上限"
    return None


def position_exit(pos: dict, row: pd.Series, cfg: dict) -> str | None:
    c = row["adjclose"]
    if c < pos["peak"] * (1 - cfg["trail_pct"]):
        return f"自高點回落 {int(cfg['trail_pct']*100)}%"
    if c < row[f"ma{cfg['ma_slow']}"]:
        return f"跌破 {cfg['ma_slow']}MA"
    if pos["held_days"] >= cfg["max_hold_days"]:
        return "持有滿上限"
    return None


def failed_conditions(row: pd.Series, sleeve: str, cfg: dict) -> list[str]:
    """列出這檔卡在哪些硬性條件上——比只說「沒選上」有用得多。

    儀表板的「昨天在榜、今天掉出」區塊與 inspect_stock.py 共用這一份。
    兩邊必須是同一份邏輯，否則會出現「診斷說過了、卡片說沒過」的矛盾。
    """
    out = []
    c = row["adjclose"]
    if sleeve == "swing":
        f, s_ = cfg["ma_fast"], cfg["ma_slow"]
        if not c > row.get(f"ma{f}", float("nan")):
            out.append(f"收盤沒站上 {f}MA")
        if not row.get(f"ma{f}", 0) > row.get(f"ma{s_}", 0):
            out.append(f"{f}MA 沒有在 {s_}MA 之上")
        if not row["near_high"] >= cfg["breakout_ratio"]:
            out.append(f"距 60 日高點 {(1-row['near_high'])*100:.1f}%，超過 "
                       f"{(1-cfg['breakout_ratio'])*100:.0f}% 門檻")
        if not row["vol_ratio"] >= cfg["vol_ratio_min"]:
            out.append(f"量能 {row['vol_ratio']:.2f}，低於 {cfg['vol_ratio_min']}")
        if not row["rsi14"] < cfg["rsi_max"]:
            out.append(f"RSI {row['rsi14']:.0f} 超過 {cfg['rsi_max']}，過熱")
        if not row["mom_short"] > 0:
            out.append(f"20 日動能 {row['mom_short']*100:+.1f}%，為負")
    else:
        m, s_ = cfg["ma_mid"], cfg["ma_slow"]
        if not c > row.get(f"ma{s_}", float("nan")):
            out.append(f"收盤沒站上 {s_}MA")
        if not row.get(f"ma{m}", 0) > row.get(f"ma{s_}", 0):
            out.append(f"{m}MA 沒有在 {s_}MA 之上")
        if not row["mom_long"] > 0:
            out.append(f"120 日動能 {row['mom_long']*100:+.1f}%，為負")
        if not row["dd_from_high120"] > -cfg["max_drawdown_from_high"]:
            out.append(f"距 120 日高點 {row['dd_from_high120']*100:.1f}%，回檔超過 "
                       f"{cfg['max_drawdown_from_high']*100:.0f}%")
    return out
