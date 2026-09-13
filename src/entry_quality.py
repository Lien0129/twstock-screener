"""進場品質因子：回答「這個價位是不是一個好的進場點」。

現有評分只回答「這檔股票好不好」——而且有個結構性問題：
technical score 的組成裡有「20 日漲幅」跟「距 60 日高點」，
所以**漲得越兇、買得越高，分數越高**。它沒有任何一個欄位在描述
「你現在追進去的代價」。

這裡的五個因子全部只描述「進場時機」，跟股票本身的好壞無關：

  ext_ma20      乖離（收盤 / 20MA − 1）。追高的直接度量。
  stop_atr_fit  硬停損 8% 相對於 ATR% 的倍數。低於 1.5 表示停損落在
                單日正常波動裡面，會被雜訊掃出場——這正是聯茂那筆的機制。
  dist_high60   距 60 日高點的距離（正值 = 在高點下方）。用來測試
                「回檔進場」是不是真的優於「突破追高」。
  vol_fade      今日量 / 5 日均量 − 1。爆量之後量縮 = 追價力道衰竭。
  limit_up_5d   近 5 個交易日的漲停根數。把連續漲停研究直接因子化。

每一個都要通過跟其他因子一樣的關卡才會被採用：
樣本內外 IC 同號，且樣本外 IC > 0.02。沒過就不用——這個專案到目前為止
已經有六個「看起來很合理」的想法死在這一關。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FACTORS = ["ext_ma20", "stop_atr_fit", "dist_high60", "vol_fade", "limit_up_5d"]

# 方向：+1 表示「數值越大，預期報酬越好」；−1 表示越大越差。
# 這是事前假設，不是看完結果才填的——寫在這裡就是為了讓後面的檢驗
# 有辦法判斷「符號有沒有跟假設相反」。
EXPECTED_SIGN = {
    "ext_ma20": -1,      # 越乖離越差
    "stop_atr_fit": +1,  # 停損相對波動越寬越好
    "dist_high60": +1,   # 離高點有點距離（回檔）比追在高點好
    "vol_fade": -1,      # 量縮越多越差
    "limit_up_5d": -1,   # 漲停根數越多越差
}

LIMIT_UP = 0.095  # 台股漲停 10%，用 9.5% 容納跳動單位造成的誤差


def add_entry_quality(panel: pd.DataFrame, hard_stop: float = 0.08) -> pd.DataFrame:
    """在 panel 上加出五個進場品質欄位。panel 需已含 add_indicators 的輸出。"""
    d = panel.sort_values(["code", "date"]).copy()

    d["ext_ma20"] = d["adjclose"] / d["ma20"] - 1
    d["stop_atr_fit"] = hard_stop / d["atr_pct"].replace(0, np.nan)
    d["dist_high60"] = 1 - d["near_high"]

    vol_ma5 = d.groupby("code")["volume"].transform(lambda s: s.rolling(5).mean())
    d["vol_fade"] = d["volume"] / vol_ma5.replace(0, np.nan) - 1

    ret = d.groupby("code")["adjclose"].pct_change()
    is_lim = (ret >= LIMIT_UP).astype(float)
    d["limit_up_5d"] = is_lim.groupby(d["code"]).transform(
        lambda s: s.rolling(5).sum())

    return d


def signed(d: pd.DataFrame) -> pd.DataFrame:
    """把每個因子乘上預期方向，讓「越大越好」統一成正向，方便加總。"""
    out = d.copy()
    for f in FACTORS:
        if f in out:
            out[f + "_s"] = out[f] * EXPECTED_SIGN[f]
    return out
