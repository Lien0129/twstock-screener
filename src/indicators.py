"""技術指標計算。

所有指標一律用「還原股價」(adjclose) 計算，避免除權息造成假跌破。
成交量與價位則保留原始值，供部位計算與顯示使用。
"""
import numpy as np
import pandas as pd


def _wilder_rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder_rma(delta.clip(lower=0), n)
    loss = _wilder_rma(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return _wilder_rma(tr, n)


def add_indicators(df: pd.DataFrame, swing_cfg: dict, pos_cfg: dict) -> pd.DataFrame:
    """對單一股票的時間序列加上所有指標欄位。df 需依日期排序。"""
    d = df.sort_values("date").copy()

    # 還原價格序列：用 adjclose 當基準，高低開按同比例還原
    ratio = (d["adjclose"] / d["close"]).replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
    d["adj_open"] = d["open"] * ratio
    d["adj_high"] = d["high"] * ratio
    d["adj_low"] = d["low"] * ratio
    c, h, l = d["adjclose"], d["adj_high"], d["adj_low"]

    for n in sorted({swing_cfg["ma_fast"], swing_cfg["ma_slow"],
                     pos_cfg["ma_mid"], pos_cfg["ma_slow"]}):
        d[f"ma{n}"] = c.rolling(n).mean()

    d["rsi14"] = rsi(c, 14)
    d["atr14"] = atr(h, l, c, swing_cfg["atr_window"])
    d["atr_pct"] = d["atr14"] / c

    d["mom_short"] = c.pct_change(swing_cfg["mom_lookback"])
    d["mom_long"] = c.pct_change(pos_cfg["mom_lookback"])
    d["ret_5"] = c.pct_change(5)

    bw = swing_cfg["breakout_window"]
    d["high_60"] = h.rolling(bw).max()
    d["near_high"] = c / d["high_60"]
    d["high_120"] = h.rolling(pos_cfg["ma_slow"]).max()
    d["dd_from_high120"] = c / d["high_120"] - 1

    d["vol_ma5"] = d["volume"].rolling(5).mean()
    d["vol_ma60"] = d["volume"].rolling(60).mean()
    d["vol_ratio"] = d["vol_ma5"] / d["vol_ma60"]

    d["above_ma60"] = (c > d[f"ma{swing_cfg['ma_slow']}"]).astype(float)
    return d


def build_panel(raw: pd.DataFrame, swing_cfg: dict, pos_cfg: dict) -> pd.DataFrame:
    """把長格式原始資料轉成含指標的面板資料。"""
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    raw["code"] = raw["code"].astype(str).str.zfill(4)
    frames = [add_indicators(g, swing_cfg, pos_cfg) for _, g in raw.groupby("code", sort=False)]
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["date", "code"]).reset_index(drop=True)
