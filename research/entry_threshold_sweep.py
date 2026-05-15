"""
2D sweep: entry_threshold × min_hold
Фикс: K=3, exit=-0.15, signal_window=12

Вопрос: сколько платим по risk-метрикам при снижении entry_threshold от 30% к 5%?
И как min_hold это модифицирует?
"""

import numpy as np
import pandas as pd
from pathlib import Path
from concurrency_cap import simulate_multi_capped, metrics_on_capital
from engine import TOTAL_CAPITAL, HOURS_PER_YEAR

COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K = 3
EXIT_THRESHOLD = -0.15
SIGNAL_WINDOW = 12

ENTRY_THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
MIN_HOLDS = [1, 24, 72, 120]

CAPITAL_BASE = K * TOTAL_CAPITAL  # $6000

rows = []
for entry in ENTRY_THRESHOLDS:
    for min_hold in MIN_HOLDS:
        pnl, cap_per_hour, info = simulate_multi_capped(
            COINS,
            max_concurrent=K,
            entry_threshold=entry,
            exit_threshold=EXIT_THRESHOLD,
            min_hold=min_hold,
            signal_window=SIGNAL_WINDOW,
        )
        n_hours = len(pnl)
        m = metrics_on_capital(pnl, CAPITAL_BASE, n_hours)

        total_trades = info["total_trades"]
        time_in_market_pct = round((cap_per_hour > 0).mean() * 100, 1)

        # Runs of empty (cap_per_hour == 0) windows
        empty = cap_per_hour == 0
        runs = []
        run = 0
        for x in empty:
            if x:
                run += 1
            else:
                if run > 0:
                    runs.append(run)
                run = 0
        if run > 0:
            runs.append(run)
        n_empty_windows = len(runs)
        median_wait_hours = int(np.median(runs)) if runs else 0

        rows.append({
            "entry":               entry,
            "min_hold":            min_hold,
            "annual":              m["annual"],
            "max_dd":              m["max_dd"],
            "calmar":              m["calmar"],
            "sharpe":              m["sharpe"],
            "trades":              total_trades,
            "time_in_market_pct":  time_in_market_pct,
            "n_empty_windows":     n_empty_windows,
            "median_wait_hours":   median_wait_hours,
        })
        print(f"entry={entry:.2f} min_hold={min_hold:3d} -> annual={m['annual']:6.2f}% calmar={m['calmar']:6.1f} trades={total_trades}")

df = pd.DataFrame(rows, columns=[
    "entry", "min_hold", "annual", "max_dd", "calmar", "sharpe",
    "trades", "time_in_market_pct", "n_empty_windows", "median_wait_hours",
])

print()
print(df.to_string(index=False))

out = Path(__file__).parent / "entry_threshold_sweep_results.csv"
df.to_csv(out, index=False)
print(f"\nСохранено: {out}")
