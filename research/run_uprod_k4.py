"""Single backtest: U-prod, Config B_live, K=4, buf=3, pos_size=$100.
Appends one aggregate row and per-coin rows to the validated CSVs.
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

MB = 3.0
PS = 100.0
K = 4

AGG_METRICS = [
    'annual_pct', 'vol_pct', 'sharpe', 'sortino', 'max_dd_pct', 'calmar',
    'n_liquidations', 'n_top_ups', 'n_forced_closes', 'n_skipped_opens_capital',
    'total_funding', 'total_fees', 'final_equity',
]

AGG_PATH = Path(__file__).parent / "sweep_aggregate_2026_06_validated.csv"
PC_PATH  = Path(__file__).parent / "sweep_per_coin_2026_06_validated.csv"


def run():
    print(f"Running U-prod B_live K={K} buf={MB} pos_size=${PS} ...", flush=True)
    m = simulate_portfolio(
        coins=UNIVERSE["coins"],
        leverage_map=UNIVERSE["leverage"],
        margin_buffer_x=MB,
        position_size=PS,
        concurrency_cap=K,
        budget_cap_usd=BUDGET_CAP_USD,
        **CFG_PARAMS,
    )
    print(f"  annual={m['annual_pct']:+.4f}%  sharpe={m['sharpe']:.4f}  maxdd={m['max_dd_pct']:.4f}%  liq={m['n_liquidations']}", flush=True)

    base = {
        "config": CFG_NAME,
        "universe": UNIV_NAME,
        "margin_buffer_x": MB,
        "position_size": PS,
        "concurrency_cap": K,
        "entry_threshold": CFG_PARAMS["entry_threshold"],
        "exit_threshold": CFG_PARAMS["exit_threshold"],
        "min_hold_hours": CFG_PARAMS["min_hold_hours"],
    }

    agg_row = dict(base)
    for key in AGG_METRICS:
        agg_row[key] = m.get(key, float("nan"))

    per_coin_rows = []
    for coin, pc in m.get("per_coin", {}).items():
        pc_row = dict(base)
        pc_row["coin"] = coin
        pc_row.update(pc)
        h = pc["hours_in_position"]
        net = pc["funding_gross"] - pc["fees_paid"] + pc["realized_pnl"]
        pc_row["net_pnl_per_hour"] = net / h if h > 0 else 0.0
        per_coin_rows.append(pc_row)

    return pd.DataFrame([agg_row]), pd.DataFrame(per_coin_rows)


if __name__ == "__main__":
    agg_new, pc_new = run()

    # Append to existing CSVs
    agg_existing = pd.read_csv(AGG_PATH)
    pc_existing  = pd.read_csv(PC_PATH)

    agg_combined = pd.concat([agg_existing, agg_new], ignore_index=True)
    pc_combined  = pd.concat([pc_existing, pc_new], ignore_index=True)

    agg_combined.to_csv(AGG_PATH, index=False)
    pc_combined.to_csv(PC_PATH, index=False)

    print(f"\nAppended 1 aggregate row  -> {AGG_PATH}")
    print(f"Appended {len(pc_new)} per-coin rows -> {PC_PATH}")

    print("\n--- Aggregate: U-prod B_live buf=3 sz=$100 K=4 ---")
    print(agg_new[["config","universe","concurrency_cap","annual_pct","sharpe","max_dd_pct",
                    "n_liquidations","total_funding","total_fees"]].to_string(index=False))

    print("\n--- Per-coin attribution: U-prod B_live buf=3 sz=$100 K=4 ---")
    print(pc_new[["coin","n_opens","funding_gross","fees_paid","realized_pnl",
                   "hours_in_position","net_pnl_per_hour"]].to_string(index=False))

    # Compare K=3 vs K=4 on same universe
    k3_agg = agg_existing[
        (agg_existing["config"] == "B_live") &
        (agg_existing["universe"] == "U-prod") &
        (agg_existing["margin_buffer_x"] == 3.0) &
        (agg_existing["position_size"] == 100.0) &
        (agg_existing["concurrency_cap"] == 3)
    ]
    print("\n--- K=3 reference (same config) ---")
    if not k3_agg.empty:
        print(k3_agg[["concurrency_cap","annual_pct","sharpe","max_dd_pct",
                        "n_liquidations","total_funding","total_fees"]].to_string(index=False))

    print("\n--- Per-coin K=3 reference ---")
    k3_pc = pc_existing[
        (pc_existing["config"] == "B_live") &
        (pc_existing["universe"] == "U-prod") &
        (pc_existing["margin_buffer_x"] == 3.0) &
        (pc_existing["position_size"] == 100.0) &
        (pc_existing["concurrency_cap"] == 3)
    ]
    if not k3_pc.empty:
        print(k3_pc[["coin","n_opens","funding_gross","fees_paid","realized_pnl",
                       "hours_in_position","net_pnl_per_hour"]].to_string(index=False))
