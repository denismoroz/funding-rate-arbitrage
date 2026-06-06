"""
Crypto Cross-Sectional Momentum Backtest
=========================================
Strategy: Each week rank coins by trailing-K-day return.
  Long-only: long top-N equal weight (1/N each, gross=1).
  Long-short: long top-N / short bottom-N, dollar-neutral (1/N each side, gross=2).
Rebalance: weekly. Daily harness with weekly weight updates (forward-fill within week).
Costs: DEFAULT_COST_BPS per side on |Δweight| (turns over ~weekly).

Survivorship caveat: Universe is coins liquid/surviving TODAY. A real deployment
needs point-in-time listings. This MATERIALLY biases results upward.

Universe: BTC ETH SOL AVAX LINK AAVE ARB OP DOGE UNI INJ TIA
  (MATIC excluded: POL rebrand truncates series 2024-09-10)
  TIA starts 2023-10-31 so inner-join gives ~Oct-2023 start.
"""
import sys
import json
from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
TF = "1d"

UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE",
            "ARB", "OP", "DOGE", "UNI", "INJ", "TIA"]

# Default parameters (a priori)
DEFAULT_K = 30    # trailing days for momentum signal
DEFAULT_N = 3     # coins per side

GRID_K = [7, 14, 30, 60, 90]
GRID_N = [2, 3, 4]

COST_BPS_BASE = 5.0
COST_BPS_HIGH = 10.0

REBAL_DOW = 0   # Monday = 0 (weekly rebalance boundary)


# ---------------------------------------------------------------------------
# Signal + weight construction
# ---------------------------------------------------------------------------

def compute_weights_longonly(px: pd.DataFrame, K: int, N: int) -> pd.DataFrame:
    """Weekly-rebalanced long-only: top-N by K-day trailing return.

    Signal is computed each bar using only data through close[t] (no look-ahead).
    Weight changes ONLY on weekly boundaries; within the week the weight is
    forward-filled from the previous boundary, so turnover is ~weekly.
    qutil.backtest_weights will shift these weights forward 1 bar (next-bar exec).
    """
    ret_K = px.pct_change(K)   # trailing K-day return, known at close[t]

    W = pd.DataFrame(0.0, index=px.index, columns=px.columns)

    # Identify weekly rebalance dates: first bar of each ISO week (or any Monday)
    # We rebalance every bar whose day_of_week == REBAL_DOW, or the very first bar.
    is_rebal = pd.Series(False, index=px.index)
    is_rebal.iloc[0] = True  # always start

    # Mark first bar of each calendar week
    week_id = pd.Series(
        [(d.isocalendar()[0], d.isocalendar()[1]) for d in px.index],
        index=px.index
    )
    is_rebal |= (week_id != week_id.shift(1))

    current_w = pd.Series(0.0, index=px.columns)
    for t in px.index:
        if is_rebal.loc[t]:
            r = ret_K.loc[t]
            valid = r.dropna()
            if len(valid) < N:
                current_w = pd.Series(0.0, index=px.columns)
            else:
                ranked = valid.rank(ascending=False)
                top = ranked[ranked <= N].index.tolist()
                w = pd.Series(0.0, index=px.columns)
                w[top] = 1.0 / N
                current_w = w
        W.loc[t] = current_w

    return W


