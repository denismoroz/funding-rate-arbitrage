"""
Strategy B — ЧЕСТНЫЙ прогон (снимаем артефакты бэктеста).

Что убрано относительно backtest_b_constdollar.py (v3):
  1. Per-coin подбор сигнала   -> единый mom14d для ВСЕХ монет (no cherry-pick).
  2. Same-bar look-ahead       -> signal_lag=1 (торгуем по close следующего часа).
  3. Выдуманный cash 10%       -> 4% (реалистичный USDC-лендинг).
  4. Нулевой slippage          -> +5 bps на каждую ногу.
  5. Линейная годовая          -> CAGR (compounded).

DD/Sharpe берём из compute_metrics (как раньше, для сопоставимости),
но доходность смотрим как CAGR.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    STAKING_YIELD, load_data, buy_and_hold, compute_metrics,
    HOURS_PER_YEAR, TOTAL_CAPITAL,
)
from backtest_b_constdollar import simulate_constdollar, build_mom14d, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]

CASH_APR  = 0.04     # реалистичный USDC lending
SLIPPAGE  = 0.0005   # 5 bps на ногу сверх taker-fee
SIG_LAG   = 1        # торгуем сигнал следующего часа


def cagr(pnl_arr: np.ndarray) -> float:
    years = len(pnl_arr) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl_arr))
    if end <= 0:
        return -100.0
    return ((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        close = df["close"].values

        hedge    = build_mom14d(close)      # ЕДИНЫЙ сигнал для всех
        trend_up = build_trend_up(close)

        pnl, info = simulate_constdollar(
            df, staking, hedge,
            risk_free_apr=CASH_APR,
            refill_confirm=trend_up,
            signal_lag=SIG_LAG,
            slippage=SLIPPAGE,
        )
        m = compute_metrics(pnl)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)
        years  = len(df) / HOURS_PER_YEAR

        rows.append({
            "coin":       coin,
            "cagr":       round(cagr(pnl), 2),
            "max_dd":     m["max_dd_pct"],
            "calmar":     round(cagr(pnl) / m["max_dd_pct"], 2) if m["max_dd_pct"] > 0 else 0,
            "sharpe":     m["sharpe"],
            "trades":     info["trades"],
            "funding":    round(info["funding_total"] / TOTAL_CAPITAL / years * 100, 2),
            "hedge_pnl":  round(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100, 2),
            "fees":       round(-(info["perp_fees_total"] + info["spot_fees_total"]) / TOTAL_CAPITAL / years * 100, 2),
            "bh_cagr":    round(cagr(bh_pnl), 2),
            "bh_dd":      bh_m["max_dd_pct"],
            "bh_calmar":  round(cagr(bh_pnl) / bh_m["max_dd_pct"], 2) if bh_m["max_dd_pct"] > 0 else 0,
        })

    res = pd.DataFrame(rows)
    print("\n" + "=" * 110)
    print("STRATEGY B — ЧЕСТНЫЙ прогон: единый mom14d, lag=1, slippage=5bps, cash=4%, CAGR")
    print("=" * 110)
    print(res.to_string(index=False))

    print("\n" + "-" * 110)
    print("Портфель (equal-weight по монетам):")
    print(f"  Strategy B : CAGR={res['cagr'].mean():.2f}%  avgDD={res['max_dd'].mean():.2f}%  "
          f"Calmar(avg/avg)={res['cagr'].mean()/res['max_dd'].mean():.2f}  "
          f"Sharpe={res['sharpe'].mean():.2f}")
    print(f"  Buy & Hold : CAGR={res['bh_cagr'].mean():.2f}%  avgDD={res['bh_dd'].mean():.2f}%  "
          f"Calmar(avg/avg)={res['bh_cagr'].mean()/res['bh_dd'].mean():.2f}")
    print(f"  Доход-сплит: funding={res['funding'].mean():.2f}  hedge_pnl={res['hedge_pnl'].mean():.2f}  "
          f"fees={res['fees'].mean():.2f}  (остальное = spot+staking+cash)")

    out = Path(__file__).parent / "backtest_b_honest_results.csv"
    res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
