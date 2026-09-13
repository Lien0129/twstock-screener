"""回測引擎。

執行假設（刻意保守）：
  * 訊號在 T 日收盤後產生，T+1 日「開盤價」成交。
  * 買賣都算滑價，手續費雙邊，賣出加證交稅。
  * 每個部位固定佔「當時權益」的一個比例，不加槓桿、不融資。
  * 沒有停損單掛在盤中，一律收盤判斷、隔日開盤執行——實際操作若用盤中停損會不同。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config as C
import strategy as S


class Backtest:
    def __init__(self, panel: pd.DataFrame, regime: pd.Series | None = None,
                 capital: float = C.INITIAL_CAPITAL, sleeve_weight=(0.5, 0.5)):
        self.panel = panel
        self.regime = regime  # index=date, columns bool: swing_ok / position_ok
        self.capital = capital
        self.sleeve_weight = dict(zip(("swing", "position"), sleeve_weight))
        self.positions: dict[tuple[str, str], dict] = {}
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []

    # ------------------------------------------------------------------ 工具
    def _cost_buy(self, px):
        return px * (1 + C.SLIPPAGE) * (1 + C.FEE_RATE * C.FEE_DISCOUNT)

    def _cost_sell(self, px):
        return px * (1 - C.SLIPPAGE) * (1 - C.FEE_RATE * C.FEE_DISCOUNT - C.TAX_RATE)

    def _regime_ok(self, date, sleeve):
        if self.regime is None:
            return True
        if date not in self.regime.index:
            return True
        return bool(self.regime.loc[date, f"{sleeve}_ok"])

    # ------------------------------------------------------------------ 主迴圈
    def run(self):
        dates = sorted(self.panel["date"].unique())
        by_date = {d: g.set_index("code") for d, g in self.panel.groupby("date")}
        cash = self.capital
        pending_buys: list[dict] = []
        pending_sells: list[tuple] = []

        for i, date in enumerate(dates):
            day = by_date[date]

            # 1) 先執行昨天決定的賣出（今天開盤）
            for key, reason in pending_sells:
                if key not in self.positions or key[0] not in day.index:
                    continue
                pos = self.positions.pop(key)
                px = self._cost_sell(day.loc[key[0], "adj_open"])
                proceeds = px * pos["shares"]
                cash += proceeds
                self.trades.append({
                    "sleeve": key[1], "code": key[0], "name": C.UNIVERSE.get(key[0], key[0]),
                    "entry_date": pos["entry_date"], "exit_date": date,
                    "entry_price": pos["entry_price"], "exit_price": px,
                    "shares": pos["shares"], "pnl": proceeds - pos["cost"],
                    "ret": proceeds / pos["cost"] - 1, "hold_days": pos["held_days"],
                    "reason": reason,
                })
            pending_sells = []

            # 2) 執行昨天決定的買進（今天開盤）
            for order in pending_buys:
                code, sleeve = order["code"], order["sleeve"]
                if (code, sleeve) in self.positions or code not in day.index:
                    continue
                px = self._cost_buy(day.loc[code, "adj_open"])
                budget = order["budget"]
                shares = int(budget // px)
                if shares <= 0 or px * shares > cash:
                    continue
                cost = px * shares
                cash -= cost
                self.positions[(code, sleeve)] = {
                    "entry_date": date, "entry_price": px, "shares": shares,
                    "cost": cost, "peak": day.loc[code, "adjclose"], "held_days": 0,
                    "below_ma": 0, "reason_in": order["reason"],
                }
            pending_buys = []

            # 3) 盤後：更新部位狀態、檢查出場
            for key, pos in list(self.positions.items()):
                code, sleeve = key
                if code not in day.index:
                    continue
                row = day.loc[code]
                pos["held_days"] += 1
                pos["peak"] = max(pos["peak"], row["adjclose"])
                cfg = C.SWING if sleeve == "swing" else C.POSITION
                if sleeve == "swing":
                    below = row["adjclose"] < row[f"ma{cfg['ma_fast']}"]
                    reason = S.swing_exit(pos, row, cfg)
                    pos["below_ma"] = pos["below_ma"] + 1 if below else 0
                else:
                    reason = S.position_exit(pos, row, cfg)
                if reason:
                    pending_sells.append((key, reason))

            # 4) 盤後：計算權益、挑明天要買的標的
            mv = sum(
                self.positions[k]["shares"] * by_date[date].loc[k[0], "adjclose"]
                for k in self.positions if k[0] in day.index
            )
            equity = cash + mv
            self.equity_curve.append({"date": date, "equity": equity, "cash": cash,
                                      "n_pos": len(self.positions)})

            if i == len(dates) - 1:
                break

            held_next = {k for k, _ in pending_sells}
            for sleeve, cfg, picker in (
                ("swing", C.SWING, S.swing_candidates),
                ("position", C.POSITION, S.position_candidates),
            ):
                if not self._regime_ok(date, sleeve):
                    continue
                current = [k for k in self.positions if k[1] == sleeve and k not in held_next]
                slots = cfg["max_positions"] - len(current)
                if slots <= 0:
                    continue
                cands = picker(day.reset_index(), cfg)
                if cands.empty:
                    continue
                owned = {k[0] for k in self.positions if k[1] == sleeve}
                budget = equity * self.sleeve_weight[sleeve] / cfg["max_positions"]
                for _, r in cands.iterrows():
                    if slots <= 0:
                        break
                    if r["code"] in owned:
                        continue
                    pending_buys.append({
                        "code": r["code"], "sleeve": sleeve, "budget": budget,
                        "reason": f"score={r['score']:.2f}",
                    })
                    slots -= 1

        self.equity = pd.DataFrame(self.equity_curve).set_index("date")
        self.trade_log = pd.DataFrame(self.trades)
        return self

    # ------------------------------------------------------------------ 績效
    def stats(self) -> dict:
        eq = self.equity["equity"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        total = eq.iloc[-1] / eq.iloc[0] - 1
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
        dd = (eq / eq.cummax() - 1).min()
        rets = eq.pct_change().dropna()
        sharpe = rets.mean() / rets.std(ddof=0) * np.sqrt(252) if rets.std(ddof=0) > 0 else np.nan
        t = self.trade_log
        out = {
            "起始": str(eq.index[0].date()), "結束": str(eq.index[-1].date()),
            "總報酬": total, "年化報酬": cagr, "最大回撤": dd, "Sharpe": sharpe,
            "交易次數": len(t),
        }
        if len(t):
            wins = t[t["ret"] > 0]
            out.update({
                "勝率": len(wins) / len(t),
                "平均獲利": wins["ret"].mean() if len(wins) else 0.0,
                "平均虧損": t[t["ret"] <= 0]["ret"].mean() if len(t) > len(wins) else 0.0,
                "賺賠比": abs(wins["ret"].mean() / t[t["ret"] <= 0]["ret"].mean())
                          if len(wins) and len(t) > len(wins) else np.nan,
                "平均持有天數": t["hold_days"].mean(),
            })
        return out


def buy_and_hold(panel: pd.DataFrame, code: str, capital=C.INITIAL_CAPITAL) -> pd.Series:
    d = panel[panel["code"] == code].sort_values("date").set_index("date")["adjclose"]
    return capital * d / d.iloc[0]


def equal_weight_universe(panel: pd.DataFrame, capital=C.INITIAL_CAPITAL) -> pd.Series:
    px = panel.pivot_table(index="date", columns="code", values="adjclose")
    norm = px / px.iloc[0]
    return capital * norm.mean(axis=1)
