"""
Stability sweep для two_phase_dynamic — Кандидат C.

Проверяет чувствительность к cap_min_hold и fee_multiplier (основные),
а также к safety_mult, p1_breakeven_cap_h, p1_negative_patience, p2_exit_threshold.

Каждый sweep варьирует ОДНУ переменную; остальные = chosen values.
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

# ── Chosen config (Candidate C) ───────────────────────────────────────────────
COINS   = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K       = 3
SIGNAL_WINDOW  = 12
BASE_MIN_HOLD  = 24
CAPITAL_BASE   = K * TOTAL_CAPITAL  # $6000

# Chosen parameter values
ENTRY    = 0.10
SM       = 5.0
CAP_MH   = 720
P1_NEG   = 72
P1_CAP   = 720
P2_EXIT  = -0.10
FEE_MULT = 1.0

# ── Sweep definitions ─────────────────────────────────────────────────────────
# Each sweep: (label, param_key, values_list, description_of_others)
SWEEPS = [
    (
        "cap_min_hold",
        "cap_min_hold",
        [240, 480, 720, 1080, 1440, 2160, 4320],
        f"entry={ENTRY} sm={SM} p1_neg={P1_NEG} p1_cap={P1_CAP} p2={P2_EXIT} fee_mult={FEE_MULT}",
    ),
    (
        "fee_multiplier",
        "fee_multiplier",
        [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0],
        f"entry={ENTRY} sm={SM} cap_mh={CAP_MH} p1_neg={P1_NEG} p1_cap={P1_CAP} p2={P2_EXIT}",
    ),
    (
        "safety_mult",
        "safety_mult",
        [2.0, 3.0, 5.0, 7.0, 10.0, 15.0],
        f"entry={ENTRY} cap_mh={CAP_MH} p1_neg={P1_NEG} p1_cap={P1_CAP} p2={P2_EXIT} fee_mult={FEE_MULT}",
    ),
    (
        "p1_breakeven_cap_h",
        "phase1_breakeven_cap_hours",
        [240, 480, 720, 1200, 2160],
        f"entry={ENTRY} sm={SM} cap_mh={CAP_MH} p1_neg={P1_NEG} p2={P2_EXIT} fee_mult={FEE_MULT}",
    ),
    (
        "p1_negative_patience",
        "phase1_negative_patience",
        [24, 48, 72, 120, 240, 720],
        f"entry={ENTRY} sm={SM} cap_mh={CAP_MH} p1_cap={P1_CAP} p2={P2_EXIT} fee_mult={FEE_MULT}",
    ),
    (
        "p2_exit_threshold",
        "phase2_exit_threshold",
        [-0.05, -0.10, -0.20, -0.30],
        f"entry={ENTRY} sm={SM} cap_mh={CAP_MH} p1_neg={P1_NEG} p1_cap={P1_CAP} fee_mult={FEE_MULT}",
    ),
]


def run_one(cap_min_hold, fee_multiplier, safety_mult,
            phase1_breakeven_cap_hours, phase1_negative_patience, phase2_exit_threshold):
    """Run a single simulation and return (pnl, cap, info)."""
    return simulate_two_phase_dynamic(
        coins=COINS,
        max_concurrent=K,
        entry_threshold=ENTRY,
        signal_window=SIGNAL_WINDOW,
        base_min_hold=BASE_MIN_HOLD,
        safety_mult=safety_mult,
        cap_min_hold=cap_min_hold,
        phase1_negative_patience=phase1_negative_patience,
        phase1_breakeven_cap_hours=phase1_breakeven_cap_hours,
        phase2_exit_threshold=phase2_exit_threshold,
        fee_multiplier=fee_multiplier,
    )


def compute_row(sweep_label, param_value, pnl, cap, info, period, start_idx, end_idx):
    m = metrics_window(pnl, cap, info["opens_per_hour"], CAPITAL_BASE, start_idx, end_idx)
    return {
        "sweep_label":    sweep_label,
        "param_value":    param_value,
        "period":         period,
        "annual":         m["annual"],
        "max_dd":         m["max_dd"],
        "calmar":         m["calmar"],
        "sharpe":         m["sharpe"],
        "trades":         m["trades"],
        "avg_hold_h":     info["avg_hold_hours"],
        "avg_min_hold":   info["avg_min_hold_assigned"],
        "tim_pct":        m["time_in_market_pct"],
        "phase1_exits":   info["phase1_exits"],
        "phase2_exits":   info["phase2_exits"],
    }


def print_sweep_table(sweep_label, others_desc, rows):
    """Print a formatted table for one sweep."""
    print()
    print("=" * 72)
    print(f"SWEEP: {sweep_label} (others = {others_desc})")
    print("=" * 72)
    header = (
        f"{'param_value':>12} {'period':<9} {'annual':>7} {'max_dd':>7} "
        f"{'calmar':>8} {'sharpe':>7} {'trades':>7} {'avg_hold':>9} "
        f"{'avg_mh':>8} {'tim%':>6} {'p1_ex':>6} {'p2_ex':>6}"
    )
    print(header)
    print("-" * 72)
    for r in rows:
        label = str(r["param_value"])
        if r.get("is_chosen"):
            label += "*"
        print(
            f"{label:>12} {r['period']:<9} {r['annual']:>7.2f} {r['max_dd']:>7.3f} "
            f"{r['calmar']:>8.1f} {r['sharpe']:>7.2f} {r['trades']:>7} {r['avg_hold_h']:>9.1f} "
            f"{r['avg_min_hold']:>8.1f} {r['tim_pct']:>6.1f} {r['phase1_exits']:>6} {r['phase2_exits']:>6}"
        )


def main():
    all_csv_rows = []
    all_table_data = []  # list of (sweep_label, others_desc, rows)

    # Chosen point values for comparison
    chosen_vals = {
        "cap_min_hold":               CAP_MH,
        "fee_multiplier":             FEE_MULT,
        "safety_mult":                SM,
        "phase1_breakeven_cap_hours": P1_CAP,
        "phase1_negative_patience":   P1_NEG,
        "phase2_exit_threshold":      P2_EXIT,
    }

    total_runs = sum(len(s[2]) for s in SWEEPS)
    run_idx = 0

    for sweep_label, param_key, values, others_desc in SWEEPS:
        table_rows = []

        for val in values:
            run_idx += 1
            # Build kwargs: start from chosen, override the one being swept
            kwargs = dict(
                cap_min_hold=CAP_MH,
                fee_multiplier=FEE_MULT,
                safety_mult=SM,
                phase1_breakeven_cap_hours=P1_CAP,
                phase1_negative_patience=P1_NEG,
                phase2_exit_threshold=P2_EXIT,
            )
            kwargs[param_key] = val

            print(
                f"[{run_idx}/{total_runs}] sweep={sweep_label} {param_key}={val} ...",
                flush=True,
            )

            pnl, cap, info = run_one(**kwargs)
            n = len(pnl)
            is_chosen = (val == chosen_vals[param_key])

            for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
                row = compute_row(sweep_label, val, pnl, cap, info, period, start_idx, n)
                row["is_chosen"] = is_chosen
                table_rows.append(row)
                all_csv_rows.append(row)

        all_table_data.append((sweep_label, others_desc, table_rows))

    # ── Print all tables ──────────────────────────────────────────────────────
    for sweep_label, others_desc, rows in all_table_data:
        print_sweep_table(sweep_label, others_desc, rows)

    # ── Print chosen point summary ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("CHOSEN CONFIG (C): entry=0.10 sm=5 cap_mh=720 p1_neg=72 p1_cap=720 p2=-0.10 fee_mult=1.0")
    print("(marked with * in each sweep where it is one of the points)")
    print("=" * 72)

    # ── Save CSV ──────────────────────────────────────────────────────────────
    out = Path(__file__).parent / "two_phase_dynamic_stability_results.csv"
    fieldnames = [
        "sweep_label", "param_value", "period",
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