def compute_weights_longshort(px: pd.DataFrame, K: int, N: int) -> pd.DataFrame:
    """Weekly-rebalanced long-short dollar-neutral: long top-N, short bottom-N.

    Each side 1/N weight => gross=2, net≈0 market exposure.
    """
    ret_K = px.pct_change(K)

    W = pd.DataFrame(0.0, index=px.index, columns=px.columns)

    is_rebal = pd.Series(False, index=px.index)
    is_rebal.iloc[0] = True
    week_id = pd.Series(
        [(d.isocalendar()[0], d.isocalendar()[1]) for d in px.index],
        index=px.index
    )
    is_rebal |= (week_id != week_id.shift(1))

    current_w = pd.Series(0.0, index=px.columns)
    for t in px.index:
        if is_rebal.loc[t]:
            r = ret_K.loc[t]
            valid = r.dropna()
            n_coins = len(valid)
            if n_coins < 2 * N:
                current_w = pd.Series(0.0, index=px.columns)
            else:
                ranked = valid.rank(ascending=False)
                top = ranked[ranked <= N].index.tolist()
                # Exclude top from bottom candidates to avoid overlap
                remaining_ranked = ranked[~ranked.index.isin(top)]
                bottom = remaining_ranked[
                    remaining_ranked >= (n_coins - N + 1)
                ].index.tolist()
                w = pd.Series(0.0, index=px.columns)
                w[top] = 1.0 / N
                w[bottom] = -1.0 / N
                current_w = w
        W.loc[t] = current_w

    return W


# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------

def btc_benchmark(px: pd.DataFrame, cost_bps: float) -> dict:
    """Buy-and-hold BTC: 100% weight from day 1."""
    w_btc = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    w_btc["BTC"] = 1.0
    bt = q.backtest_weights(px, w_btc, cost_bps=cost_bps)
    return q.metrics_from_returns(bt["ret_net"], TF), bt


def ew_benchmark(px: pd.DataFrame, cost_bps: float) -> dict:
    """Equal-weight all coins, weekly rebalance."""
    N = len(px.columns)
    # Rebalance weekly (forward-fill approach, same as strategy)
    is_rebal = pd.Series(False, index=px.index)
    is_rebal.iloc[0] = True
    week_id = pd.Series(
        [(d.isocalendar()[0], d.isocalendar()[1]) for d in px.index],
        index=px.index
    )
    is_rebal |= (week_id != week_id.shift(1))

    W = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    current_w = pd.Series(1.0 / N, index=px.columns)
    for t in px.index:
        if is_rebal.loc[t]:
            current_w = pd.Series(1.0 / N, index=px.columns)
        W.loc[t] = current_w
    bt = q.backtest_weights(px, W, cost_bps=cost_bps)
    return q.metrics_from_returns(bt["ret_net"], TF), bt


# ---------------------------------------------------------------------------
# Trades log
# ---------------------------------------------------------------------------

