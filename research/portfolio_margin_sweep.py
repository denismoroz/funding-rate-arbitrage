"""Policy sweep for portfolio_margin.py.

Sweeps:
  margin_buffer_x in [2.0, 3.0, 5.0]
  position_size   in [50.0, 100.0, 150.0]
  concurrency_cap in [3, 5]

Total: 3 x 3 x 2 = 18 configs.
All other params remain at portfolio_margin.py defaults.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from portfolio_margin import simulate_portfolio, BUDGET_CAP_USD

METRIC_COLUMNS = [
    'annual_pct', 'vol_pct', 'sharpe', 'sortino', 'max_dd_pct', 'calmar',
    'n_liquidations', 'n_top_ups', 'n_forced_closes', 'n_skipped_opens_capital',
    'min_margin_ratio', 'peak_committed_capital', 'final_equity',
    'total_funding', 'total_fees',
]

SWEEP_COLS = ['margin_buffer_x', 'position_size', 'concurrency_cap']

MARGIN_BUFFER_X_VALS = [2.0, 3.0, 5.0]
POSITION_SIZE_VALS   = [50.0, 100.0, 150.0]
CONCURRENCY_CAP_VALS = [3, 5]


def run_sweep() -> pd.DataFrame:
    rows = []
    config_num = 0
    total = len(MARGIN_BUFFER_X_VALS) * len(POSITION_SIZE_VALS) * len(CONCURRENCY_CAP_VALS)

    for mb in MARGIN_BUFFER_X_VALS:
        for ps in POSITION_SIZE_VALS:
            for cc in CONCURRENCY_CAP_VALS:
                config_num += 1
                label = f"buffer={mb} size={ps} K={cc}"
                print(f"[{config_num:2d}/{total}] Running {label} ...", flush=True)

                t0 = time.time()
                try:
                    metrics = simulate_portfolio(
                        margin_buffer_x=mb,
                        position_size=ps,
                        concurrency_cap=cc,
                        budget_cap_usd=BUDGET_CAP_USD,
                    )
                    elapsed = time.time() - t0
                    if elapsed > 30:
                        print(f"  WARNING: config took {elapsed:.1f}s", flush=True)
                    row = {
                        'margin_buffer_x': mb,
                        'position_size': ps,
                        'concurrency_cap': cc,
                    }
                    row.update({k: metrics[k] for k in METRIC_COLUMNS})
                    rows.append(row)
                    print(
                        f"  annual={metrics['annual_pct']:+.2f}%  "
                        f"calmar={metrics['calmar']:.3f}  "
                        f"sharpe={metrics['sharpe']:.3f}  "
                        f"liq={metrics['n_liquidations']}  "
                        f"({elapsed:.1f}s)",
                        flush=True,
                    )
                except Exception as exc:
                    elapsed = time.time() - t0
                    print(f"  ERROR after {elapsed:.1f}s: {exc}", flush=True)
                    row = {
                        'margin_buffer_x': mb,
                        'position_size': ps,
                        'concurrency_cap': cc,
                    }
                    for k in METRIC_COLUMNS:
                        row[k] = float('nan')
                    rows.append(row)

    return pd.DataFrame(rows, columns=SWEEP_COLS + METRIC_COLUMNS)


def print_summary(df: pd.DataFrame) -> None:
    valid = df.dropna(subset=['calmar', 'sharpe'])

    print("\n" + "=" * 60)
    print("TOP 3 BY CALMAR")
    print("=" * 60)
    top_calmar = valid.nlargest(3, 'calmar')
    for _, row in top_calmar.iterrows():
        print(
            f"  buffer={row['margin_buffer_x']:.1f}  size=${row['position_size']:.0f}"
            f"  K={int(row['concurrency_cap'])}"
            f"  calmar={row['calmar']:.3f}  annual={row['annual_pct']:+.2f}%"
            f"  maxdd={row['max_dd_pct']:.2f}%  sharpe={row['sharpe']:.3f}"
            f"  liq={int(row['n_liquidations'])}"
        )

    print("\n" + "=" * 60)
    print("TOP 3 BY SHARPE")
    print("=" * 60)
    top_sharpe = valid.nlargest(3, 'sharpe')
    for _, row in top_sharpe.iterrows():
        print(
            f"  buffer={row['margin_buffer_x']:.1f}  size=${row['position_size']:.0f}"
            f"  K={int(row['concurrency_cap'])}"
            f"  sharpe={row['sharpe']:.3f}  annual={row['annual_pct']:+.2f}%"
            f"  maxdd={row['max_dd_pct']:.2f}%  calmar={row['calmar']:.3f}"
            f"  liq={int(row['n_liquidations'])}"
        )

    print("\n" + "=" * 60)
    print("CONFIGS WITH >= 1 LIQUIDATION")
    print("=" * 60)
    liq = df[df['n_liquidations'] >= 1]
    if liq.empty:
        print("  None — all configs survived without liquidation.")
    else:
        for _, row in liq.iterrows():
            print(
                f"  buffer={row['margin_buffer_x']:.1f}  size=${row['position_size']:.0f}"
                f"  K={int(row['concurrency_cap'])}"
                f"  liq={int(row['n_liquidations'])}  annual={row['annual_pct']:+.2f}%"
            )
    print("=" * 60)


if __name__ == "__main__":
    wall_start = time.time()
    print("Portfolio margin policy sweep starting...")
    print(f"Grid: {MARGIN_BUFFER_X_VALS} x {POSITION_SIZE_VALS} x {CONCURRENCY_CAP_VALS}")
    print(f"Total configs: {len(MARGIN_BUFFER_X_VALS) * len(POSITION_SIZE_VALS) * len(CONCURRENCY_CAP_VALS)}\n")

    df = run_sweep()

    csv_path = Path(__file__).parent / "portfolio_margin_sweep_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nResults written to {csv_path}")
    print(f"Rows in CSV: {len(df)}")

    print_summary(df)

    wall_elapsed = time.time() - wall_start
    print(f"\nTotal wall time: {wall_elapsed:.1f}s")
