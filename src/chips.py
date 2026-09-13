"""籌碼面因子。

資料：FinMind 的三大法人買賣超與融資融券餘額（欄位皆為股數／張數）。
所有因子都會除以成交量或流通量做標準化，否則大型股的絕對買超永遠贏小型股。
"""
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHIP_PATH = os.path.join(ROOT, "data", "chips", "twstock_chips.csv")

FACTORS = [
    "foreign_5d_ratio",   # 外資近 5 日買超 ÷ 20 日均量
    "trust_5d_ratio",     # 投信近 5 日買超 ÷ 20 日均量
    "dealer_5d_ratio",    # 自營商近 5 日買超 ÷ 20 日均量
    "big3_20d_ratio",     # 三大法人近 20 日合計 ÷ 20 日均量
    "foreign_streak",     # 外資連續買超天數（負值代表連續賣超）
    "trust_streak",       # 投信連續買超天數
    "margin_chg_5d",      # 融資餘額 5 日變化率（散戶槓桿，通常是反指標）
    "short_margin_ratio", # 券資比
    "vol_zscore_20d",     # 成交量異常：當日量相對 20 日的 z 分數（熱度代理）
    "turnover_spike",     # 5 日均量 ÷ 60 日均量（熱度代理）
]


def _streak(s: pd.Series) -> pd.Series:
    """連續同號天數：連買為正、連賣為負。"""
    sign = np.sign(s.fillna(0))
    out = np.zeros(len(sign))
    run = 0
    for i, v in enumerate(sign.to_numpy()):
        if v > 0:
            run = run + 1 if run > 0 else 1
        elif v < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        out[i] = run
    return pd.Series(out, index=s.index)


def load_chips() -> pd.DataFrame:
    if not os.path.exists(CHIP_PATH):
        return pd.DataFrame()
    c = pd.read_csv(CHIP_PATH, dtype={"code": str})
    c["code"] = c["code"].str.zfill(4)
    c["date"] = pd.to_datetime(c["date"])
    for col in ["foreign_net", "trust_net", "dealer_net", "margin_bal", "short_bal"]:
        c[col] = pd.to_numeric(c[col], errors="coerce")
    return c


def add_chip_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """把籌碼因子併進價格面板。沒有籌碼資料時原樣回傳。"""
    chips = load_chips()
    if chips.empty:
        return panel
    df = panel.merge(chips, on=["code", "date"], how="left").sort_values(["code", "date"])

    out = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        vol20 = g["volume"].rolling(20).mean()
        g["foreign_5d_ratio"] = g["foreign_net"].rolling(5).sum() / vol20
        g["trust_5d_ratio"] = g["trust_net"].rolling(5).sum() / vol20
        g["dealer_5d_ratio"] = g["dealer_net"].rolling(5).sum() / vol20
        big3 = g["foreign_net"] + g["trust_net"] + g["dealer_net"]
        g["big3_20d_ratio"] = big3.rolling(20).sum() / (vol20 * 20)
        g["foreign_streak"] = _streak(g["foreign_net"])
        g["trust_streak"] = _streak(g["trust_net"])
        g["margin_chg_5d"] = g["margin_bal"].pct_change(5)
        g["short_margin_ratio"] = g["short_bal"] / g["margin_bal"].replace(0, np.nan)
        v = g["volume"]
        g["vol_zscore_20d"] = (v - v.rolling(20).mean()) / v.rolling(20).std(ddof=0)
        g["turnover_spike"] = v.rolling(5).mean() / v.rolling(60).mean()
        out.append(g)
    return pd.concat(out, ignore_index=True).sort_values(["date", "code"]).reset_index(drop=True)


# ------------------------------------------------------------------ 因子檢定
def forward_returns(panel: pd.DataFrame, horizon: int = 20) -> pd.DataFrame:
    df = panel.sort_values(["code", "date"]).copy()
    df["fwd_ret"] = df.groupby("code")["adjclose"].shift(-horizon) / df["adjclose"] - 1
    return df


def factor_report(panel: pd.DataFrame, factors=None, horizon: int = 20,
                  split: pd.Timestamp | None = None) -> pd.DataFrame:
    """對每個因子算 IC（每日橫斷面的 Spearman 相關），分樣本內外。

    IC 的意義：當天因子值高的股票，未來 N 天報酬是不是真的比較好。
    |IC| < 0.02 基本上等於沒有訊息；符號在樣本內外反轉代表不穩定。
    """
    factors = factors or [f for f in FACTORS if f in panel.columns]
    df = forward_returns(panel, horizon).dropna(subset=["fwd_ret"])
    if split is None:
        dates = sorted(df["date"].unique())
        split = dates[len(dates) // 2]

    rows = []
    for f in factors:
        sub = df.dropna(subset=[f])
        if sub.empty:
            continue
        rec = {"因子": f, "樣本數": len(sub)}
        for label, part in (("樣本內", sub[sub["date"] < split]), ("樣本外", sub[sub["date"] >= split])):
            ics = (part.groupby("date")[[f, "fwd_ret"]]
                       .apply(lambda g: g[f].corr(g["fwd_ret"], method="spearman")
                              if g[f].nunique() > 3 else np.nan)
                       .dropna())
            rec[f"{label}IC"] = ics.mean() if len(ics) else np.nan
            rec[f"{label}勝率"] = (ics > 0).mean() if len(ics) else np.nan
        # 五分位價差：最高組平均報酬 − 最低組平均報酬（樣本外）
        oos = sub[sub["date"] >= split]
        try:
            q = oos.groupby("date")[f].transform(lambda s: pd.qcut(s, 5, labels=False, duplicates="drop"))
            top = oos.loc[q == 4, "fwd_ret"].mean()
            bot = oos.loc[q == 0, "fwd_ret"].mean()
            rec["樣本外五分位價差"] = top - bot
        except Exception:
            rec["樣本外五分位價差"] = np.nan
        rows.append(rec)
    return pd.DataFrame(rows)
