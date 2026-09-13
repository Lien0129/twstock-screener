"""短線因子（持有 1~5 日）。

跟中長線因子分開的理由：檢定的持有期間不同，20 日有效的因子在 3 日可能完全沒用，反之亦然。
短線最大的敵人是交易成本——來回約 0.3%~0.6%，對 1~5 日的報酬是很重的拖累，
所以這裡的門檻要比 20 日因子嚴格得多。
"""
import numpy as np
import pandas as pd

# 價格類（用現有日線就能算）
PRICE_FACTORS = [
    "rev_1d",          # 前一日報酬（隔日反轉）
    "rev_5d",          # 近五日報酬（短線反轉）
    "bias_5",          # 五日乖離率
    "bias_10",         # 十日乖離率
    "gap_open",        # 開盤跳空幅度
    "intraday_pos",    # 收盤在當日高低區間的位置
    "squeeze",         # 波動壓縮：近 5 日 ATR ÷ 近 60 日 ATR
    "rs_index_5",      # 相對大盤五日強弱
    "rs_index_20",     # 相對大盤二十日強弱
    "vol_price_5d",    # 量增幅 × 漲幅（量價配合）
    "range_expand",    # 當日振幅 ÷ 近 20 日平均振幅
]

# 當沖類（需要 data/daytrading）
DAYTRADE_FACTORS = [
    "dt_ratio",        # 當沖比率 = 當沖量 ÷ 成交量
    "dt_ratio_chg",    # 當沖比率五日變化
    "dt_pnl_ratio",    # 當沖賣出金額 ÷ 買進金額（>1 代表當沖客整體賺錢）
]


def add_price_short_factors(panel: pd.DataFrame, index_series: pd.Series | None = None) -> pd.DataFrame:
    """加入價格類短線因子。index_series 是加權指數收盤（用來算相對強弱）。"""
    df = panel.sort_values(["code", "date"]).copy()
    out = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        c, h, l, o, v = g["adjclose"], g["high"], g["low"], g["open"], g["volume"]

        g["rev_1d"] = -(c.pct_change(1))          # 加負號：跌得多的排前面（反轉假設）
        g["rev_5d"] = -(c.pct_change(5))
        g["bias_5"] = c / c.rolling(5).mean() - 1
        g["bias_10"] = c / c.rolling(10).mean() - 1
        g["gap_open"] = o / c.shift(1) - 1
        rng = (h - l).replace(0, np.nan)
        g["intraday_pos"] = (g["close"] - l) / rng
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        g["squeeze"] = tr.rolling(5).mean() / tr.rolling(60).mean()
        g["range_expand"] = rng / rng.rolling(20).mean()
        vol5 = v.rolling(5).mean()
        g["vol_price_5d"] = (vol5 / vol5.shift(5) - 1) * c.pct_change(5)
        out.append(g)
    df = pd.concat(out, ignore_index=True)

    if index_series is not None:
        idx = index_series.rename("idx").to_frame()
        idx["idx_5"] = idx["idx"].pct_change(5)
        idx["idx_20"] = idx["idx"].pct_change(20)
        df = df.merge(idx.reset_index().rename(columns={"index": "date", "Date": "date"}),
                      on="date", how="left")
        df["rs_index_5"] = df.groupby("code")["adjclose"].pct_change(5) - df["idx_5"]
        df["rs_index_20"] = df.groupby("code")["adjclose"].pct_change(20) - df["idx_20"]
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def add_daytrade_factors(panel: pd.DataFrame, dt: pd.DataFrame) -> pd.DataFrame:
    """加入當沖因子。dt 欄位：code,date,dt_volume,buy_amount,sell_amount"""
    if dt is None or dt.empty:
        return panel
    df = panel.merge(dt, on=["code", "date"], how="left").sort_values(["code", "date"])
    out = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("date").copy()
        g["dt_ratio"] = g["dt_volume"] / g["volume"].replace(0, np.nan)
        g["dt_ratio_chg"] = g["dt_ratio"] - g["dt_ratio"].rolling(5).mean()
        g["dt_pnl_ratio"] = g["sell_amount"] / g["buy_amount"].replace(0, np.nan)
        out.append(g)
    return pd.concat(out, ignore_index=True).sort_values(["date", "code"]).reset_index(drop=True)


def short_factor_report(panel: pd.DataFrame, factors: list[str],
                        horizons=(1, 3, 5), split=None) -> pd.DataFrame:
    """對多個短線持有期間同時檢定。回傳每個因子在各期間的樣本內外 IC。"""
    df = panel.sort_values(["code", "date"]).copy()
    for h in horizons:
        df[f"fwd_{h}"] = df.groupby("code")["adjclose"].shift(-h) / df["adjclose"] - 1
    dates = sorted(df["date"].unique())
    split = split or dates[len(dates) // 2]

    rows = []
    for f in factors:
        if f not in df.columns:
            continue
        rec = {"因子": f}
        for h in horizons:
            sub = df.dropna(subset=[f, f"fwd_{h}"])
            for label, part in (("IS", sub[sub["date"] < split]), ("OOS", sub[sub["date"] >= split])):
                ics = (part.groupby("date")[[f, f"fwd_{h}"]]
                           .apply(lambda g: g[f].corr(g[f"fwd_{h}"], method="spearman")
                                  if g[f].nunique() > 3 else np.nan)
                           .dropna())
                rec[f"{h}日{label}"] = round(float(ics.mean()), 4) if len(ics) else np.nan
        rows.append(rec)
    return pd.DataFrame(rows)
