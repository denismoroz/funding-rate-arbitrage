"""
Crypto Funding Rate Contrarian / Reversal Backtest.

HYPOTHESIS: Extreme funding rates signal crowded positioning that subsequently reverts.
  - High funding (over-leveraged longs) -> price tends to FALL -> go SHORT.
  - Low / negative funding -> price tends to RISE -> go LONG.
This is CONTRARIAN on funding, testing genuine PRICE-reversal alpha (not just carry income).

KEY DESIGN: Price PnL vs Funding PnL decomposition.
  Each variant runs backtest_weights TWICE on the SAME weights:
    price_only: cost_bps=5, funding=None   -> pure directional price PnL
    total:      cost_bps=5, funding=panel  -> price + funding carry
  If price_only Sharpe ≈ 0 or negative, the strategy is carry in disguise. We say so plainly.

Variant A — CROSS-SECTIONAL (market-neutral):
  Each day rank coins by funding z-score; SHORT top-N (highest), LONG bottom-N (lowest).
  Dollar-neutral (net weight = 0, gross = 2). Default N=3, L=30.
  Grid: N in {2,3,4} x L in {14,30,60}.

Variant B — TIME-SERIES per-coin:
  weight_i = -1 if z_i > +Z_thresh ; +1 if z_i < -Z_thresh ; else 0. (contrarian)
  Average across coins. Default Z=1.5.
  Grid: Z in {1.0, 1.5, 2.0}.

No look-ahead: z-scores use trailing windows ending at t; qutil shifts weights +1 bar.
Universe: BTC,ETH,SOL,AVAX,LINK,AAVE,ARB,OP,DOGE,UNI,INJ,TIA (full history, MATIC excluded).
Secondary: add HYPE, ZEC (shorter history) — reported separately.
Costs: 5 bps default + 10 bps sensitivity.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
TF = "1d"

UNIVERSE_FULL = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "ARB", "OP", "DOGE", "UNI", "INJ", "TIA"]
UNIVERSE_EXTENDED = UNIVERSE_FULL + ["HYPE", "ZEC"]

# Default parameters
DEFAULT_L = 30       # z-score rolling window (days)
DEFAULT_N = 3        # top/bottom N coins for cross-sectional
DEFAULT_Z = 1.5      # threshold for time-series variant
DEFAULT_COST_BPS = 5.0
HIGH_COST_BPS = 10.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_funding_daily(coins: list[str]) -> pd.DataFrame:
    """
    Load hourly funding CSVs for each coin, resample to daily SUM.
    Daily sum = the day's total funding fraction received by a long position.
    Returns a date-indexed DataFrame (UTC, daily).
    """
    panels = {}
    for coin in coins:
        fp = q.DATA_DIR / f"{coin}.csv"
        if not fp.exists():
            continue
        df = pd.read_csv(fp)
        df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
        df = df.set_index("time").sort_index()
        # de-duplicate timestamps (keep first)
        df = df[~df.index.duplicated(keep="first")]
        r = df["fundingRate"].astype(float)
        # resample to daily sum (causal: bar D covers [D 00:00, D+1 00:00) hourly stamps)
        daily = r.resample("1D").sum()
        panels[coin] = daily
    out = pd.DataFrame(panels).sort_index()
    # UTC midnight index
    out.index = out.index.tz_localize(None) if out.index.tzinfo is not None else out.index
    return out


def load_prices_daily(coins: list[str]) -> pd.DataFrame:
    """Load daily close prices; UTC midnight index stripped of tz for merge.
    Coins whose 1h OHLCV file is missing standard columns (e.g. HYPE has only close)
    are loaded directly from the funding CSV's close column if available, otherwise skipped.
    """
    # First try the standard qutil path for each coin individually
    cols = {}
    for c in coins:
        try:
            s = q.load_ohlcv(c, "1d")["close"]
            cols[c] = s
        except (FileNotFoundError, KeyError):
            # Fallback: try to build daily closes from the funding CSV (has 'close' for HYPE)
            fp_funding = q.DATA_DIR / f"{c}.csv"
            fp_1h = q.DATA_DIR / f"{c}_1h.csv"
            loaded = False
            for fp in [fp_1h, fp_funding]:
                if fp.exists():
                    try:
                        df = pd.read_csv(fp)
                        df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
                        df = df.set_index("time").sort_index()
                        if "close" in df.columns:
                            s = df["close"].astype(float)
                            s = s[~s.index.duplicated(keep="first")]
                            daily = s.resample("1D").last().dropna()
                            cols[c] = daily
                            loaded = True
                            break
                    except Exception:
                        continue
            if not loaded:
                print(f"  WARNING: could not load prices for {c}, skipping")
    px = pd.DataFrame(cols).sort_index()
    if px.index.tzinfo is not None:
        px.index = px.index.tz_localize(None)
    return px


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------
def compute_funding_zscore(funding_daily: pd.DataFrame, L: int) -> pd.DataFrame:
    """
    Annualize daily funding, then compute rolling z-score over L days (causal).
    funding_ann = daily_funding * 365.
    z = (funding_ann - roll_mean_L) / roll_std_L
    No look-ahead: at time t, uses only data up to and including t.
    """
    funding_ann = funding_daily * 365.0
    roll_mean = funding_ann.rolling(L, min_periods=L // 2).mean()
    roll_std = funding_ann.rolling(L, min_periods=L // 2).std()
    z = (funding_ann - roll_mean) / roll_std.replace(0, np.nan)
    return z


def weights_cross_sectional(z: pd.DataFrame, N: int) -> pd.DataFrame:
    """
    Market-neutral: SHORT top-N (highest z, over-leveraged longs -> expect price fall),
    LONG bottom-N (lowest z). Equal weight, dollar-neutral (net=0, gross=2).
    """
    W = pd.DataFrame(0.0, index=z.index, columns=z.columns)
    for t in z.index:
        row = z.loc[t].dropna()
        if len(row) < 2 * N:
            continue
        ranked = row.rank(ascending=True)
        n_coins = len(row)
        # bottom-N: LONG (contrarian: low funding -> expect price rise)
        long_mask = ranked <= N
        # top-N: SHORT (contrarian: high funding -> expect price fall)
        short_mask = ranked > (n_coins - N)
        w = pd.Series(0.0, index=row.index)
        w[long_mask] = 1.0 / N
        w[short_mask] = -1.0 / N
        W.loc[t, w.index] = w.values
    return W


def weights_time_series(z: pd.DataFrame, Z_thresh: float) -> pd.DataFrame:
    """
    Per-coin: weight = -1 if z > +Z_thresh (high funding, short), +1 if z < -Z_thresh (long).
    Average across coins to get portfolio weights (not dollar-neutral in general).
    """
    raw = pd.DataFrame(0.0, index=z.index, columns=z.columns)
    raw[z > Z_thresh] = -1.0   # contrarian short on high funding
    raw[z < -Z_thresh] = 1.0  # contrarian long on low/negative funding
    # replace NaN z (not enough history) with 0
    raw[z.isna()] = 0.0
    # average across coins
    n_active = (raw != 0).sum(axis=1).replace(0, np.nan)
    # normalize: each coin contributes equal weight; total gross varies
    # keep raw as-is and divide by number of coins (not active) for consistent notional
    W = raw.div(z.shape[1])   # 1/N_total per coin slot -> aggregate gross < 2
    return W


# ---------------------------------------------------------------------------
# Backtest runner: DECOMPOSE price vs funding
# ---------------------------------------------------------------------------
def run_decomposed(px: pd.DataFrame, W: pd.DataFrame, funding_daily: pd.DataFrame,
                   cost_bps: float, label: str) -> dict:
    """
    Run backtest_weights twice on same weights:
      price_only: funding=None  (pure price PnL)
      total:      funding=panel (price + funding carry)
    Returns metrics dict with both components.
    """
    coins = [c for c in W.columns if c in px.columns]
    px_sub = px[coins]
    W_sub = W[coins]

    # align funding to price index
    fnd_sub = funding_daily[coins].reindex(px_sub.index).fillna(0.0)

    # price-only run
    bt_price = q.backtest_weights(px_sub, W_sub, cost_bps=cost_bps, funding=None)
    # total run
    bt_total = q.backtest_weights(px_sub, W_sub, cost_bps=cost_bps, funding=fnd_sub)

    # funding component = total ret - price_only ret (costs already baked in price_only)
    # note: costs are identical in both runs; ret_funding in bt_total gives clean funding component
    ret_price_only = bt_price["ret_net"]
    ret_total = bt_total["ret_net"]
    ret_funding_component = bt_total["ret_funding"]  # = -(w_held * funding) summed

    m_price = q.metrics_from_returns(ret_price_only, TF)
    m_total = q.metrics_from_returns(ret_total, TF)

    # funding-only component metrics (additive contribution, no separate cost)
    funding_cagr_approx = float(ret_funding_component.mean() * 365)
    funding_vol_approx = float(ret_funding_component.std() * np.sqrt(365))

    yearly_price = q.period_breakdown(ret_price_only).to_dict("records")
    yearly_total = q.period_breakdown(ret_total).to_dict("records")

    return {
        "label": label,
        "cost_bps": cost_bps,
        "price_only": m_price,
        "total": m_total,
        "funding_component": {
            "approx_ann_mean": funding_cagr_approx,
            "approx_ann_vol": funding_vol_approx,
            "total_funding_received": float(ret_funding_component.sum()),
        },
        "yearly_price_only": yearly_price,
        "yearly_total": yearly_total,
        "bt_price": bt_price,
        "bt_total": bt_total,
        "ret_price_only": ret_price_only,
        "ret_total": ret_total,
    }


# ---------------------------------------------------------------------------
# Grid search helpers
# ---------------------------------------------------------------------------
def grid_cross_sectional(px, funding_daily, z_full, cost_bps):
    rows = []
    for N in [2, 3, 4]:
        for L in [14, 30, 60]:
            z = compute_funding_zscore(funding_daily, L)
            z_aligned = z.reindex(px.index).ffill(limit=1)
            W = weights_cross_sectional(z_aligned, N)
            coins = [c for c in W.columns if c in px.columns]
            fnd = funding_daily[coins].reindex(px.index).fillna(0.0)
            bt_p = q.backtest_weights(px[coins], W[coins], cost_bps=cost_bps, funding=None)
            bt_t = q.backtest_weights(px[coins], W[coins], cost_bps=cost_bps, funding=fnd)
            mp = q.metrics_from_returns(bt_p["ret_net"], TF)
            mt = q.metrics_from_returns(bt_t["ret_net"], TF)
            rows.append({
                "N": N, "L": L,
                "price_cagr": mp.get("cagr"), "price_sharpe": mp.get("sharpe"),
                "total_cagr": mt.get("cagr"), "total_sharpe": mt.get("sharpe"),
                "funding_component_ann": mt.get("cagr", 0) - mp.get("cagr", 0),
            })
    return pd.DataFrame(rows)


def grid_time_series(px, funding_daily, cost_bps):
    rows = []
    for Z_thresh in [1.0, 1.5, 2.0]:
        for L in [14, 30, 60]:
            z = compute_funding_zscore(funding_daily, L)
            z_aligned = z.reindex(px.index).ffill(limit=1)
            W = weights_time_series(z_aligned, Z_thresh)
            coins = [c for c in W.columns if c in px.columns]
            fnd = funding_daily[coins].reindex(px.index).fillna(0.0)
            bt_p = q.backtest_weights(px[coins], W[coins], cost_bps=cost_bps, funding=None)
            bt_t = q.backtest_weights(px[coins], W[coins], cost_bps=cost_bps, funding=fnd)
            mp = q.metrics_from_returns(bt_p["ret_net"], TF)
            mt = q.metrics_from_returns(bt_t["ret_net"], TF)
            rows.append({
                "Z": Z_thresh, "L": L,
                "price_cagr": mp.get("cagr"), "price_sharpe": mp.get("sharpe"),
                "total_cagr": mt.get("cagr"), "total_sharpe": mt.get("sharpe"),
                "funding_component_ann": mt.get("cagr", 0) - mp.get("cagr", 0),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_universe(coins: list[str], tag: str):
    print(f"\n{'='*60}")
    print(f"UNIVERSE: {tag}  ({len(coins)} coins)")
    print(f"{'='*60}")

    px = load_prices_daily(coins)
    # inner-join: only dates where ALL coins have prices
    px = px.dropna()
    avail_coins = [c for c in coins if c in px.columns]
    px = px[avail_coins]

    funding_daily = load_funding_daily(avail_coins)
    # align to price index
    funding_daily = funding_daily.reindex(px.index).fillna(0.0)

    print(f"  Coins available: {avail_coins}")
    print(f"  Date range: {px.index[0].date()} to {px.index[-1].date()}  ({len(px)} bars)")

    # z-scores with default L
    z = compute_funding_zscore(funding_daily, DEFAULT_L)
    z_aligned = z.reindex(px.index).ffill(limit=1)

    # BTC benchmark
    btc_px = px["BTC"] if "BTC" in px.columns else None

    results = {}

    # ------------------------------------------------------------------
    # VARIANT A: Cross-sectional, default params
    # ------------------------------------------------------------------
    print(f"\n--- Variant A: Cross-sectional (N={DEFAULT_N}, L={DEFAULT_L}) ---")
    W_cs = weights_cross_sectional(z_aligned, DEFAULT_N)

    for cost in [DEFAULT_COST_BPS, HIGH_COST_BPS]:
        key = f"A_cs_N{DEFAULT_N}_L{DEFAULT_L}_cost{int(cost)}"
        res = run_decomposed(px, W_cs, funding_daily, cost, key)
        results[key] = res
        mp, mt = res["price_only"], res["total"]
        print(f"  cost={cost}bps | price-only: CAGR={mp['cagr']*100:.1f}% Sh={mp['sharpe']:.2f} "
              f"| total: CAGR={mt['cagr']*100:.1f}% Sh={mt['sharpe']:.2f} "
              f"| funding contrib ann={res['funding_component']['approx_ann_mean']*100:.1f}%")

    # ------------------------------------------------------------------
    # VARIANT B: Time-series, default params
    # ------------------------------------------------------------------
    print(f"\n--- Variant B: Time-series (Z={DEFAULT_Z}, L={DEFAULT_L}) ---")
    W_ts = weights_time_series(z_aligned, DEFAULT_Z)

    for cost in [DEFAULT_COST_BPS, HIGH_COST_BPS]:
        key = f"B_ts_Z{DEFAULT_Z}_L{DEFAULT_L}_cost{int(cost)}"
        res = run_decomposed(px, W_ts, funding_daily, cost, key)
        results[key] = res
        mp, mt = res["price_only"], res["total"]
        print(f"  cost={cost}bps | price-only: CAGR={mp['cagr']*100:.1f}% Sh={mp['sharpe']:.2f} "
              f"| total: CAGR={mt['cagr']*100:.1f}% Sh={mt['sharpe']:.2f} "
              f"| funding contrib ann={res['funding_component']['approx_ann_mean']*100:.1f}%")

    # ------------------------------------------------------------------
    # BTC correlation (use default A total)
    # ------------------------------------------------------------------
    default_key = f"A_cs_N{DEFAULT_N}_L{DEFAULT_L}_cost{int(DEFAULT_COST_BPS)}"
    ret_a_total = results[default_key]["ret_total"]
    btc_ret = btc_px.pct_change().reindex(ret_a_total.index).fillna(0)
    corr_a_btc = float(ret_a_total.corr(btc_ret))

    default_b_key = f"B_ts_Z{DEFAULT_Z}_L{DEFAULT_L}_cost{int(DEFAULT_COST_BPS)}"
    ret_b_total = results[default_b_key]["ret_total"]
    corr_b_btc = float(ret_b_total.corr(btc_ret))

    print(f"\n  BTC correlation: A_total={corr_a_btc:.3f}, B_total={corr_b_btc:.3f}")

    # BTC B&H metrics
    btc_bh_ret = btc_px.pct_change().fillna(0)
    m_btc = q.metrics_from_returns(btc_bh_ret, TF)
    print(f"  BTC B&H: CAGR={m_btc['cagr']*100:.1f}% Sh={m_btc['sharpe']:.2f}")

    # ------------------------------------------------------------------
    # Sensitivity grids (5bps only for brevity)
    # ------------------------------------------------------------------
    print("\n--- Grid: Variant A cross-sectional ---")
    grid_a = grid_cross_sectional(px, funding_daily, z_aligned, DEFAULT_COST_BPS)
    print(grid_a.to_string(index=False))

    print("\n--- Grid: Variant B time-series ---")
    grid_b = grid_time_series(px, funding_daily, DEFAULT_COST_BPS)
    print(grid_b.to_string(index=False))

    # ------------------------------------------------------------------
    # Yearly breakdown for default variants
    # ------------------------------------------------------------------
    print("\n--- Yearly breakdown: Variant A (price-only) ---")
    print(pd.DataFrame(results[default_key]["yearly_price_only"]).to_string(index=False))
    print("\n--- Yearly breakdown: Variant A (total) ---")
    print(pd.DataFrame(results[default_key]["yearly_total"]).to_string(index=False))
    print("\n--- Yearly breakdown: Variant B (price-only) ---")
    print(pd.DataFrame(results[default_b_key]["yearly_price_only"]).to_string(index=False))
    print("\n--- Yearly breakdown: Variant B (total) ---")
    print(pd.DataFrame(results[default_b_key]["yearly_total"]).to_string(index=False))

    # ------------------------------------------------------------------
    # Save outputs
    # ------------------------------------------------------------------
    HERE.mkdir(parents=True, exist_ok=True)
    suffix = f"_{tag}" if tag != "full" else ""

    # results.csv — default A total returns
    bt_a = results[default_key]["bt_total"]
    bt_a_price = results[default_key]["bt_price"]
    bt_a_combined = bt_a.copy()
    bt_a_combined["ret_price_only"] = bt_a_price["ret_net"]
    bt_a_combined["equity_price_only"] = (1 + bt_a_price["ret_net"]).cumprod()
    bt_a_combined.to_csv(HERE / f"results{suffix}.csv")

    # trades.csv — daily weights for default A
    W_cs.to_csv(HERE / f"trades{suffix}.csv")

    # equity plot
    eq_total = results[default_key]["bt_total"]["equity"]
    eq_price = results[default_key]["bt_price"]["equity"]
    eq_b_total = results[default_b_key]["bt_total"]["equity"]

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(3, 1, figsize=(12, 10), gridspec_kw={"height_ratios": [3, 3, 1]})

        # top: A equity curves (price-only vs total vs BTC)
        ax = axes[0]
        ax.plot(eq_price.index, eq_price.values, label="A price-only", lw=1.3, color="steelblue")
        ax.plot(eq_total.index, eq_total.values, label="A total (price+funding)", lw=1.3, color="green")
        ax.plot(eq_b_total.index, eq_b_total.values, label="B total", lw=1.0, color="orange", alpha=0.8)
        if btc_px is not None:
            btc_norm = btc_px.reindex(eq_total.index).ffill()
            btc_norm = btc_norm / btc_norm.iloc[0]
            ax.plot(btc_norm.index, btc_norm.values, label="BTC B&H", lw=1.0, alpha=0.5, color="gray")
        ax.set_yscale("log")
        ax.legend(fontsize=9)
        ax.set_title(f"Funding Reversal — {tag} universe (default params)")
        ax.grid(alpha=0.3)

        # middle: B equity
        ax2 = axes[1]
        eq_b_price = results[default_b_key]["bt_price"]["equity"]
        ax2.plot(eq_b_price.index, eq_b_price.values, label="B price-only", lw=1.3, color="steelblue")
        ax2.plot(eq_b_total.index, eq_b_total.values, label="B total", lw=1.3, color="orange")
        ax2.set_yscale("log")
        ax2.legend(fontsize=9)
        ax2.set_title("Variant B (time-series) equity")
        ax2.grid(alpha=0.3)

        # bottom: drawdown of A total
        dd = eq_total / eq_total.cummax() - 1.0
        axes[2].fill_between(dd.index, dd.values, 0, color="red", alpha=0.4)
        axes[2].set_title("Drawdown (A total)")
        axes[2].grid(alpha=0.3)

        fig.tight_layout()
        fig.savefig(HERE / f"equity{suffix}.png", dpi=90)
        plt.close(fig)
        print(f"\n  Saved equity{suffix}.png")
    except Exception as e:
        print(f"  Plot failed: {e}")

    # metrics.json
    def safe_metrics(res):
        return {
            "price_only": res["price_only"],
            "total": res["total"],
            "funding_component": res["funding_component"],
            "yearly_price_only": res["yearly_price_only"],
            "yearly_total": res["yearly_total"],
        }

    out_metrics = {
        "strategy": "crypto_funding_reversal",
        "universe": avail_coins,
        "tag": tag,
        "date_range": f"{px.index[0].date()} to {px.index[-1].date()}",
        "n_bars": len(px),
        "default_params": {"L": DEFAULT_L, "N": DEFAULT_N, "Z": DEFAULT_Z, "cost_bps": DEFAULT_COST_BPS},
        "btc_benchmark": m_btc,
        "btc_correlation": {"A_total": corr_a_btc, "B_total": corr_b_btc},
        "variant_A_5bps": safe_metrics(results[f"A_cs_N{DEFAULT_N}_L{DEFAULT_L}_cost{int(DEFAULT_COST_BPS)}"]),
        "variant_A_10bps": safe_metrics(results[f"A_cs_N{DEFAULT_N}_L{DEFAULT_L}_cost{int(HIGH_COST_BPS)}"]),
        "variant_B_5bps": safe_metrics(results[f"B_ts_Z{DEFAULT_Z}_L{DEFAULT_L}_cost{int(DEFAULT_COST_BPS)}"]),
        "variant_B_10bps": safe_metrics(results[f"B_ts_Z{DEFAULT_Z}_L{DEFAULT_L}_cost{int(HIGH_COST_BPS)}"]),
        "grid_A": grid_a.to_dict("records"),
        "grid_B": grid_b.to_dict("records"),
    }
    q.save_metrics(HERE / f"metrics{suffix}.json", out_metrics)
    print(f"  Saved metrics{suffix}.json")

    return out_metrics


def run():
    # Primary universe (full history)
    m_full = run_universe(UNIVERSE_FULL, "full")

    # Secondary: extended universe (HYPE + ZEC have shorter history)
    # Inner-join on dates will naturally restrict to the shorter history
    m_ext = run_universe(UNIVERSE_EXTENDED, "extended")

    # Print summary comparison
    print("\n" + "="*60)
    print("FINAL SUMMARY")
    print("="*60)
    for tag, m in [("full", m_full), ("extended", m_ext)]:
        mAp = m["variant_A_5bps"]["price_only"]
        mAt = m["variant_A_5bps"]["total"]
        mBp = m["variant_B_5bps"]["price_only"]
        mBt = m["variant_B_5bps"]["total"]
        print(f"\n{tag} universe ({m['date_range']}, {m['n_bars']} bars):")
        print(f"  Variant A (cross-sectional, N=3, L=30, 5bps):")
        print(f"    Price-only : CAGR={mAp['cagr']*100:+.1f}%  Sharpe={mAp['sharpe']:.2f}  MDD={mAp['max_drawdown']*100:.1f}%")
        print(f"    Total      : CAGR={mAt['cagr']*100:+.1f}%  Sharpe={mAt['sharpe']:.2f}  MDD={mAt['max_drawdown']*100:.1f}%")
        print(f"    Funding contrib (ann mean): {m['variant_A_5bps']['funding_component']['approx_ann_mean']*100:+.1f}%")
        print(f"  Variant B (time-series, Z=1.5, L=30, 5bps):")
        print(f"    Price-only : CAGR={mBp['cagr']*100:+.1f}%  Sharpe={mBp['sharpe']:.2f}  MDD={mBp['max_drawdown']*100:.1f}%")
        print(f"    Total      : CAGR={mBt['cagr']*100:+.1f}%  Sharpe={mBt['sharpe']:.2f}  MDD={mBt['max_drawdown']*100:.1f}%")
        print(f"    Funding contrib (ann mean): {m['variant_B_5bps']['funding_component']['approx_ann_mean']*100:+.1f}%")
        print(f"  BTC B&H: CAGR={m['btc_benchmark']['cagr']*100:.1f}%  Sharpe={m['btc_benchmark']['sharpe']:.2f}")
        print(f"  BTC corr: A={m['btc_correlation']['A_total']:.3f}  B={m['btc_correlation']['B_total']:.3f}")


if __name__ == "__main__":
    run()
