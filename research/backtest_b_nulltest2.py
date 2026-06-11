"""
Null-тест победителей signal-sweep (mom30d, mom14|mom30) — те же circular shifts.
Подтверждаем, что их timing-эдж реален, а не выловлен перебором 8 сигналов.
Косты как в свипе: thr=0.20, slip=5bps, lag=1, cash=4%. refill=mom14>0 (real).
"""
import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar
from backtest_b_signalsweep import build_signals

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
N_TRIALS = 1000
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04
TEST_SIGS = ["mom30d", "mom14|mom30"]


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def hpnl(df, stk, hedge, refill):
    pnl, info = simulate_constdollar(df, stk, hedge, rebal_threshold=THR,
                                     risk_free_apr=CASH, refill_confirm=refill,
                                     signal_lag=LAG, slippage=SLIP)
    years = len(pnl) / HOURS_PER_YEAR
    return info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100


def main():
    rng = np.random.default_rng(42)
    for sig in TEST_SIGS:
        print("\n" + "=" * 88)
        print(f"NULL TEST: {sig}  ({N_TRIALS} circular shifts, метрика = hedge_pnl)")
        print("=" * 88)
        signs_pos = 0
        for coin in COINS:
            df = load_data(coin)
            if df.empty:
                continue
            close = df["close"].values
            stk = STAKING_YIELD.get(coin, 0.0)
            hedge = build_signals(close)[sig]
            refill = (pd.Series(close).pct_change(14 * 24).fillna(0).values > 0)
            n = len(close)

            real = hpnl(df, stk, hedge, refill)
            shifts = rng.integers(int(0.05 * n), int(0.95 * n), size=N_TRIALS)
            null = np.array([hpnl(df, stk, np.roll(hedge, int(k)), refill) for k in shifts])
            p = float(np.mean(null >= real))
            z = (real - null.mean()) / (null.std() + 1e-9)
            signs_pos += int(real > null.mean())
            print(f"  {coin:5s} real={real:6.2f}  null_mean={null.mean():6.2f}  z={z:5.2f}  p={p:.3f}"
                  f"{'  *' if p <= 0.05 else ''}")
        # sign-test по 6 монетам
        from math import comb
        p_sign = sum(comb(6, k) for k in range(signs_pos, 7)) / 2**6
        print(f"  --> real>null на {signs_pos}/6 коинах; sign-test p={p_sign:.4f}")


if __name__ == "__main__":
    main()
