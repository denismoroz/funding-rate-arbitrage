"""
2D sweep по signal_window × phase1_negative_patience для two_phase_dynamic.

Universe: live config (BTC, ETH, SOL, HYPE, PURR).
Grid: 5×5 = 25 точек.
"""

import sys
import csv
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import TOTAL_CAPITAL, HOURS_PER_YEAR
from two_phase_dynamic import simulate_two_phase_dynamic
from adaptive_entry import metrics_window

# ── Universe (live config) ────────────────────────────────────────────────────
COINS  = ["BTC", "ETH", "SOL", "HYPE", "PURR"]
K      = 3
CAPITAL_BASE = K * TOTAL_CAPITAL

# ── Fixed parameters (live config) ───────────────────────────────────────────
BASE_MIN_HOLD = 24
ENTRY    = 0.10
SM       = 5.0
CAP_MH   = 720
P1_CAP   = 720
P2_EXIT  = -0.10
FEE_MULT = 1.0

# ── Sweep grids ───────────────────────────────────────────────────────────────
SIGNAL_WINDOWS = [4, 6, 8, 12, 24]
PATIENCES      = [24, 48, 72, 120, 240]

TOTAL_POINTS = len(SIGNAL_WINDOWS) * len(PATIENCES)


def run_one(signal_window, patience):
    return simulate_two_phase_dynamic(
        coins=COINS,
        max_concurrent=K,
        entry_threshold=ENTRY,
        signal_window=signal_window,
        base_min_hold=BASE_MIN_HOLD,
        safety_mult=SM,
        cap_min_hold=CAP_MH,
        phase1_negative_patience=patience,
        phase1_breakeven_cap_hours=P1_CAP,
        phase2_exit_threshold=P2_EXIT,
        fee_multiplier=FEE_MULT,
    )


def compute_row(signal_window, patience, pnl, cap, info, period, start_idx, end_idx):
    m = metrics_window(pnl, cap, info["opens_per_hour"], CAPITAL_BASE, start_idx, end_idx)
    return {
        "signal_window":             signal_window,
        "phase1_negative_patience":  patience,
        "period":                    period,
        "annual":                    m["annual"],
        "max_dd":                    m["max_dd"],
        "calmar":                    m["calmar"],
        "sharpe":                    m["sharpe"],
        "trades":                    m["trades"],
        "avg_hold_h":                info["avg_hold_hours"],
        "avg_min_hold":              info["avg_min_hold_assigned"],
        "tim_pct":                   m["time_in_market_pct"],
        "phase1_exits":              info["phase1_exits"],
        "phase2_exits":              info["phase2_exits"],
    }


def print_2d_table(results_full, metric, fmt):
    """Print a 2D table: rows=signal_window, cols=patience."""
    # results_full: list of dicts with signal_window, phase1_negative_patience, and metric
    lookup = {(r["signal_window"], r["phase1_negative_patience"]): r[metric] for r in results_full}

    # Header
    col_w = 10
    header = f"{'sw\\pat':>8}" + "".join(f"{p:>{col_w}}" for p in PATIENCES)
    print(header)
    print("-" * (8 + col_w * len(PATIENCES)))
    for sw in SIGNAL_WINDOWS:
        row = f"{sw:>8}"
        for p in PATIENCES:
            val = lookup.get((sw, p))
            if val is None:
                row += f"{'N/A':>{col_w}}"
            else:
                row += f"{val:{col_w}.{fmt}}"
        print(row)


