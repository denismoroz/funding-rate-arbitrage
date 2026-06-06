"""
Short-term reversal / mean-reversion backtest.

Flavor A: Cross-sectional weekly reversal (1d bars).
  Each week rank coins by trailing-K-day return; long bottom-N, short top-N, equal weight,
  dollar-neutral. Hold for 1 week (weights forward-filled to daily).

Flavor B: Single-asset intraday z-score mean-reversion (1h bars) on BTC and ETH.
  z = (close - SMA_W(close)) / std_W(close).
  Long when z < -Z_THRESH, short when z > +Z_THRESH, flat when |z| < 0.5.

No look-ahead: signals computed at bar t, qutil shifts +1 bar before earning returns.
"""
import sys, json, itertools
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "ARB", "OP", "DOGE", "UNI", "INJ", "TIA"]

# =============================================================================
# FLAVOR A: Cross-sectional short-term reversal
# =============================================================================

def flavor_a_weights(px: pd.DataFrame, K: int, N: int) -> pd.DataFrame:
    """
    Each day, compute trailing-K-day return. On each Friday close (weekly rebalance):
      - rank all coins by that K-day return
      - long the bottom-N (biggest losers), short the top-N (biggest winners)
      - equal-weight, dollar-neutral (each leg sums to 1 in abs terms -> net 0)
    Weight decisions made at close[t], executed at open of [t+1] (qutil shifts).
    Between rebalance days, hold the last weekly weight (forward-fill).
    """
    # Trailing K-day return: ret[t] = px[t]/px[t-K] - 1 (uses only past data)
    trailing_ret = px.pct_change(K)

    # Identify rebalance dates: every Friday (weekday==4), or last bar of each week
    # We use ISO weekday: Monday=0, Friday=4. Resample weekly to get last trading day.
    rebal_dates = px.resample("W").last().index  # Sunday-anchored week end
    # These are the actual last trading days in each week
    rebal_dates_actual = [px.index[px.index <= d][-1] for d in rebal_dates if len(px.index[px.index <= d]) > 0]
    rebal_dates_actual = pd.DatetimeIndex(rebal_dates_actual).unique()

    # Build weight frame (same shape as px)
    W = pd.DataFrame(0.0, index=px.index, columns=px.columns)

    for dt in rebal_dates_actual:
        if dt not in trailing_ret.index:
            continue
        ret_row = trailing_ret.loc[dt]
        valid = ret_row.dropna()
        if len(valid) < 2 * N:
            continue
        sorted_coins = valid.sort_values()
        losers = sorted_coins.index[:N]   # worst K-day return -> BUY
        winners = sorted_coins.index[-N:] # best K-day return -> SELL

        w = pd.Series(0.0, index=px.columns)
        w[losers] = 1.0 / N    # equal long
        w[winners] = -1.0 / N  # equal short
        W.loc[dt] = w

    # Forward-fill weights between rebalance dates (hold position).
    # CORRECT approach: only set non-rebalance rows to NaN so ffill propagates the
    # FULL rebalance row (including intentional zeros for coins not in that week's portfolio).
    # DO NOT replace(0, NaN) globally — that corrupts the rebalance rows by treating the
    # intentional flat positions for out-of-portfolio coins as missing data, causing
    # ghost positions from prior weeks to accumulate.
    rebal_mask = W.index.isin(rebal_dates_actual)
    W_ffill = W.copy().astype(float)
    # Mark non-rebalance rows as NaN so ffill pulls from last rebalance
    W_ffill.loc[~rebal_mask] = np.nan
    W_ffill = W_ffill.ffill()
    W_ffill = W_ffill.fillna(0.0)

    return W_ffill


def run_flavor_a(K: int = 5, N: int = 3, cost_bps: float = q.DEFAULT_COST_BPS) -> dict:
    px = q.load_closes(UNIVERSE, "1d").dropna()
    # Need at least K+7 days of warmup
    W = flavor_a_weights(px, K, N)

    # Warmup: skip first K+7 days so trailing return is valid
    warmup = K + 7
    start_idx = px.index[warmup]

    bt = q.backtest_weights(px, W, cost_bps=cost_bps)
    bt_trim = bt[bt.index >= start_idx]

    m = q.metrics_from_returns(bt_trim["ret_net"], "1d")
    yearly = q.period_breakdown(bt_trim["ret_net"])

    # Avg weekly turnover
    dw = W.diff().abs().sum(axis=1)
    avg_weekly_turnover = dw[dw > 0].mean()

    return {
        "K": K, "N": N, "cost_bps": cost_bps,
        "metrics": m,
        "yearly": yearly.to_dict("records"),
        "avg_weekly_turnover": float(avg_weekly_turnover),
        "bt": bt_trim,
        "px": px,
        "W": W[W.index >= start_idx],
    }


