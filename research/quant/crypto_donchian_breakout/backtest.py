"""
Donchian Channel Breakout — classic Turtle Trading System.

Signal (long/flat only):
  Enter LONG  when close[t] > prior-N-day highest HIGH
              i.e. close[t] > high.rolling(N).max().shift(1)  (excludes today's high)
  Exit to flat when close[t] < prior-M-day lowest LOW
              i.e. close[t] < low.rolling(M).min().shift(1)   (excludes today's low)

Weight is 1.0 when in position, 0.0 when flat.
qutil.backtest_weights shifts the weight one extra bar (execution at next-bar open/close),
which is fully conservative.

Also tests a long/short version:
  Short leg: enter short when close[t] < prior-N-day lowest LOW.

Grid: N in {20, 55}, M in {10, 20}. Default: N=55, M=20 (Turtle System 2).
Costs: 5 bps per side (default), also reported at 10 bps.
Assets: BTC, ETH, SOL (full 2023-06-01..2026-06-01), plus equal-weight basket.
"""
import sys, json
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
TF = "1d"
COINS = ["BTC", "ETH", "SOL"]
DEFAULT_N = 55   # entry: N-day high breakout
DEFAULT_M = 20   # exit:  M-day low breakdown
COST_BPS = 5.0


# ---------------------------------------------------------------------------
# Signal construction
# ---------------------------------------------------------------------------

def donchian_weights(ohlcv: dict[str, pd.DataFrame], N: int, M: int,
                     long_short: bool = False) -> pd.DataFrame:
    """Compute per-coin target weights on daily bars.

    For each coin:
      upper = high.rolling(N).max().shift(1)   # prior N-day high
      lower = low.rolling(M).min().shift(1)    # prior M-day low

    State machine: starts flat.
      If flat & close > upper -> go long (1.0)
      If flat & long_short & close < lower -> go short (-1.0)
      If long  & close < lower -> go flat  (0.0)
      If short & close > upper -> go flat  (0.0)

    Returns DataFrame indexed by the common daily index, columns = COINS.
    """
    # Build aligned close/high/low panels
    closes = pd.DataFrame({c: ohlcv[c]["close"] for c in COINS}).sort_index()
    highs  = pd.DataFrame({c: ohlcv[c]["high"]  for c in COINS}).sort_index()
    lows   = pd.DataFrame({c: ohlcv[c]["low"]   for c in COINS}).sort_index()

    # Donchian channels (prior-bar, no look-ahead)
    upper = highs.rolling(N).max().shift(1)
    lower = lows.rolling(M).min().shift(1)

    weights = pd.DataFrame(0.0, index=closes.index, columns=COINS)
    for coin in COINS:
        cl = closes[coin].values
        up = upper[coin].values
        lo = lower[coin].values
        w  = np.zeros(len(cl))
        cur = 0  # -1 short, 0 flat, 1 long
        for i in range(len(cl)):
            if np.isnan(up[i]) or np.isnan(lo[i]):
                w[i] = 0; cur = 0; continue
            if cur == 0:
                if cl[i] > up[i]:
                    cur = 1
                elif long_short and cl[i] < lo[i]:
                    cur = -1
            elif cur == 1:
                if cl[i] < lo[i]:
                    cur = 0
            elif cur == -1:
                if cl[i] > up[i]:
                    cur = 0
            w[i] = cur
        weights[coin] = w

    return weights


