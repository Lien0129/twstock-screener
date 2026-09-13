"""跑回測並印出績效比較。"""
import sys, os
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import indicators as I
import backtest as B
import chips as CH
import fundamentals as F
import shortterm as ST

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def price_csv() -> str:
    """股價檔的位置。

    daily_update.py 每天寫的是 data/prices/twstock_history.csv，
    但這裡原本只讀 data/twstock_history.csv（建庫時的舊路徑）。
    兩個檔案都存在時就會出現「每天更新成功、儀表板卻停在舊資料」——
    在 kai 的電腦上實際發生過：prices/ 是 106 檔到 8/28，
    根目錄那份卻是 30 檔停在 8/24，本機儀表板整整錯了四天。
    優先讀 prices/，找不到才退回根目錄。
    """
    a = os.path.join(DATA, "prices", "twstock_history.csv")
    return a if os.path.exists(a) else os.path.join(DATA, "twstock_history.csv")


def load_panel():
    raw = pd.read_csv(price_csv(), dtype={"code": str})
    panel = I.build_panel(raw, C.SWING, C.POSITION)
    panel = F.add_fundamental_factors(CH.add_chip_factors(panel))
    dt_path = os.path.join(DATA, "daytrading", "twstock_daytrading.csv")
    if os.path.exists(dt_path):
        dt = pd.read_csv(dt_path, dtype={"code": str})
        dt["date"] = pd.to_datetime(dt["date"])
        panel = ST.add_daytrade_factors(panel, dt)
    return panel


def load_regime(panel):
    """大盤濾網。有加權指數就用指數，沒有就用觀察池廣度替代。"""
    path = os.path.join(DATA, "twstock_macro.csv")
    if os.path.exists(path):
        m = pd.read_csv(path)
        m["date"] = pd.to_datetime(m["date"])
        twii = m[m["symbol"] == C.REGIME["index_symbol"]].sort_values("date").set_index("date")["close"]
        coverage = twii.index.isin(panel["date"].unique()).sum() / panel["date"].nunique()
        if coverage < 0.95:
            print(f"⚠️  加權指數只覆蓋了 {coverage:.0%} 的交易日，大盤濾網會在缺資料的日子失效！")
        if len(twii) > C.REGIME["position_ma"]:
            df = pd.DataFrame({
                "swing_ok": twii > twii.rolling(C.REGIME["swing_ma"]).mean(),
                "position_ok": twii > twii.rolling(C.REGIME["position_ma"]).mean(),
            })
            return df.fillna(True), "加權指數"
    breadth = panel.groupby("date")["above_ma60"].mean()
    df = pd.DataFrame({
        "swing_ok": breadth > C.REGIME["breadth_threshold"],
        "position_ok": breadth > C.REGIME["breadth_threshold"],
    })
    return df.fillna(True), "觀察池廣度（替代）"


def pct(x):
    return f"{x*100:,.1f}%" if pd.notna(x) else "n/a"


def main():
    panel = load_panel()
    regime, regime_src = load_regime(panel)
    print(f"資料期間 {panel.date.min().date()} ~ {panel.date.max().date()}，"
          f"{panel.code.nunique()} 檔，大盤濾網來源：{regime_src}\n")

    results = {}
    for label, rg in (("有大盤濾網", regime), ("無大盤濾網", None)):
        bt = B.Backtest(panel, regime=rg).run()
        results[label] = bt
        s = bt.stats()
        print(f"── {label} " + "─" * 40)
        print(f"總報酬 {pct(s['總報酬'])}  年化 {pct(s['年化報酬'])}  "
              f"最大回撤 {pct(s['最大回撤'])}  Sharpe {s['Sharpe']:.2f}")
        if s["交易次數"]:
            print(f"交易 {s['交易次數']} 次  勝率 {pct(s['勝率'])}  "
                  f"平均獲利 {pct(s['平均獲利'])}  平均虧損 {pct(s['平均虧損'])}  "
                  f"賺賠比 {s['賺賠比']:.2f}  平均持有 {s['平均持有天數']:.0f} 天")
        print()

    # 基準
    bt = results["有大盤濾網"]
    eq = bt.equity["equity"]
    for name, series in (("等權重買進持有觀察池", B.equal_weight_universe(panel)),
                         ("單押台積電", B.buy_and_hold(panel, "2330"))):
        s = series.reindex(eq.index).ffill()
        total = s.iloc[-1] / s.iloc[0] - 1
        dd = (s / s.cummax() - 1).min()
        r = s.pct_change().dropna()
        sharpe = r.mean() / r.std(ddof=0) * (252 ** 0.5)
        print(f"[基準] {name}：總報酬 {pct(total)}  最大回撤 {pct(dd)}  Sharpe {sharpe:.2f}")

    print("\n── 各出場原因統計 " + "─" * 30)
    t = bt.trade_log
    if len(t):
        g = t.groupby("reason").agg(次數=("ret", "size"), 平均報酬=("ret", "mean"))
        g["平均報酬"] = g["平均報酬"].map(pct)
        print(g.sort_values("次數", ascending=False).to_string())
        print("\n── 兩個池子分別表現 " + "─" * 28)
        g2 = t.groupby("sleeve").agg(次數=("ret", "size"), 勝率=("ret", lambda x: (x > 0).mean()),
                                     平均報酬=("ret", "mean"), 總損益=("pnl", "sum"))
        g2["勝率"] = g2["勝率"].map(pct); g2["平均報酬"] = g2["平均報酬"].map(pct)
        print(g2.to_string())

    out = os.path.join(os.path.dirname(__file__), "..", "output")
    os.makedirs(out, exist_ok=True)
    bt.equity.to_csv(os.path.join(out, "equity_curve.csv"))
    bt.trade_log.to_csv(os.path.join(out, "trades.csv"), index=False)
    print(f"\n已輸出 equity_curve.csv / trades.csv")


if __name__ == "__main__":
    main()
