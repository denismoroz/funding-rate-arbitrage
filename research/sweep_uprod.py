"""One-shot sweep for U-prod = {BTC, ETH, SOL, HYPE, PURR}, Config B only.

Appends 8 aggregate rows and per-coin rows to the validated CSVs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from portfolio_margin import simulate_portfolio, BUDGET_CAP_USD

UNIVERSE = {
    "coins": ["BTC", "ETH", "SOL", "HYPE", "PURR"],
    "leverage": {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "PURR": 3},
}
UNIV_NAME = "U-prod"

CFG_NAME = "B_live"
CFG_PARAMS = {"entry_threshold": 0.10, "exit_threshold": -0.10, "min_hold_hours": 120}

MARGIN_BUFFER_X_VALS = [3.0, 5.0]
POSITION_SIZE_VALS   = [100.0, 150.0]
CONCURRENCY_CAP_VALS = [3, 5]

AGG_METRICS = [
    'annual_pct', 'vol_pct', 'sharpe', 'sortino', 'max_dd_pct', 'calmar',
    'n_liquidations', 'n_top_ups', 'n_forced_closes', 'n_skipped_opens_capital',
    'total_funding', 'total_fees', 'final_equity',
]

AGG_PATH = Path(__file__).parent / "sweep_aggregate_2026_06_validated.csv"
PC_PATH  = Path(__file__).parent / "sweep_per_coin_2026_06_validated.csv"


def run() -> tuple[pd.DataFrame, pd.DataFrame]:
    agg_rows = []
    per_coin_rows = []
    coins = UNIVERSE["coins"]
    leverage_map = UNIVERSE["leverage"]

    total = len(MARGIN_BUFFER_X_VALS) * len(POSITION_SIZE_VALS) * len(CONCURRENCY_CAP_VALS)
    run_idx = 0

    for mb in MARGIN_BUFFER_X_VALS:
        for ps in POSITION_SIZE_VALS:
            for k in CONCURRENCY_CAP_VALS:
                run_idx += 1
                label = f"{UNIV_NAME} buf={mb} sz={ps} K={k}"
                print(f"[{run_idx}/{total}] {label} ...", end="", flush=True)

                try:
                    m = simulate_portfolio(
                        coins=coins,
                        leverage_map=leverage_map,
                        margin_buffer_x=mb,
                        position_size=ps,
                        concurrency_cap=k,
                        budget_cap_usd=BUDGET_CAP_USD,
                        **CFG_PARAMS,
                    )

                    base = {
                        "config": CFG_NAME,
                        "universe": UNIV_NAME,
                        "margin_buffer_x": mb,
                        "position_size": ps,
                        "concurrency_cap": k,
                        "entry_threshold": CFG_PARAMS["entry_threshold"],
                        "exit_threshold": CFG_PARAMS["exit_threshold"],
                        "min_hold_hours": CFG_PARAMS["min_hold_hours"],
                    }

                    agg_row = dict(base)
                    for key in AGG_METRICS:
                        agg_row[key] = m.get(key, float("nan"))
                    agg_rows.append(agg_row)

                    for coin, pc in m.get("per_coin", {}).items():
                        pc_row = dict(base)
                        pc_row["coin"] = coin
                        pc_row.update(pc)
                        h = pc["hours_in_position"]
                        net = pc["funding_gross"] - pc["fees_paid"] + pc["realized_pnl"]
                        pc_row["net_pnl_per_hour"] = net / h if h > 0 else 0.0
                        per_coin_rows.append(pc_row)

                    print(f" OK  annual={m['annual_pct']:+.1f}% sharpe={m['sharpe']:.3f} maxdd={m['max_dd_pct']:.2f}% liq={m['n_liquidations']}", flush=True)

                except Exception as exc:
                    print(f" ERROR: {exc}", flush=True)
                    import traceback; traceback.print_exc()

    return pd.DataFrame(agg_rows), pd.DataFrame(per_coin_rows)


if __name__ == "__main__":
    agg_new, pc_new = run()

    # Append to existing CSVs
    agg_existing = pd.read_csv(AGG_PATH)
    pc_existing  = pd.read_csv(PC_PATH)

    agg_combined = pd.concat([agg_existing, agg_new], ignore_index=True)
    pc_combined  = pd.concat([pc_existing, pc_new], ignore_index=True)

    agg_combined.to_csv(AGG_PATH, index=False)
    pc_combined.to_csv(PC_PATH, index=False)

    print(f"\nAppended {len(agg_new)} aggregate rows -> {AGG_PATH}")
    print(f"Appended {len(pc_new)} per-coin rows  -> {PC_PATH}")

    # Summary: K=3 rows
    k3 = agg_new[agg_new["concurrency_cap"] == 3].copy()
    print("\n--- U-prod K=3 summary ---")
    print(k3[["margin_buffer_x","position_size","concurrency_cap",
               "annual_pct","sharpe","max_dd_pct","n_liquidations"]].to_string(index=False))

    # Per-coin for buf=3 sz=100 K=3 (actual prod config)
    prod_row = agg_new[
        (agg_new["margin_buffer_x"] == 3.0) &
        (agg_new["position_size"] == 100.0) &
        (agg_new["concurrency_cap"] == 3)
    ]
    if not prod_row.empty:
        prod_pc = pc_new[
            (pc_new["margin_buffer_x"] == 3.0) &
            (pc_new["position_size"] == 100.0) &
            (pc_new["concurrency_cap"] == 3)
        ]
        print("\n--- Per-coin attribution: U-prod buf=3 sz=$100 K=3 ---")
        print(prod_pc[["coin","n_opens","funding_gross","fees_paid","realized_pnl","net_pnl_per_hour"]].to_string(index=False))
