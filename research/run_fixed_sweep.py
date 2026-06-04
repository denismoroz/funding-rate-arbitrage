"""Re-run the full 2026-06 sweep after fixing the double-counted req_margin bug.

Same grid as sweep_aggregate_2026_06_validated (portfolio_margin_sweep_expanded + sweep_uprod):
  Universes    : U3-new, U4, U5, U7, U-prod
  Configs      : A_research (entry=0.30, exit=-0.15), B_live (entry=0.10, exit=-0.10)
  margin_buffer_x  : {3, 5}
  position_size    : {100, 150}
  concurrency_cap  : {3, 4, 5}   <- K=4 is actual live prod

Outputs (NEW files, do not overwrite the buggy ones):
  research/sweep_aggregate_2026_06_FIXED.csv
  research/sweep_per_coin_2026_06_FIXED.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from portfolio_margin import simulate_portfolio, BUDGET_CAP_USD

UNIVERSES = {
    "U3-new": {
        "coins": ["BTC", "ETH", "SOL"],
        "leverage": {"BTC": 40, "ETH": 25, "SOL": 20},
    },
    "U4": {
        "coins": ["BTC", "ETH", "SOL", "HYPE"],
        "leverage": {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10},
    },
    "U5": {
        "coins": ["BTC", "ETH", "SOL", "HYPE", "ZEC"],
        "leverage": {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "ZEC": 10},
    },
    "U7": {
        "coins": ["BTC", "ETH", "SOL", "HYPE", "ZEC", "PURR", "XPL"],
        "leverage": {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "ZEC": 10, "PURR": 3, "XPL": 10},
    },
    "U-prod": {
        "coins": ["BTC", "ETH", "SOL", "HYPE", "PURR"],
        "leverage": {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "PURR": 3},
    },
}

CONFIGS = {
    "A_research": {"entry_threshold": 0.30, "exit_threshold": -0.15, "min_hold_hours": 120},
    "B_live":     {"entry_threshold": 0.10, "exit_threshold": -0.10, "min_hold_hours": 120},
}

MARGIN_BUFFER_X_VALS = [3.0, 5.0]
POSITION_SIZE_VALS   = [100.0, 150.0]
CONCURRENCY_CAP_VALS = [3, 4, 5]

AGG_METRICS = [
    "annual_pct", "vol_pct", "sharpe", "sortino", "max_dd_pct", "calmar",
    "n_liquidations", "n_top_ups", "n_forced_closes", "n_skipped_opens_capital",
    "total_funding", "total_fees", "final_equity",
]

AGG_PATH = Path(__file__).parent / "sweep_aggregate_2026_06_FIXED.csv"
PC_PATH  = Path(__file__).parent / "sweep_per_coin_2026_06_FIXED.csv"


def run_sweep():
    agg_rows = []
    per_coin_rows = []

    total_runs = (
        len(CONFIGS) * len(UNIVERSES)
        * len(MARGIN_BUFFER_X_VALS) * len(POSITION_SIZE_VALS) * len(CONCURRENCY_CAP_VALS)
    )
    run_idx = 0

    for cfg_name, cfg_params in CONFIGS.items():
        for univ_name, univ in UNIVERSES.items():
            coins = univ["coins"]
            leverage_map = univ["leverage"]

            for mb in MARGIN_BUFFER_X_VALS:
                for ps in POSITION_SIZE_VALS:
                    for k in CONCURRENCY_CAP_VALS:
                        run_idx += 1
                        label = f"cfg={cfg_name} {univ_name} buf={mb} sz={ps} K={k}"
                        print(f"[{run_idx}/{total_runs}] {label} ...", end="", flush=True)

                        try:
                            m = simulate_portfolio(
                                coins=coins,
                                leverage_map=leverage_map,
                                margin_buffer_x=mb,
                                position_size=ps,
                                concurrency_cap=k,
                                budget_cap_usd=BUDGET_CAP_USD,
                                **cfg_params,
                            )

                            base = {
                                "config": cfg_name,
                                "universe": univ_name,
                                "margin_buffer_x": mb,
                                "position_size": ps,
                                "concurrency_cap": k,
                                "entry_threshold": cfg_params["entry_threshold"],
                                "exit_threshold": cfg_params["exit_threshold"],
                                "min_hold_hours": cfg_params["min_hold_hours"],
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

                            print(
                                f" OK  annual={m['annual_pct']:+.2f}%"
                                f" sharpe={m['sharpe']:.3f}"
                                f" maxdd={m['max_dd_pct']:.2f}%"
                                f" liq={m['n_liquidations']}",
                                flush=True,
                            )

                        except Exception as exc:
                            import traceback
                            print(f" ERROR: {exc}", flush=True)
                            traceback.print_exc()
                            agg_rows.append({
                                "config": cfg_name, "universe": univ_name,
                                "margin_buffer_x": mb, "position_size": ps,
                                "concurrency_cap": k, "error": str(exc),
                            })

    return pd.DataFrame(agg_rows), pd.DataFrame(per_coin_rows)


if __name__ == "__main__":
    agg_df, pc_df = run_sweep()

    agg_df.to_csv(AGG_PATH, index=False)
    pc_df.to_csv(PC_PATH, index=False)

    print(f"\nAggregate results -> {AGG_PATH}")
    print(f"Per-coin results  -> {PC_PATH}")

    # Quick summary: top 5 by Sharpe per config
    for cfg in CONFIGS:
        subset = agg_df[agg_df["config"] == cfg].copy()
        if subset.empty or "sharpe" not in subset.columns:
            continue
        top = subset.nlargest(5, "sharpe")[
            ["config", "universe", "margin_buffer_x", "position_size", "concurrency_cap",
             "annual_pct", "sharpe", "max_dd_pct", "n_liquidations"]
        ]
        print(f"\nTop 5 by Sharpe — {cfg}:")
        print(top.to_string(index=False))