# =============================================================================
# FLAVOR B: Single-asset intraday z-score mean-reversion
# =============================================================================

def flavor_b_weights(px_1h: pd.DataFrame, W_period: int, Z_thresh: float) -> pd.DataFrame:
    """
    For each coin series, compute z = (close - SMA_W) / std_W (trailing, causal).
    Position: +1 if z < -Z_thresh, -1 if z > +Z_thresh, 0 if |z| < 0.5.
    State-machine: once in a position, stay until |z| < 0.5 (exit band).
    """
    coins = px_1h.columns.tolist()
    W_out = pd.DataFrame(0.0, index=px_1h.index, columns=coins)

    for coin in coins:
        s = px_1h[coin]
        sma = s.rolling(W_period).mean()
        std = s.rolling(W_period).std(ddof=1)
        z = (s - sma) / std

        pos = np.zeros(len(z))
        cur = 0
        zv = z.values
        for i in range(len(zv)):
            if np.isnan(zv[i]):
                cur = 0; pos[i] = 0; continue
            if cur == 0:
                if zv[i] < -Z_thresh:
                    cur = 1    # long
                elif zv[i] > Z_thresh:
                    cur = -1   # short
            else:
                if abs(zv[i]) < 0.5:
                    cur = 0    # exit
            pos[i] = cur
        W_out[coin] = pos

    return W_out


def run_flavor_b(W_period: int = 48, Z_thresh: float = 2.0,
                 cost_bps: float = q.DEFAULT_COST_BPS,
                 coins: list | None = None) -> dict:
    if coins is None:
        coins = ["BTC", "ETH"]
    px = q.load_closes(coins, "1h").dropna()

    W = flavor_b_weights(px, W_period, Z_thresh)

    # Scale so each coin contributes equally (divide by n_coins)
    n = len(coins)
    W_scaled = W / n

    warmup = W_period + 1
    start_idx = px.index[warmup]

    bt = q.backtest_weights(px, W_scaled, cost_bps=cost_bps)
    bt_trim = bt[bt.index >= start_idx]

    m = q.metrics_from_returns(bt_trim["ret_net"], "1h")
    yearly = q.period_breakdown(bt_trim["ret_net"])

    # Turnover stats
    dw = W_scaled.diff().abs().sum(axis=1)
    avg_hourly_turnover = dw.mean()
    trades_per_day = float((dw > 0).mean() * 24)

    return {
        "W_period": W_period, "Z_thresh": Z_thresh, "cost_bps": cost_bps,
        "metrics": m,
        "yearly": yearly.to_dict("records"),
        "avg_hourly_turnover": float(avg_hourly_turnover),
        "trades_per_day_approx": trades_per_day,
        "bt": bt_trim,
        "px": px,
        "W": W_scaled[W_scaled.index >= start_idx],
    }


# =============================================================================
# MAIN: run grids, save outputs
# =============================================================================