def build_trades(ohlcv: dict[str, pd.DataFrame], weights: pd.DataFrame,
                 coin: str) -> pd.DataFrame:
    """Extract individual trade records for a single coin."""
    # qutil shifts weights by 1 bar; replicate here for consistent trade dates
    w = weights[coin].shift(1).fillna(0.0)
    close = ohlcv[coin]["close"].reindex(w.index)

    trades = []
    in_trade = False
    entry_date = entry_px = cur_dir = None

    prev_w = 0.0
    for date, wt in w.items():
        if not in_trade:
            if wt != 0.0:
                in_trade = True
                entry_date = date
                entry_px = close.loc[date]
                cur_dir = wt
        else:
            if wt == 0.0:
                exit_date = date
                exit_px = close.loc[date]
                ret_pct = cur_dir * (exit_px / entry_px - 1.0) * 100.0
                bars = (exit_date - entry_date).days
                trades.append({
                    "coin": coin, "direction": "long" if cur_dir > 0 else "short",
                    "entry_date": entry_date.date(), "entry_px": round(entry_px, 4),
                    "exit_date": exit_date.date(), "exit_px": round(exit_px, 4),
                    "return_pct": round(ret_pct, 3),
                    "bars_held": bars,
                    "pnl": ret_pct / 100.0,
                })
                in_trade = False
            # else: still in trade (direction may be the same)
        prev_w = wt

    # open trade at end
    if in_trade:
        exit_date = w.index[-1]
        exit_px = close.iloc[-1]
        ret_pct = cur_dir * (exit_px / entry_px - 1.0) * 100.0
        bars = (exit_date - entry_date).days
        trades.append({
            "coin": coin, "direction": "long" if cur_dir > 0 else "short",
            "entry_date": entry_date.date(), "entry_px": round(entry_px, 4),
            "exit_date": "(open)", "exit_px": round(exit_px, 4),
            "return_pct": round(ret_pct, 3),
            "bars_held": bars,
            "pnl": ret_pct / 100.0,
        })

    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_config(ohlcv, N, M, cost_bps, long_short=False, label=""):
    """Run one backtest configuration; return (metrics_dict, trades_df)."""
    weights = donchian_weights(ohlcv, N, M, long_short=long_short)

    # Prices panel
    closes = pd.DataFrame({c: ohlcv[c]["close"] for c in COINS}).sort_index()

    # --- individual coin results ---
    coin_metrics = {}
    all_trades = []
    for coin in COINS:
        bt_c = q.backtest_weights(closes[[coin]], weights[[coin]], cost_bps=cost_bps)
        m_c = q.metrics_from_returns(bt_c["ret_net"], TF)
        coin_metrics[coin] = m_c
        tr = build_trades(ohlcv, weights, coin)
        all_trades.append(tr)

    # --- equal-weight basket ---
    basket_w = weights / len(COINS)   # weight per coin = 1/3 when in
    bt_basket = q.backtest_weights(closes, basket_w, cost_bps=cost_bps)
    m_basket = q.metrics_from_returns(bt_basket["ret_net"], TF)

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    closed_trades = trades_df[trades_df["exit_date"] != "(open)"] if len(trades_df) else trades_df
    ts = q.trade_stats(closed_trades, "pnl")

    # basket exposure from average weight held
    basket_w_held = basket_w.shift(1).fillna(0.0)
    exposure = float((basket_w_held.abs().sum(axis=1) > 0).mean())

    result = {
        "label": label or f"N={N}/M={M}/ls={long_short}/cost={cost_bps}bps",
        "params": {"N": N, "M": M, "long_short": long_short, "cost_bps": cost_bps},
        "basket": m_basket,
        "coin_metrics": coin_metrics,
        "trade_stats": ts,
        "exposure": exposure,
    }
    return result, bt_basket, trades_df