def main():
    all_csv_rows = []
    # Store (signal_window, patience, pnl, cap, info) for post-processing
    run_results = []

    run_idx = 0
    for sw in SIGNAL_WINDOWS:
        for p in PATIENCES:
            run_idx += 1
            print(f"[{run_idx}/{TOTAL_POINTS}] sw={sw} patience={p} ...", flush=True)
            pnl, cap, info = run_one(sw, p)
            run_results.append((sw, p, pnl, cap, info))

    # Build CSV rows and full-period list
    results_full = []
    for sw, p, pnl, cap, info in run_results:
        n = len(pnl)
        for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
            row = compute_row(sw, p, pnl, cap, info, period, start_idx, n)
            all_csv_rows.append(row)
            if period == "full":
                results_full.append(row)

    # ── Print 2D tables ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("2D TABLE — annual% (full period)")
    print("rows = signal_window (hours), cols = phase1_negative_patience (hours)")
    print("=" * 72)
    print_2d_table(results_full, "annual", "2f")

    print()
    print("=" * 72)
    print("2D TABLE — Calmar (full period)")
    print("rows = signal_window (hours), cols = phase1_negative_patience (hours)")
    print("=" * 72)
    print_2d_table(results_full, "calmar", "1f")

    # ── Top-3 by annual ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("TOP-3 by annual% (full period)")
    print("=" * 72)
    sorted_annual = sorted(results_full, key=lambda r: r["annual"], reverse=True)
    for i, r in enumerate(sorted_annual[:3], 1):
        print(
            f"  #{i}: sw={r['signal_window']:>2}h  patience={r['phase1_negative_patience']:>3}h  "
            f"annual={r['annual']:.2f}%  calmar={r['calmar']:.1f}  max_dd={r['max_dd']:.2f}%"
        )

    # ── Top-3 by calmar ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("TOP-3 by Calmar (full period)")
    print("=" * 72)
    sorted_calmar = sorted(results_full, key=lambda r: r["calmar"], reverse=True)
    for i, r in enumerate(sorted_calmar[:3], 1):
        print(
            f"  #{i}: sw={r['signal_window']:>2}h  patience={r['phase1_negative_patience']:>3}h  "
            f"calmar={r['calmar']:.1f}  annual={r['annual']:.2f}%  max_dd={r['max_dd']:.2f}%"
        )

    # ── Baseline comparison (sw=12, patience=72) ─────────────────────────────
    baseline = next((r for r in results_full if r["signal_window"] == 12 and r["phase1_negative_patience"] == 72), None)
    best_annual = sorted_annual[0]
    best_calmar = sorted_calmar[0]
    print()
    print("=" * 72)
    print("BASELINE vs BEST (full period)")
    print("=" * 72)
    if baseline:
        print(
            f"  Baseline (sw=12, patience=72): annual={baseline['annual']:.2f}%  "
            f"calmar={baseline['calmar']:.1f}  max_dd={baseline['max_dd']:.2f}%"
        )
        print(
            f"  Best annual (sw={best_annual['signal_window']}, patience={best_annual['phase1_negative_patience']}): "
            f"annual={best_annual['annual']:.2f}%  "
            f"Δannual={best_annual['annual'] - baseline['annual']:+.2f}pp  "
            f"calmar={best_annual['calmar']:.1f}"
        )
        print(
            f"  Best calmar (sw={best_calmar['signal_window']}, patience={best_calmar['phase1_negative_patience']}): "
            f"calmar={best_calmar['calmar']:.1f}  "
            f"Δcalmar={best_calmar['calmar'] - baseline['calmar']:+.1f}  "
            f"annual={best_calmar['annual']:.2f}%"
        )
    else:
        print("  Baseline point (sw=12, patience=72) not found in results.")

    # ── Save CSV ──────────────────────────────────────────────────────────────
    out = Path(__file__).parent / "signal_window_patience_sweep_results.csv"
    fieldnames = [
        "signal_window", "phase1_negative_patience", "period",
        "annual", "max_dd", "calmar", "sharpe",
        "trades", "avg_hold_h", "avg_min_hold", "tim_pct",
        "phase1_exits", "phase2_exits",
    ]
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_csv_rows)
    print(f"\nСохранено: {out}  ({len(all_csv_rows)} строк)")


if __name__ == "__main__":
    main()
