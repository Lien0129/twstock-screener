"""基本面因子：月營收、財報、估值。

最重要的一件事是**公告時間差**。月營收是次月 10 日前公告、財報是季末後 45 天內公告，
如果直接把資料對齊到營收月份或季末，等於偷看未來，回測會漂亮得不合理。
這裡一律用「可取得日」對齊，並且只用小於等於當天的最新一筆（merge_asof backward）。
"""
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUND_PATH = os.path.join(ROOT, "data", "fundamentals", "twstock_fundamentals.csv")

REVENUE_LAG_DAYS = 10   # 月營收：資料月份的次月 10 日前公告
FINANCIAL_LAG_DAYS = 45  # 財報：季末後 45 天內公告

FACTORS = [
    "rev_yoy",           # 單月營收年增率
    "rev_yoy_3m",        # 近三月營收年增率（平滑掉單月雜訊）
    "rev_12m_high",      # 單月營收是否創 12 個月新高
    "eps_ttm_yoy",       # 近四季 EPS 年增率
    "gross_margin",      # 毛利率
    "gross_margin_chg",  # 毛利率季變化
    "op_margin",         # 營益率
    "per_pctile",        # 本益比在自身過去兩年的百分位（低 = 便宜）
    "pbr_pctile",        # 股價淨值比百分位
    "dividend_yield",    # 現金殖利率
]


def load_raw() -> pd.DataFrame:
    if not os.path.exists(FUND_PATH):
        return pd.DataFrame()
    d = pd.read_csv(FUND_PATH, dtype={"code": str, "key": str})
    d["code"] = d["code"].str.zfill(4)
    d["date"] = pd.to_datetime(d["date"])
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    return d


def _revenue_frame(raw: pd.DataFrame) -> pd.DataFrame:
    r = raw[raw["dataset"] == "rev"].copy()
    if r.empty:
        return r
    r = r.rename(columns={"key": "rev_month", "value": "revenue"})
    r["avail"] = r["date"] + pd.Timedelta(days=REVENUE_LAG_DAYS)
    r = r.sort_values(["code", "date"])
    out = []
    for code, g in r.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        g["rev_yoy"] = g["revenue"] / g["revenue"].shift(12) - 1
        g["rev_yoy_3m"] = (g["revenue"].rolling(3).sum()
                           / g["revenue"].rolling(3).sum().shift(12) - 1)
        g["rev_12m_high"] = (g["revenue"] >= g["revenue"].rolling(12).max()).astype(float)
        out.append(g)
    return pd.concat(out, ignore_index=True)[
        ["code", "avail", "rev_yoy", "rev_yoy_3m", "rev_12m_high"]]


def _financial_frame(raw: pd.DataFrame) -> pd.DataFrame:
    f = raw[raw["dataset"] == "fin"].copy()
    if f.empty:
        return f
    w = f.pivot_table(index=["code", "date"], columns="key", values="value", aggfunc="last").reset_index()
    w["avail"] = w["date"] + pd.Timedelta(days=FINANCIAL_LAG_DAYS)
    w = w.sort_values(["code", "date"])
    out = []
    for code, g in w.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        rev = g.get("Revenue")
        g["gross_margin"] = g.get("GrossProfit") / rev if rev is not None else np.nan
        g["op_margin"] = g.get("OperatingIncome") / rev if rev is not None else np.nan
        g["gross_margin_chg"] = g["gross_margin"].diff()
        if "EPS" in g:
            ttm = g["EPS"].rolling(4).sum()
            g["eps_ttm_yoy"] = ttm / ttm.shift(4) - 1
        else:
            g["eps_ttm_yoy"] = np.nan
        out.append(g)
    cols = ["code", "avail", "gross_margin", "op_margin", "gross_margin_chg", "eps_ttm_yoy"]
    return pd.concat(out, ignore_index=True)[cols]


def _valuation_frame(raw: pd.DataFrame) -> pd.DataFrame:
    p = raw[raw["dataset"] == "per"].copy()
    if p.empty:
        return p
    w = p.pivot_table(index=["code", "date"], columns="key", values="value", aggfunc="last").reset_index()
    w = w.rename(columns={"YIELD": "dividend_yield"})
    out = []
    for code, g in w.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        # 百分位用「至今為止」的分布，不能用整段期間，否則偷看未來
        g["per_pctile"] = g["PER"].expanding(min_periods=60).apply(
            lambda s: (s.iloc[-1] >= s).mean(), raw=False)
        g["pbr_pctile"] = g["PBR"].expanding(min_periods=60).apply(
            lambda s: (s.iloc[-1] >= s).mean(), raw=False)
        out.append(g)
    w = pd.concat(out, ignore_index=True)
    w["avail"] = w["date"]
    return w[["code", "avail", "PER", "PBR", "dividend_yield", "per_pctile", "pbr_pctile"]]


def add_fundamental_factors(panel: pd.DataFrame) -> pd.DataFrame:
    raw = load_raw()
    if raw.empty:
        return panel
    base = panel.sort_values(["code", "date"]).copy()
    for frame in (_revenue_frame(raw), _financial_frame(raw), _valuation_frame(raw)):
        if frame is None or frame.empty:
            continue
        frame = frame.sort_values(["avail", "code"])
        base = pd.merge_asof(
            base.sort_values("date"), frame,
            left_on="date", right_on="avail", by="code", direction="backward",
        ).drop(columns=["avail"])
    return base.sort_values(["date", "code"]).reset_index(drop=True)


def per_band_target(row: pd.Series, low_pct: float = 0.5, high_pct: float = 0.8) -> dict | None:
    """本益比河流目標價：用目前 EPS 推估，套上該股自身的本益比區間。

    EPS(近四季) = 現價 ÷ 目前本益比。這比用 ATR 推出來的價格區間多了估值意涵，
    但仍然假設 EPS 不變——所以它回答的是「回到過去的估值水準會是多少錢」，不是預測獲利。
    """
    per, px = row.get("PER"), row.get("close")
    if not per or not np.isfinite(per) or per <= 0:
        return None
    eps = px / per
    hist = row.get("_per_hist")
    if hist is None or len(hist) < 60:
        return None
    return {
        "eps_ttm": round(float(eps), 2),
        "per_now": round(float(per), 1),
        "per_low": round(float(np.quantile(hist, low_pct)), 1),
        "per_high": round(float(np.quantile(hist, high_pct)), 1),
        "target_mid": round(float(eps * np.quantile(hist, low_pct)), 2),
        "target_high": round(float(eps * np.quantile(hist, high_pct)), 2),
    }
