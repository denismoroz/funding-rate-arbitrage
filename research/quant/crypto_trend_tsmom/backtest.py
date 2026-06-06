"""
Crypto Time-Series Momentum / Trend-Following backtest.

Universe: BTC, ETH, SOL  (2023-06-01..2026-06-01, daily)

Two signal families
-------------------
(A) MA crossover: long when SMA(fast) > SMA(slow), else flat.
    Grid: fast/slow in {10/50, 20/100, 50/200}.
(B) TSMOM: long when trailing-K-day return > 0, else flat.
    Grid: K in {30, 60, 90}.

Default: 50/200 SMA crossover, long/flat, vol-targeted (target_vol=0.40,
         realized_vol=trailing 30d std annualized, weight capped at 1.5).
         Applied as equal-weight basket of BTC+ETH+SOL.

Vol-targeting is applied only for the DEFAULT config output.
Sensitivity grid is run WITHOUT vol-targeting for clarity.

No look-ahead: weights decided at bar t execute on bar t+1 (qutil shifts internally).
"""
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import qutil as q

TF = "1d"
COINS = ["BTC", "ETH", "SOL"]
START = "2023-06-01"
END = "2026-06-01"

# Vol-targeting parameters
TARGET_VOL = 0.40
VOL_WINDOW = 30           # trailing days for realised vol
VOL_CAP = 1.5             # max scaled weight

COST_BPS_DEFAULT = 5.0
COST_BPS_HIGH = 10.0

ANN = q.BARS_PER_YEAR[TF]


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def sma_signal(px: pd.Series, fast: int, slow: int) -> pd.Series:
    """Long (1) when SMA(fast)>SMA(slow), flat (0) otherwise. No look-ahead."""
    s_fast = px.rolling(fast).mean()
    s_slow = px.rolling(slow).mean()
    return (s_fast > s_slow).astype(float)


def tsmom_signal(px: pd.Series, k: int) -> pd.Series:
    """Long (1) when trailing k-day return > 0, flat (0) otherwise."""
    ret_k = px / px.shift(k) - 1.0
    return (ret_k > 0).astype(float)


def vol_scale(px: pd.Series, raw_signal: pd.Series,
              vol_window: int = VOL_WINDOW,
              target_vol: float = TARGET_VOL,
              cap: float = VOL_CAP) -> pd.Series:
    """Scale raw_signal (0 or 1) by target_vol / realized_vol, cap at `cap`."""
    daily_ret = px.pct_change()
    rv = daily_ret.rolling(vol_window).std() * np.sqrt(ANN)
    scale = (target_vol / rv).clip(upper=cap)
    return (raw_signal * scale).fillna(0.0)


# ---------------------------------------------------------------------------
# Build weight panel for a basket given a weight-per-coin function
# ---------------------------------------------------------------------------

def basket_weights(px: pd.DataFrame, weight_fn) -> pd.DataFrame:
    """Apply weight_fn to each coin, return equal-weight combined panel."""
    W = pd.DataFrame(index=px.index, columns=COINS, dtype=float)
    for c in COINS:
        W[c] = weight_fn(px[c])
    # equal weight: divide each coin's weight by N so total notional = 1
    return W / len(COINS)


def basket_ls_weights(px: pd.DataFrame, weight_fn) -> pd.DataFrame:
    """Long/short variant: 1 when signal on, -1 when off. Equal-weight basket."""
    W = pd.DataFrame(index=px.index, columns=COINS, dtype=float)
    for c in COINS:
        sig = weight_fn(px[c])
        W[c] = sig * 2 - 1   # map 0->-1, 1->+1
    return W / len(COINS)


# ---------------------------------------------------------------------------
# Run a single configuration, return metrics
# ---------------------------------------------------------------------------