def run():
    HERE.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("FLAVOR A: Cross-sectional short-term reversal (1d)")
    print("=" * 60)

    # --- Default run at 3 cost levels ---
    print("\nDefault params K=5, N=3:")
    results_a = {}
    for cbps in [0, 5, 10]:
        r = run_flavor_a(K=5, N=3, cost_bps=cbps)
        m = r["metrics"]
        print(f"  cost={cbps:2d}bps  CAGR={m['cagr']:+.1%}  Sharpe={m['sharpe']:+.2f}  MaxDD={m['max_drawdown']:.1%}  Calmar={m['calmar']:.2f}")
        results_a[cbps] = r

    # Save default (5bps) equity plot
    default_a = results_a[5]
    px_btc_daily = q.load_closes(["BTC"], "1d")["BTC"]
    q.equity_plot(
        default_a["bt"]["equity"] / default_a["bt"]["equity"].iloc[0],
        "Flavor A: Cross-sectional reversal (K=5, N=3, 5bps)",
        HERE / "equity_a.png",
        benchmark=px_btc_daily,
    )

    # --- Grid search ---
    print("\nFlavor A grid (K, N, cost_bps) -> CAGR | Sharpe | MaxDD:")
    grid_a = []
    for K in [3, 5, 7]:
        for N in [2, 3, 4]:
            for cbps in [0, 5, 10]:
                r = run_flavor_a(K=K, N=N, cost_bps=cbps)
                m = r["metrics"]
                row = {"K": K, "N": N, "cost_bps": cbps,
                       "cagr": m["cagr"], "sharpe": m["sharpe"],
                       "max_drawdown": m["max_drawdown"], "calmar": m["calmar"],
                       "avg_weekly_turnover": r["avg_weekly_turnover"]}
                grid_a.append(row)
                print(f"  K={K} N={N} cost={cbps:2d}bps  CAGR={m['cagr']:+.1%}  Sharpe={m['sharpe']:+.2f}  MaxDD={m['max_drawdown']:.1%}")

    grid_a_df = pd.DataFrame(grid_a)
    # Best at 5bps
    best_a = grid_a_df[grid_a_df["cost_bps"] == 5].sort_values("sharpe", ascending=False).iloc[0]
    print(f"\nBest Flavor A at 5bps: K={best_a['K']:.0f} N={best_a['N']:.0f}  Sharpe={best_a['sharpe']:.2f}  CAGR={best_a['cagr']:.1%}")

    print("\n" + "=" * 60)
    print("FLAVOR B: Intraday z-score mean-reversion (1h, BTC+ETH)")
    print("=" * 60)

    # --- Default run at 3 cost levels ---
    print("\nDefault params W=48, Z=2.0:")
    results_b = {}
    for cbps in [0, 5, 10]:
        r = run_flavor_b(W_period=48, Z_thresh=2.0, cost_bps=cbps)
        m = r["metrics"]
        print(f"  cost={cbps:2d}bps  CAGR={m['cagr']:+.1%}  Sharpe={m['sharpe']:+.2f}  MaxDD={m['max_drawdown']:.1%}  trades/day~{r['trades_per_day_approx']:.1f}")
        results_b[cbps] = r

    # Save default (5bps) equity plot
    default_b = results_b[5]
    px_btc_1h = q.load_closes(["BTC"], "1h")["BTC"]
    q.equity_plot(
        default_b["bt"]["equity"] / default_b["bt"]["equity"].iloc[0],
        "Flavor B: z-score mean-reversion (W=48, Z=2.0, 5bps)",
        HERE / "equity_b.png",
        benchmark=px_btc_1h,
    )

    # --- Grid search ---
    print("\nFlavor B grid (W, Z, cost_bps) -> CAGR | Sharpe | MaxDD:")
    grid_b = []
    for W_period in [24, 48, 72]:
        for Z_thresh in [1.5, 2.0, 2.5]:
            for cbps in [0, 5, 10]:
                r = run_flavor_b(W_period=W_period, Z_thresh=Z_thresh, cost_bps=cbps)
                m = r["metrics"]
                row = {"W": W_period, "Z": Z_thresh, "cost_bps": cbps,
                       "cagr": m["cagr"], "sharpe": m["sharpe"],
                       "max_drawdown": m["max_drawdown"], "calmar": m["calmar"],
                       "trades_per_day": r["trades_per_day_approx"],
                       "avg_hourly_turnover": r["avg_hourly_turnover"]}
                grid_b.append(row)
                print(f"  W={W_period:2d} Z={Z_thresh:.1f} cost={cbps:2d}bps  CAGR={m['cagr']:+.1%}  Sharpe={m['sharpe']:+.2f}  MaxDD={m['max_drawdown']:.1%}  trades/day~{r['trades_per_day_approx']:.1f}")

    grid_b_df = pd.DataFrame(grid_b)
    best_b = grid_b_df[grid_b_df["cost_bps"] == 5].sort_values("sharpe", ascending=False).iloc[0]
    print(f"\nBest Flavor B at 5bps: W={best_b['W']:.0f} Z={best_b['Z']:.1f}  Sharpe={best_b['sharpe']:.2f}  CAGR={best_b['cagr']:.1%}")

    # ==========================================================================
    # BUY & HOLD BTC BENCHMARK
    # ==========================================================================
    print("\n" + "=" * 60)
    print("BENCHMARK: Buy & Hold BTC (daily)")
    print("=" * 60)
    px_btc = q.load_closes(["BTC"], "1d")["BTC"].dropna()
    btc_ret = px_btc.pct_change().dropna()
    btc_m = q.metrics_from_returns(btc_ret, "1d")
    print(f"  CAGR={btc_m['cagr']:+.1%}  Sharpe={btc_m['sharpe']:+.2f}  MaxDD={btc_m['max_drawdown']:.1%}")

    # Correlation of Flavor A with BTC
    a_ret = default_a["bt"]["ret_net"]
    b_ret_daily = default_b["bt"]["ret_net"].resample("1D").sum()
    common_a = a_ret.reindex(btc_ret.index).dropna()
    common_btc_a = btc_ret.reindex(common_a.index).dropna()
    corr_a_btc = common_a.corr(common_btc_a)
    print(f"\nCorrelation Flavor A vs BTC daily ret: {corr_a_btc:.3f}")

    b_daily_aligned = b_ret_daily.reindex(btc_ret.index).dropna()
    btc_for_b = btc_ret.reindex(b_daily_aligned.index).dropna()
    b_aligned = b_daily_aligned.reindex(btc_for_b.index).dropna()
    corr_b_btc = b_aligned.corr(btc_for_b)
    print(f"Correlation Flavor B (daily agg) vs BTC daily ret: {corr_b_btc:.3f}")

    # ==========================================================================
    # YEARLY BREAKDOWNS for defaults
    # ==========================================================================
    print("\n--- Flavor A (K=5, N=3, 5bps) yearly breakdown ---")
    yearly_a = pd.DataFrame(results_a[5]["yearly"])
    print(yearly_a.to_string(index=False))

    print("\n--- Flavor B (W=48, Z=2.0, 5bps) yearly breakdown ---")
    yearly_b = pd.DataFrame(results_b[5]["yearly"])
    print(yearly_b.to_string(index=False))

    # ==========================================================================
    # SAVE OUTPUTS
    # ==========================================================================

    # results.csv: daily equity for both flavors at all cost levels
    results_rows = []
    for cbps, r in results_a.items():
        tmp = r["bt"][["ret_gross", "cost", "ret_net", "equity"]].copy()
        tmp.columns = [f"A_{c}" for c in tmp.columns]
        tmp["cost_bps"] = cbps
        tmp["flavor"] = "A"
        results_rows.append(tmp)
    results_df_a = pd.concat([r["bt"].assign(cost_bps=cbps, flavor="A") for cbps, r in results_a.items()])
    results_df_b = pd.concat([r["bt"].assign(cost_bps=cbps, flavor="B") for cbps, r in results_b.items()])
    pd.concat([results_df_a, results_df_b]).to_csv(HERE / "results.csv")

    # trades.csv: Flavor B trades (entry/exit events) for BTC
    print("\nBuilding trades.csv for Flavor B (BTC)...")
    px_btc_1h_full = q.load_closes(["BTC"], "1h")["BTC"].dropna()
    W_b = flavor_b_weights(px_btc_1h_full.to_frame("BTC"), 48, 2.0)["BTC"]
    # Find transitions
    pos_change = W_b.diff().fillna(W_b)
    entries = pos_change[pos_change != 0].copy()
    trade_list = []
    for ts, delta in entries.items():
        if delta != 0:
            trade_list.append({"time": ts, "coin": "BTC", "position_delta": delta,
                                "new_position": W_b.loc[ts]})
    trades_df = pd.DataFrame(trade_list)
    if len(trades_df) > 0:
        trades_df.to_csv(HERE / "trades.csv", index=False)
        print(f"  Saved {len(trades_df)} trade events")

    # Flavor A trades.csv supplementary
    W_a = flavor_a_weights(q.load_closes(UNIVERSE, "1d").dropna(), 5, 3)
    trades_a = []
    rebal_dates = W_a.index[W_a.abs().sum(axis=1) > 0]
    for dt in rebal_dates:
        w_row = W_a.loc[dt]
        longs = w_row[w_row > 0].index.tolist()
        shorts = w_row[w_row < 0].index.tolist()
        trades_a.append({"date": dt, "longs": str(longs), "shorts": str(shorts)})
    pd.DataFrame(trades_a).to_csv(HERE / "trades_a.csv", index=False)
    print(f"  Saved {len(trades_a)} weekly rebalances for Flavor A")

    # metrics.json
    out_metrics = {
        "strategy": "crypto_reversal_meanrev",
        "flavor_A_default": {
            "params": {"K": 5, "N": 3, "universe": UNIVERSE},
            "cost_0bps": results_a[0]["metrics"],
            "cost_5bps": results_a[5]["metrics"],
            "cost_10bps": results_a[10]["metrics"],
            "yearly_5bps": results_a[5]["yearly"],
            "corr_btc": float(corr_a_btc),
        },
        "flavor_B_default": {
            "params": {"W": 48, "Z": 2.0, "coins": ["BTC", "ETH"]},
            "cost_0bps": results_b[0]["metrics"],
            "cost_5bps": results_b[5]["metrics"],
            "cost_10bps": results_b[10]["metrics"],
            "yearly_5bps": results_b[5]["yearly"],
            "corr_btc_daily_agg": float(corr_b_btc),
        },
        "benchmark_btc_buyhold": btc_m,
        "grid_A": grid_a,
        "grid_B": grid_b,
        "best_A_at_5bps": best_a.to_dict(),
        "best_B_at_5bps": best_b.to_dict(),
    }
    q.save_metrics(HERE / "metrics.json", out_metrics)
    print("\nSaved metrics.json")

    print("\nDone. Files saved to:", HERE)


if __name__ == "__main__":
    run()