def run():
    # Load OHLCV
    ohlcv = {c: q.load_ohlcv(c, TF) for c in COINS}

    # BTC benchmark for plots
    btc_close = ohlcv["BTC"]["close"]

    HERE.mkdir(parents=True, exist_ok=True)

    # ---- Grid search ----
    grid_results = []
    all_results_detail = []
    N_values = [20, 55]
    M_values = [10, 20]

    default_bt = None
    default_trades = None

    for N in N_values:
        for M in M_values:
            for cost_bps in [5.0, 10.0]:
                label = f"N={N}/M={M}/long-only/cost={cost_bps}bps"
                res, bt, trd = run_config(ohlcv, N, M, cost_bps, long_short=False, label=label)
                grid_results.append(res)
                all_results_detail.append((res, bt, trd))
                if N == DEFAULT_N and M == DEFAULT_M and cost_bps == COST_BPS:
                    default_bt = bt
                    default_trades = trd

    # Long/short variant (default params, 5bps)
    res_ls, bt_ls, trd_ls = run_config(ohlcv, DEFAULT_N, DEFAULT_M, COST_BPS,
                                        long_short=True, label=f"N={DEFAULT_N}/M={DEFAULT_M}/long-short/cost=5bps")
    grid_results.append(res_ls)

    # ---- Default config: yearly breakdown ----
    default_res = next(r for r in grid_results
                       if r["params"]["N"] == DEFAULT_N
                       and r["params"]["M"] == DEFAULT_M
                       and not r["params"]["long_short"]
                       and r["params"]["cost_bps"] == COST_BPS)

    yearly = q.period_breakdown(default_bt["ret_net"])

    # ---- BTC buy & hold ----
    btc_ret = btc_close.pct_change().fillna(0.0)
    bh_metrics = q.metrics_from_returns(btc_ret, TF)

    # ---- Save results.csv ----
    default_bt.to_csv(HERE / "results.csv")

    # ---- Save trades.csv ----
    default_trades.to_csv(HERE / "trades.csv", index=False)

    # ---- Save long/short trades ----
    trd_ls.to_csv(HERE / "trades_long_short.csv", index=False)

    # ---- Save metrics.json ----
    closed_def = default_trades[default_trades["exit_date"] != "(open)"] if len(default_trades) else default_trades
    ts_def = q.trade_stats(closed_def, "pnl")
    closed_ls = trd_ls[trd_ls["exit_date"] != "(open)"] if len(trd_ls) else trd_ls
    ts_ls = q.trade_stats(closed_ls, "pnl")

    # Grid summary table
    grid_rows = []
    for r in grid_results:
        p = r["params"]
        bm = r["basket"]
        ts = r["trade_stats"]
        grid_rows.append({
            "N": p["N"], "M": p["M"], "long_short": p["long_short"],
            "cost_bps": p["cost_bps"],
            "cagr": bm.get("cagr"), "sharpe": bm.get("sharpe"),
            "sortino": bm.get("sortino"), "max_dd": bm.get("max_drawdown"),
            "calmar": bm.get("calmar"), "exposure": r["exposure"],
            "n_trades": ts.get("n_trades"), "win_rate": ts.get("win_rate"),
            "profit_factor": ts.get("profit_factor"),
            "avg_trade": ts.get("avg_trade"),
        })
    grid_df = pd.DataFrame(grid_rows)
    grid_df.to_csv(HERE / "grid_search.csv", index=False)

    metrics_out = {
        "strategy": "crypto_donchian_breakout",
        "tf": TF,
        "coins": COINS,
        "default_params": {"N": DEFAULT_N, "M": DEFAULT_M, "long_short": False, "cost_bps": COST_BPS},
        "benchmark_btc_buyhold": bh_metrics,
        "default_basket": default_res["basket"],
        "default_coin_metrics": default_res["coin_metrics"],
        "default_trade_stats_5bps": ts_def,
        "long_short_basket_5bps": res_ls["basket"],
        "long_short_trade_stats_5bps": ts_ls,
        "grid": grid_rows,
        "yearly_breakdown": yearly.to_dict("records"),
    }
    q.save_metrics(HERE / "metrics.json", metrics_out)

    # ---- Equity plot ----
    eq = default_bt["equity"]
    q.equity_plot(eq / eq.iloc[0], f"Donchian Breakout N={DEFAULT_N}/M={DEFAULT_M} basket (5bps)",
                  HERE / "equity.png", benchmark=btc_close)

    # ---- Print summary ----
    bm = default_res["basket"]
    print("=" * 65)
    print(f"DONCHIAN BREAKOUT (N={DEFAULT_N}/M={DEFAULT_M}, long-only, 5bps, equal-weight basket)")
    print("=" * 65)
    for k in ["cagr", "vol", "sharpe", "sortino", "max_drawdown", "calmar", "exposure", "years"]:
        v = bm.get(k, float("nan"))
        print(f"  {k:16s} {v:.4f}")
    print()
    print("TRADE STATS (closed trades, all coins, 5bps):")
    for k, v in ts_def.items():
        print(f"  {k:20s} {v:.4f}" if isinstance(v, float) else f"  {k:20s} {v}")
    print()
    print("YEARLY BREAKDOWN (basket):")
    print(yearly.to_string(index=False))
    print()
    print("GRID SEARCH (basket CAGR, 5bps only):")
    g5 = grid_df[~grid_df["long_short"] & (grid_df["cost_bps"] == 5.0)][
        ["N", "M", "cagr", "sharpe", "max_dd", "calmar", "n_trades", "win_rate", "profit_factor"]
    ]
    print(g5.to_string(index=False))
    print()
    print("COST SENSITIVITY (N=55/M=20):")
    cs = grid_df[(grid_df["N"] == 55) & (grid_df["M"] == 20) & ~grid_df["long_short"]][
        ["cost_bps", "cagr", "sharpe", "calmar"]
    ]
    print(cs.to_string(index=False))
    print()
    print("LONG/SHORT VARIANT (N=55/M=20, 5bps):")
    for k in ["cagr", "sharpe", "sortino", "max_drawdown", "calmar"]:
        print(f"  {k:16s} {res_ls['basket'].get(k, float('nan')):.4f}")
    print()
    print("BENCHMARK BTC buy&hold:")
    for k in ["cagr", "sharpe", "max_drawdown"]:
        print(f"  {k:16s} {bh_metrics.get(k, float('nan')):.4f}")
    print()

    # Best grid cell
    g5_sorted = g5.sort_values("calmar", ascending=False)
    best = g5_sorted.iloc[0]
    print(f"BEST GRID CELL (by Calmar, 5bps): N={int(best['N'])}/M={int(best['M'])}"
          f"  CAGR={best['cagr']:.3f}  Sharpe={best['sharpe']:.3f}"
          f"  MaxDD={best['max_dd']:.3f}  Calmar={best['calmar']:.3f}")


if __name__ == "__main__":
    run()