def build_trades_log(W: pd.DataFrame, label: str) -> pd.DataFrame:
    """Record weekly rebalances: date, holdings, turnover."""
    week_id = pd.Series(
        [(d.isocalendar()[0], d.isocalendar()[1]) for d in W.index],
        index=W.index
    )
    is_rebal = (week_id != week_id.shift(1))
    is_rebal.iloc[0] = True
    rebal_idx = W.index[is_rebal]

    rows = []
    prev_w = pd.Series(0.0, index=W.columns)
    for t in rebal_idx:
        w = W.loc[t]
        turnover = (w - prev_w).abs().sum()
        longs = w[w > 0].index.tolist()
        shorts = w[w < 0].index.tolist()
        rows.append({
            "date": t.date(),
            "label": label,
            "longs": ",".join(longs),
            "shorts": ",".join(shorts) if shorts else "",
            "n_long": len(longs),
            "n_short": len(shorts),
            "gross": float(w.abs().sum()),
            "turnover": float(turnover),
        })
        prev_w = w
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    # Load prices — inner join, all 12 coins; TIA limits start to ~2023-10-31
    px = q.load_closes(UNIVERSE, TF).dropna()
    print(f"Price panel: {px.shape[1]} coins, {len(px)} bars, "
          f"{px.index[0].date()} -> {px.index[-1].date()}")

    # -----------------------------------------------------------------------
    # Benchmarks
    # -----------------------------------------------------------------------
    btc_m, btc_bt = btc_benchmark(px, COST_BPS_BASE)
    ew_m, ew_bt = ew_benchmark(px, COST_BPS_BASE)
    print(f"\nBTC B&H:        CAGR={btc_m['cagr']:.1%}  Sharpe={btc_m['sharpe']:.2f}  "
          f"MDD={btc_m['max_drawdown']:.1%}")
    print(f"EW basket:      CAGR={ew_m['cagr']:.1%}  Sharpe={ew_m['sharpe']:.2f}  "
          f"MDD={ew_m['max_drawdown']:.1%}")

    # -----------------------------------------------------------------------
    # Default: Long-only K=30, N=3 @ 5bps
    # -----------------------------------------------------------------------
    print("\n--- Long-only default K=30 N=3 ---")
    W_lo = compute_weights_longonly(px, DEFAULT_K, DEFAULT_N)
    bt_lo = q.backtest_weights(px, W_lo, cost_bps=COST_BPS_BASE)
    m_lo = q.metrics_from_returns(bt_lo["ret_net"], TF)
    yr_lo = q.period_breakdown(bt_lo["ret_net"])
    print(f"  CAGR={m_lo['cagr']:.1%}  Sharpe={m_lo['sharpe']:.2f}  "
          f"Sortino={m_lo['sortino']:.2f}  MDD={m_lo['max_drawdown']:.1%}  "
          f"Calmar={m_lo['calmar']:.2f}")
    print("  Yearly breakdown:")
    print(yr_lo.to_string(index=False))

    # Also 10bps
    bt_lo_10 = q.backtest_weights(px, W_lo, cost_bps=COST_BPS_HIGH)
    m_lo_10 = q.metrics_from_returns(bt_lo_10["ret_net"], TF)
    print(f"  @10bps: CAGR={m_lo_10['cagr']:.1%}  Sharpe={m_lo_10['sharpe']:.2f}  "
          f"MDD={m_lo_10['max_drawdown']:.1%}")

    # -----------------------------------------------------------------------
    # Default: Long-short K=30, N=3 @ 5bps
    # -----------------------------------------------------------------------
    print("\n--- Long-short default K=30 N=3 ---")
    W_ls = compute_weights_longshort(px, DEFAULT_K, DEFAULT_N)
    bt_ls = q.backtest_weights(px, W_ls, cost_bps=COST_BPS_BASE)
    m_ls = q.metrics_from_returns(bt_ls["ret_net"], TF)
    yr_ls = q.period_breakdown(bt_ls["ret_net"])
    print(f"  CAGR={m_ls['cagr']:.1%}  Sharpe={m_ls['sharpe']:.2f}  "
          f"Sortino={m_ls['sortino']:.2f}  MDD={m_ls['max_drawdown']:.1%}  "
          f"Calmar={m_ls['calmar']:.2f}")
    print("  Yearly breakdown:")
    print(yr_ls.to_string(index=False))

    bt_ls_10 = q.backtest_weights(px, W_ls, cost_bps=COST_BPS_HIGH)
    m_ls_10 = q.metrics_from_returns(bt_ls_10["ret_net"], TF)
    print(f"  @10bps: CAGR={m_ls_10['cagr']:.1%}  Sharpe={m_ls_10['sharpe']:.2f}  "
          f"MDD={m_ls_10['max_drawdown']:.1%}")

    # -----------------------------------------------------------------------
    # K x N grid (long-only, 5bps)
    # -----------------------------------------------------------------------
    print("\n--- K x N grid (long-only, 5bps) ---")
    grid_results = {}
    best_sharpe = -np.inf
    best_cell = None
    for K, N in product(GRID_K, GRID_N):
        W_g = compute_weights_longonly(px, K, N)
        bt_g = q.backtest_weights(px, W_g, cost_bps=COST_BPS_BASE)
        m_g = q.metrics_from_returns(bt_g["ret_net"], TF)
        key = f"K{K}_N{N}"
        grid_results[key] = {
            "K": K, "N": N,
            "cagr": round(m_g["cagr"], 4),
            "sharpe": round(m_g["sharpe"], 3),
            "sortino": round(m_g.get("sortino", float("nan")), 3),
            "max_drawdown": round(m_g["max_drawdown"], 4),
            "calmar": round(m_g["calmar"], 3),
        }
        if m_g["sharpe"] > best_sharpe:
            best_sharpe = m_g["sharpe"]
            best_cell = key
        print(f"  K={K:3d} N={N}: CAGR={m_g['cagr']:6.1%}  Sharpe={m_g['sharpe']:.2f}  "
              f"MDD={m_g['max_drawdown']:.1%}  Calmar={m_g['calmar']:.2f}")

    print(f"\nBest cell by Sharpe: {best_cell} = {grid_results[best_cell]}")

    # -----------------------------------------------------------------------
    # Save results
    # -----------------------------------------------------------------------
    HERE.mkdir(parents=True, exist_ok=True)

    # results.csv: daily equity for all main scenarios
    results = pd.DataFrame({
        "ret_net_lo5": bt_lo["ret_net"],
        "equity_lo5": bt_lo["equity"],
        "ret_net_ls5": bt_ls["ret_net"],
        "equity_ls5": bt_ls["equity"],
        "ret_net_lo10": bt_lo_10["ret_net"],
        "equity_lo10": bt_lo_10["equity"],
        "ret_net_ls10": bt_ls_10["ret_net"],
        "equity_ls10": bt_ls_10["equity"],
        "ret_net_btc": btc_bt["ret_net"],
        "equity_btc": btc_bt["equity"],
        "ret_net_ew": ew_bt["ret_net"],
        "equity_ew": ew_bt["equity"],
    })
    results.to_csv(HERE / "results.csv")

    # trades.csv: weekly holdings log
    trades_lo = build_trades_log(W_lo, "long_only")
    trades_ls = build_trades_log(W_ls, "long_short")
    trades = pd.concat([trades_lo, trades_ls], ignore_index=True)
    trades.to_csv(HERE / "trades.csv", index=False)

    # metrics.json
    out = {
        "strategy": "crypto_xsec_momentum",
        "tf": TF,
        "universe": UNIVERSE,
        "data_range": {
            "start": str(px.index[0].date()),
            "end": str(px.index[-1].date()),
            "n_bars": len(px),
        },
        "survivorship_bias_warning": (
            "Universe is liquid coins surviving through 2026-06. "
            "Point-in-time listing data NOT used. Results are materially biased upward."
        ),
        "params_default": {"K": DEFAULT_K, "N": DEFAULT_N},
        "benchmarks": {
            "btc_buyhold_5bps": btc_m,
            "ew_basket_5bps": ew_m,
        },
        "long_only_K30_N3": {
            "5bps": m_lo,
            "10bps": m_lo_10,
            "yearly_5bps": yr_lo.to_dict("records"),
        },
        "long_short_K30_N3": {
            "5bps": m_ls,
            "10bps": m_ls_10,
            "yearly_5bps": yr_ls.to_dict("records"),
        },
        "grid_longonly_5bps": grid_results,
        "best_grid_cell_by_sharpe": best_cell,
    }
    q.save_metrics(HERE / "metrics.json", out)

    # Equity plots
    q.equity_plot(
        bt_lo["equity"],
        f"XSec Momentum Long-Only K={DEFAULT_K} N={DEFAULT_N} (5bps)",
        HERE / "equity.png",
        benchmark=px["BTC"]
    )
    # Long-short equity
    q.equity_plot(
        bt_ls["equity"],
        f"XSec Momentum Long-Short K={DEFAULT_K} N={DEFAULT_N} (5bps)",
        HERE / "equity_ls.png",
        benchmark=px["BTC"]
    )

    print("\nSaved: results.csv, trades.csv, metrics.json, equity.png, equity_ls.png")
    return m_lo, m_ls, grid_results, best_cell, yr_lo, yr_ls, btc_m, ew_m


if __name__ == "__main__":
    run()