def run_config(px: pd.DataFrame, W: pd.DataFrame, cost_bps: float,
               label: str, btc_px: pd.Series) -> dict:
    bt = q.backtest_weights(px, W, cost_bps=cost_bps)
    ret = bt["ret_net"]
    m = q.metrics_from_returns(ret, TF)
    m["label"] = label
    m["cost_bps"] = cost_bps
    return m, bt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    px = q.load_closes(COINS, TF).loc[START:END]
    btc = px["BTC"]

    HERE.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # DEFAULT CONFIG: 50/200 SMA crossover, vol-targeted, long/flat, basket
    # -----------------------------------------------------------------------
    def default_weight_fn(s):
        sig = sma_signal(s, fast=50, slow=200)
        return vol_scale(s, sig)

    W_default = basket_weights(px, default_weight_fn)
    bt_default = q.backtest_weights(px, W_default, cost_bps=COST_BPS_DEFAULT)
    m_default = q.metrics_from_returns(bt_default["ret_net"], TF)
    yearly_default = q.period_breakdown(bt_default["ret_net"])

    # Save results.csv (equity curve)
    bt_default.to_csv(HERE / "results.csv")

    print("=" * 60)
    print("DEFAULT CONFIG: 50/200 SMA, vol-targeted, long/flat, basket")
    print(f"  cost_bps = {COST_BPS_DEFAULT}")
    print("=" * 60)
    for k in ["cagr", "vol", "sharpe", "sortino", "max_drawdown", "calmar",
              "exposure", "years", "final_equity"]:
        print(f"  {k:16s} {m_default.get(k, float('nan')):.4f}")
    print("\nYearly breakdown:")
    print(yearly_default.to_string(index=False))

    # -----------------------------------------------------------------------
    # LONG/SHORT VARIANT of default (secondary)
    # -----------------------------------------------------------------------
    def default_ls_weight_fn(s):
        sig = sma_signal(s, fast=50, slow=200)
        return vol_scale(s, sig * 2 - 1, cap=VOL_CAP)   # -1..+1, still vol-scaled

    W_ls = basket_weights(px, default_ls_weight_fn)
    bt_ls = q.backtest_weights(px, W_ls, cost_bps=COST_BPS_DEFAULT)
    m_ls = q.metrics_from_returns(bt_ls["ret_net"], TF)

    print("\nLONG/SHORT VARIANT (same 50/200, vol-targeted):")
    for k in ["cagr", "sharpe", "max_drawdown", "calmar"]:
        print(f"  {k:16s} {m_ls.get(k, float('nan')):.4f}")

    # -----------------------------------------------------------------------
    # COST SENSITIVITY: default at 10bps
    # -----------------------------------------------------------------------
    bt_hi = q.backtest_weights(px, W_default, cost_bps=COST_BPS_HIGH)
    m_hi = q.metrics_from_returns(bt_hi["ret_net"], TF)

    print(f"\nCost sensitivity (10bps): CAGR={m_hi['cagr']:.3f}  Sharpe={m_hi['sharpe']:.3f}  MDD={m_hi['max_drawdown']:.3f}")

    # -----------------------------------------------------------------------
    # SENSITIVITY GRID — NO vol-targeting, long/flat
    # -----------------------------------------------------------------------
    grid_results = []

    # Family A: MA crossover
    ma_grid = [(10, 50), (20, 100), (50, 200)]
    for fast, slow in ma_grid:
        for variant, ls in [("long_flat", False), ("long_short", True)]:
            W = pd.DataFrame(index=px.index, columns=COINS, dtype=float)
            for c in COINS:
                sig = sma_signal(px[c], fast, slow)
                W[c] = (sig * 2 - 1) / len(COINS) if ls else sig / len(COINS)
            bt = q.backtest_weights(px, W, cost_bps=COST_BPS_DEFAULT)
            m = q.metrics_from_returns(bt["ret_net"], TF)
            m["signal"] = "MA"
            m["params"] = f"sma{fast}/{slow}"
            m["variant"] = variant
            grid_results.append(m)

    # Family B: TSMOM
    tsmom_grid = [30, 60, 90]
    for k in tsmom_grid:
        for variant, ls in [("long_flat", False), ("long_short", True)]:
            W = pd.DataFrame(index=px.index, columns=COINS, dtype=float)
            for c in COINS:
                sig = tsmom_signal(px[c], k)
                W[c] = (sig * 2 - 1) / len(COINS) if ls else sig / len(COINS)
            bt = q.backtest_weights(px, W, cost_bps=COST_BPS_DEFAULT)
            m = q.metrics_from_returns(bt["ret_net"], TF)
            m["signal"] = "TSMOM"
            m["params"] = f"mom{k}d"
            m["variant"] = variant
            grid_results.append(m)

    print("\n" + "=" * 80)
    print("SENSITIVITY GRID (no vol-target, 5bps):")
    print(f"{'signal':8s} {'params':12s} {'variant':12s} {'CAGR':>8s} {'Sharpe':>8s} {'Sortino':>8s} {'MDD':>8s} {'Calmar':>8s} {'Exp':>6s}")
    for r in grid_results:
        print(f"{r['signal']:8s} {r['params']:12s} {r['variant']:12s} "
              f"{r.get('cagr',0):8.3f} {r.get('sharpe',0):8.3f} "
              f"{r.get('sortino',0):8.3f} {r.get('max_drawdown',0):8.3f} "
              f"{r.get('calmar',0):8.3f} {r.get('exposure',0):6.2f}")

    # -----------------------------------------------------------------------
    # PER-COIN breakdown for default signal (no vol-target, for transparency)
    # -----------------------------------------------------------------------
    print("\nPer-coin (50/200 SMA, long/flat, no vol-target, 5bps):")
    per_coin_metrics = {}
    for c in COINS:
        sig = sma_signal(px[c], 50, 200)
        W_c = pd.DataFrame({c: sig}, index=px.index)
        bt_c = q.backtest_weights(px[[c]], W_c, cost_bps=COST_BPS_DEFAULT)
        m_c = q.metrics_from_returns(bt_c["ret_net"], TF)
        per_coin_metrics[c] = m_c
        print(f"  {c}: CAGR={m_c.get('cagr',0):.3f}  Sharpe={m_c.get('sharpe',0):.3f}  MDD={m_c.get('max_drawdown',0):.3f}")

    # -----------------------------------------------------------------------
    # BUY & HOLD BTC benchmark
    # -----------------------------------------------------------------------
    btc_ret = btc.pct_change().dropna()
    m_bh = q.metrics_from_returns(btc_ret, TF)
    print(f"\nBuy&Hold BTC: CAGR={m_bh['cagr']:.3f}  Sharpe={m_bh['sharpe']:.3f}  MDD={m_bh['max_drawdown']:.3f}")

    # -----------------------------------------------------------------------
    # Count trades (weight sign changes = entries) for default
    # -----------------------------------------------------------------------
    n_trades_total = 0
    for c in COINS:
        sig = sma_signal(px[c], 50, 200)
        vs = vol_scale(px[c], sig)
        # entry = transition from 0 to non-zero (after shift done by qutil, we count on raw signal)
        entries = ((sig != 0) & (sig.shift(1).fillna(0) == 0)).sum()
        n_trades_total += int(entries)

    # -----------------------------------------------------------------------
    # Save all metrics to JSON
    # -----------------------------------------------------------------------
    out = {
        "strategy": "crypto_trend_tsmom",
        "tf": TF,
        "coins": COINS,
        "window": f"{START}..{END}",
        "default_config": {
            "signal": "SMA crossover 50/200",
            "variant": "long_flat",
            "vol_targeting": True,
            "target_vol": TARGET_VOL,
            "vol_window": VOL_WINDOW,
            "vol_cap": VOL_CAP,
            "cost_bps": COST_BPS_DEFAULT,
        },
        "default_metrics_5bps": m_default,
        "default_metrics_10bps": m_hi,
        "long_short_variant_5bps": m_ls,
        "yearly_breakdown_default": yearly_default.to_dict("records"),
        "benchmark_btc_buyhold": m_bh,
        "per_coin_metrics": per_coin_metrics,
        "sensitivity_grid": grid_results,
        "n_entry_trades_default": n_trades_total,
    }
    q.save_metrics(HERE / "metrics.json", out)

    # -----------------------------------------------------------------------
    # Trades CSV
    # -----------------------------------------------------------------------
    trades_rows = []
    for c in COINS:
        sig = sma_signal(px[c], 50, 200)
        vs = vol_scale(px[c], sig)
        entries_idx = sig.index[(sig != 0) & (sig.shift(1).fillna(0) == 0)]
        exits_idx = sig.index[(sig == 0) & (sig.shift(1).fillna(0) != 0)]
        for dt in entries_idx:
            trades_rows.append({"coin": c, "type": "entry", "date": dt.date(), "signal": "SMA50/200"})
        for dt in exits_idx:
            trades_rows.append({"coin": c, "type": "exit", "date": dt.date(), "signal": "SMA50/200"})
    pd.DataFrame(trades_rows).sort_values("date").to_csv(HERE / "trades.csv", index=False)

    # -----------------------------------------------------------------------
    # Equity plot
    # -----------------------------------------------------------------------
    eq = bt_default["equity"]
    q.equity_plot(eq / eq.iloc[0], "Crypto TSMOM/Trend: 50/200 SMA, vol-targeted basket",
                  HERE / "equity.png", benchmark=btc)

    print(f"\nSaved: results.csv, trades.csv, metrics.json, equity.png -> {HERE}")
    print(f"Total entry trades (default, all coins): {n_trades_total}")

    return m_default, grid_results, yearly_default, m_bh, n_trades_total


if __name__ == "__main__":
    run()
