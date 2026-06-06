"""
Crypto Variance / Volatility Risk Premium (VRP) Backtest
=========================================================
Short-vol proxy via the vol-swap framework:
  P&L_t = (IV_t - RV_fwd_{t→t+30}) × VEGA_SCALE − COST

See strategy_description.md and README.md for full methodology.

Run:
    source .venv/bin/activate
    python research/quant/crypto_vol_risk_premium/backtest.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[3]
QUANT = REPO / "research" / "quant"
DATA_DERIBIT = QUANT / "data_deribit"
OUT = QUANT / "crypto_vol_risk_premium"

sys.path.insert(0, str(QUANT))
from qutil import metrics_from_returns, period_breakdown, save_metrics, equity_plot

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
H = 30          # horizon in calendar days (DVOL is a 30-day index)
COST_VPTS = 2.0 / 100.0   # default round-trip cost in annualised vol units (decimal)
ANN_FACTOR = np.sqrt(365)  # for daily log-return → annualised vol

# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------

def load_coin(ticker: str) -> pd.DataFrame:
    """Load and inner-join DVOL + price for one coin. Returns daily UTC-indexed DF."""
    dvol_fp = DATA_DERIBIT / f"{ticker}_dvol.csv"
    price_fp = DATA_DERIBIT / f"{ticker}_price.csv"

    dvol = pd.read_csv(dvol_fp, parse_dates=["time"]).rename(columns={"time": "date"})
    dvol["date"] = pd.to_datetime(dvol["date"], utc=True).dt.normalize()
    dvol = dvol.set_index("date")[["dvol"]].sort_index()

    price_col = "BTC-USD" if ticker == "BTC" else "ETH-USD"
    px = pd.read_csv(price_fp, parse_dates=["Date"]).rename(columns={"Date": "date"})
    px["date"] = pd.to_datetime(px["date"], utc=True)
    px = px.set_index("date")[[price_col]].rename(columns={price_col: "price"}).sort_index()

    df = dvol.join(px, how="inner").dropna()
    df["iv"] = df["dvol"] / 100.0  # annualised decimal vol (e.g. 0.59)
    return df


def compute_rv_fwd(prices: pd.Series, h: int = H) -> pd.Series:
    """Forward realised vol: std of next-h daily log-returns × sqrt(365).
    Uses FUTURE prices — correct because the P&L of a position opened at t
    is only known at t+h.  NO look-ahead in the SIGNAL (signal uses IV_t only).
    """
    log_ret = np.log(prices / prices.shift(1))
    # rolling std over the NEXT h days = shift the rolling backwards
    rv_fwd = log_ret.rolling(h).std().shift(-h) * ANN_FACTOR
    return rv_fwd


def compute_rv_trailing(prices: pd.Series, h: int = H) -> pd.Series:
    """Trailing realised vol: std of last-h daily log-returns × sqrt(365)."""
    log_ret = np.log(prices / prices.shift(1))
    return log_ret.rolling(h).std() * ANN_FACTOR


# ---------------------------------------------------------------------------
# 2. Tranche engine (non-overlapping)
# ---------------------------------------------------------------------------

def build_tranches(df: pd.DataFrame, cost_vpts: float = COST_VPTS,
                   max_gap_days: int = 60) -> pd.DataFrame:
    """Generate non-overlapping 30-day short-vol tranches.

    Returns a DataFrame with one row per tranche:
        entry_date, exit_date, iv, rv_fwd, vrp_raw, cost, pnl_raw
    pnl_raw is before scaling (per unit of vega notional).

    max_gap_days: if the nearest available exit date is >max_gap_days from entry
    (e.g. due to a DVOL data gap), skip this entry and advance by H days to find
    the next valid window.  This prevents assigning 6-month RV_fwd to a 30-day
    tranche during the Deribit DVOL data gap (Dec 2022 – Jun 2023).
    """
    rows = []
    dates = df.index.tolist()
    i = 0
    while i < len(dates):
        entry_date = dates[i]
        iv = df.loc[entry_date, "iv"]
        rv_fwd = df.loc[entry_date, "rv_fwd"]  # may be NaN near end
        if pd.isna(rv_fwd) or pd.isna(iv):
            i += 1
            continue
        vrp_raw = iv - rv_fwd
        pnl_raw = vrp_raw - cost_vpts  # short-vol earns IV, pays RV_fwd + cost

        # find exit date ~ H calendar days later
        exit_target = entry_date + pd.Timedelta(days=H)
        future_dates = [d for d in dates if d >= exit_target]
        if not future_dates:
            break
        exit_date = future_dates[0]

        # Guard: skip tranche if exit is too far (data gap — the rv_fwd used
        # a window that spans the gap, making it a multi-month RV, not 30-day)
        gap_days = (exit_date - entry_date).days
        if gap_days > max_gap_days:
            # advance past the gap to the exit date and try again
            try:
                i = dates.index(exit_date)
            except ValueError:
                break
            continue

        rows.append({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "iv": iv,
            "rv_fwd": rv_fwd,
            "vrp_raw": vrp_raw,
            "cost": cost_vpts,
            "pnl_raw": pnl_raw,
        })
        # jump to next non-overlapping window
        try:
            i = dates.index(exit_date)
        except ValueError:
            break
    return pd.DataFrame(rows)


def calibrate_vega_scale(tranches: pd.DataFrame, target_ann_vol: float = 0.15) -> float:
    """Scale VEGA_SCALE so the unlevered tranche annual vol ≈ target_ann_vol.

    Tranches are ~30 days; annualise by multiplying per-period std by sqrt(12).
    """
    std_per_tranche = tranches["pnl_raw"].std(ddof=1)
    # tranches per year ≈ 365/30 ≈ 12.17
    tranches_per_year = 365.0 / H
    ann_std_per_unit = std_per_tranche * np.sqrt(tranches_per_year)
    if ann_std_per_unit == 0:
        return 1.0
    vega_scale = target_ann_vol / ann_std_per_unit
    return float(vega_scale)


def scale_tranches(tranches: pd.DataFrame, vega_scale: float) -> pd.DataFrame:
    """Apply vega_scale to produce actual per-tranche returns (fraction of capital)."""
    t = tranches.copy()
    t["pnl"] = t["pnl_raw"] * vega_scale
    t["return"] = t["pnl"]  # return on capital per tranche
    return t


# ---------------------------------------------------------------------------
# 3. Convert tranches → daily equity (non-overlapping & laddered)
# ---------------------------------------------------------------------------

def tranches_to_daily(tranches: pd.DataFrame, all_dates: pd.DatetimeIndex) -> pd.Series:
    """Map non-overlapping tranche returns to a daily return series.

    The return is assigned to the exit date of each tranche.
    Between tranches the strategy earns 0 (cash).
    """
    ret = pd.Series(0.0, index=all_dates, name="ret")
    for _, row in tranches.iterrows():
        exit_d = row["exit_date"]
        if exit_d in ret.index:
            ret.loc[exit_d] = row["return"]
    return ret


def build_laddered_daily(df: pd.DataFrame, vega_scale: float,
                          cost_vpts: float = COST_VPTS) -> pd.Series:
    """Laddered (overlapping) daily-return series.

    Each calendar day we open 1/30 of a new 30-day tranche.
    At any point 30 overlapping tranches are live; their blended P&L per day is the
    average of (IV_entry_k - RV_fwd_k) for k in [t-29..t].  We know RV_fwd only
    with a 30-day lag, so we assign the realised P&L of each sub-tranche to its
    EXIT day — exactly like the non-overlapping version but with 1/30 weight.

    Implementation: build a daily-granularity series where we spread each
    non-overlapping tranche's return uniformly over its H days (smooth approximation).
    For headline stats we use the non-overlapping version; this gives the equity curve.
    """
    log_ret = np.log(df["price"] / df["price"].shift(1))
    # forward realised vol: std of next-H daily log-returns (annualised)
    rv_fwd_daily = log_ret.rolling(H).std().shift(-H) * ANN_FACTOR
    # IV at each day
    iv_d = df["iv"]
    # 30-sub-tranche blend: average over [t-29..t] of (iv_{t-k} - rv_fwd_{t-k})
    # = rolling mean of iv minus rolling mean of rv_fwd
    iv_avg = iv_d.rolling(H, min_periods=H).mean()
    rv_avg = rv_fwd_daily.rolling(H, min_periods=H).mean().shift(H)  # shift back: past rv_fwd
    # daily P&L = (iv_avg - rv_avg - cost) * vega_scale / H
    # We already have rv_fwd_daily aligned to entry date; averaging past H rv_fwd values
    # is a smooth approximation of the laddered outcome.
    daily_pnl = (iv_avg - rv_avg - cost_vpts) * (vega_scale / H)
    daily_pnl = daily_pnl.dropna()
    return daily_pnl.rename("ret_laddered")


# ---------------------------------------------------------------------------
# 4. Conditional filter
# ---------------------------------------------------------------------------

def apply_conditional(tranches: pd.DataFrame, df: pd.DataFrame,
                       thresh: float = 0.05) -> pd.DataFrame:
    """Keep a tranche only if IV_t - RV_trailing_t > thresh (signal is positive)."""
    # add trailing RV to the tranche table (looked up at entry_date)
    filtered = []
    for _, row in tranches.iterrows():
        ed = row["entry_date"]
        if ed in df.index:
            rv_trail = df.loc[ed, "rv_trailing"]
            signal = row["iv"] - rv_trail
            if signal > thresh:
                filtered.append(row)
    return pd.DataFrame(filtered).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Size-scaled variant
# ---------------------------------------------------------------------------

def apply_size_scaling(tranches: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """Scale each tranche's return by the IV percentile at entry (size ∝ rank)."""
    t = tranches.copy()
    # compute rolling percentile of IV at time of entry
    iv_series = df["iv"]
    pct = t["entry_date"].apply(
        lambda d: (iv_series[iv_series.index <= d] <= iv_series.get(d, np.nan)).mean()
        if d in iv_series.index else np.nan
    )
    # normalise so average scale = 1 (preserves same risk budget)
    pct = pct / pct.mean()
    t["pnl"] = t["pnl"] * pct.values
    t["return"] = t["pnl"]
    return t


# ---------------------------------------------------------------------------
# 6. Metric helpers
# ---------------------------------------------------------------------------

def tranche_stats(tranches: pd.DataFrame) -> dict:
    """Per-tranche statistics."""
    if len(tranches) == 0:
        return {}
    r = tranches["return"]
    worst_idx = r.idxmin()
    best_idx = r.idxmax()
    return {
        "n_tranches": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "worst_return": float(r.min()),
        "worst_date": str(tranches.loc[worst_idx, "entry_date"].date()),
        "best_return": float(r.max()),
        "best_date": str(tranches.loc[best_idx, "entry_date"].date()),
        "mean_vrp_raw": float(tranches["vrp_raw"].mean()),
        "mean_iv": float(tranches["iv"].mean()),
        "mean_rv_fwd": float(tranches["rv_fwd"].mean()),
    }


def vrp_by_year(tranches: pd.DataFrame) -> pd.DataFrame:
    """Mean VRP (raw, before scaling) by calendar year of entry."""
    t = tranches.copy()
    t["year"] = pd.to_datetime(t["entry_date"]).dt.year
    g = t.groupby("year").agg(
        n=("vrp_raw", "count"),
        mean_vrp=("vrp_raw", "mean"),
        mean_iv=("iv", "mean"),
        mean_rv_fwd=("rv_fwd", "mean"),
        win_rate=("return", lambda x: (x > 0).mean()),
    ).reset_index()
    return g


# ---------------------------------------------------------------------------
# 7. Additivity test
# ---------------------------------------------------------------------------

def load_carry_daily() -> pd.Series:
    """Load carry returns from crypto_funding_carry, resampled to daily."""
    fp = QUANT / "crypto_funding_carry" / "results_funding_plus_staking.csv"
    df = pd.read_csv(fp)
    df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601").dt.normalize()
    df = df.set_index("time").sort_index()
    # sum intraday bars → daily
    carry_daily = df["ret_total"].resample("1D").sum()
    carry_daily = carry_daily[carry_daily != 0]  # drop zero-fill days at edges
    return carry_daily.rename("carry")


def load_trend_daily() -> pd.Series:
    """Load trend returns from crypto_trend_carry_blend results.csv."""
    fp = QUANT / "crypto_trend_carry_blend" / "results.csv"
    df = pd.read_csv(fp)
    df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601").dt.normalize()
    df = df.set_index("time").sort_index()
    # 'trend' column is already a daily return; resample sums same-day duplicates
    trend = df["trend"].resample("1D").sum()
    trend = trend[trend.index >= pd.Timestamp("2023-06-01", tz="UTC")]
    return trend.rename("trend")


def correlation_analysis(vrp_daily: pd.Series, carry_daily: pd.Series,
                          trend_daily: pd.Series) -> dict:
    """Monthly-resample correlation of VRP vs carry and trend."""
    # resample all to monthly
    vrp_m = (1 + vrp_daily).resample("ME").prod() - 1
    carry_m = (1 + carry_daily).resample("ME").prod() - 1
    trend_m = (1 + trend_daily).resample("ME").prod() - 1

    combined = pd.DataFrame({
        "vrp": vrp_m,
        "carry": carry_m,
        "trend": trend_m,
    }).dropna()

    if len(combined) < 4:
        return {"note": "insufficient overlap for correlation"}

    corr = combined.corr()
    return {
        "vrp_vs_carry": float(corr.loc["vrp", "carry"]),
        "vrp_vs_trend": float(corr.loc["vrp", "trend"]),
        "carry_vs_trend": float(corr.loc["carry", "trend"]),
        "n_months": int(len(combined)),
        "corr_matrix": corr.to_dict(),
    }


def blend_strategies(vrp_daily: pd.Series, carry_daily: pd.Series,
                     trend_daily: pd.Series) -> dict:
    """Equal-risk 3-way blend vs 2-way carry+trend blend."""
    # align to common index
    panel = pd.DataFrame({
        "vrp": vrp_daily,
        "carry": carry_daily,
        "trend": trend_daily,
    }).dropna()

    if len(panel) < 30:
        return {"note": "insufficient overlap for blend"}

    # inverse-vol weighting (equal risk contribution)
    vols = panel.std()
    inv_vol = 1.0 / vols
    inv_vol = inv_vol / inv_vol.sum()

    # 3-way blend
    ret3 = panel @ inv_vol
    m3 = metrics_from_returns(ret3, "1d")

    # 2-way carry+trend blend (no VRP)
    panel2 = panel[["carry", "trend"]]
    vols2 = panel2.std()
    inv_vol2 = 1.0 / vols2 / (1.0 / vols2).sum()
    ret2 = panel2 @ inv_vol2
    m2 = metrics_from_returns(ret2, "1d")

    return {
        "blend_3way": m3,
        "blend_2way": m2,
        "calmar_improvement": (m3.get("calmar", 0) - m2.get("calmar", 0)),
        "sharpe_improvement": (m3.get("sharpe", 0) - m2.get("sharpe", 0)),
        "inv_vol_weights": inv_vol.to_dict(),
    }


# ---------------------------------------------------------------------------
# 8. Main
# ---------------------------------------------------------------------------

def run_variant(name: str, tranches: pd.DataFrame, all_dates: pd.DatetimeIndex,
                df_coin: pd.DataFrame = None) -> dict:
    """Run a complete variant: stats + daily return series."""
    if len(tranches) == 0:
        return {"name": name, "n_tranches": 0}

    ret_daily = tranches_to_daily(tranches, all_dates)
    m = metrics_from_returns(ret_daily, "1d")
    ts = tranche_stats(tranches)
    bd = period_breakdown(ret_daily)

    return {
        "name": name,
        "metrics": m,
        "tranche_stats": ts,
        "yearly": bd.to_dict(orient="records"),
        "ret_daily": ret_daily,
        "tranches": tranches,
    }


def main():
    print("Loading data...")
    btc = load_coin("BTC")
    eth = load_coin("ETH")

    # forward RV (uses future — correct for P&L attribution)
    btc["rv_fwd"] = compute_rv_fwd(btc["price"])
    eth["rv_fwd"] = compute_rv_fwd(eth["price"])

    # trailing RV (for conditional signal — no look-ahead)
    btc["rv_trailing"] = compute_rv_trailing(btc["price"])
    eth["rv_trailing"] = compute_rv_trailing(eth["price"])

    print(f"BTC data: {btc.index[0].date()} → {btc.index[-1].date()}, {len(btc)} rows")
    print(f"ETH data: {eth.index[0].date()} → {eth.index[-1].date()}, {len(eth)} rows")

    # ----------------------------------------------------------------
    # Cost sensitivity first
    # ----------------------------------------------------------------
    cost_grid = [0.0, COST_VPTS, 4.0 / 100.0]
    cost_sensitivity = {}
    for cost in cost_grid:
        tr_btc = build_tranches(btc.dropna(subset=["rv_fwd"]), cost_vpts=cost)
        vs = calibrate_vega_scale(tr_btc)
        tr_btc = scale_tranches(tr_btc, vs)
        ret_d = tranches_to_daily(tr_btc, btc.index)
        m = metrics_from_returns(ret_d, "1d")
        cost_sensitivity[f"cost_{int(cost*100)}vpts"] = {
            "cagr": m.get("cagr"), "sharpe": m.get("sharpe"),
            "max_drawdown": m.get("max_drawdown"), "calmar": m.get("calmar"),
        }
    print("Cost sensitivity computed.")

    # ----------------------------------------------------------------
    # Calibrate VEGA_SCALE on BTC plain (canonical reference)
    # ----------------------------------------------------------------
    btc_clean = btc.dropna(subset=["rv_fwd"])
    tr_btc_raw = build_tranches(btc_clean)
    VEGA_SCALE_BTC = calibrate_vega_scale(tr_btc_raw)
    print(f"BTC VEGA_SCALE = {VEGA_SCALE_BTC:.4f}  (implied leverage = {VEGA_SCALE_BTC:.2f}×)")

    eth_clean = eth.dropna(subset=["rv_fwd"])
    tr_eth_raw = build_tranches(eth_clean)
    VEGA_SCALE_ETH = calibrate_vega_scale(tr_eth_raw)
    print(f"ETH VEGA_SCALE = {VEGA_SCALE_ETH:.4f}")

    # ----------------------------------------------------------------
    # VARIANT 1: BTC plain short vol
    # ----------------------------------------------------------------
    tr_btc = scale_tranches(tr_btc_raw, VEGA_SCALE_BTC)
    v1 = run_variant("BTC_plain", tr_btc, btc.index)
    print(f"V1 BTC plain: CAGR={v1['metrics']['cagr']:.1%}, Sharpe={v1['metrics']['sharpe']:.2f}, MDD={v1['metrics']['max_drawdown']:.1%}")

    # ----------------------------------------------------------------
    # VARIANT 2: ETH plain short vol
    # ----------------------------------------------------------------
    tr_eth = scale_tranches(tr_eth_raw, VEGA_SCALE_ETH)
    v2 = run_variant("ETH_plain", tr_eth, eth.index)
    print(f"V2 ETH plain: CAGR={v2['metrics']['cagr']:.1%}, Sharpe={v2['metrics']['sharpe']:.2f}, MDD={v2['metrics']['max_drawdown']:.1%}")

    # ----------------------------------------------------------------
    # VARIANT 3: Conditional (BTC, thresh grid)
    # ----------------------------------------------------------------
    conditional_results = {}
    for thresh in [0.0, 0.05, 0.10]:
        tr_cond = apply_conditional(tr_btc_raw, btc_clean, thresh=thresh)
        if len(tr_cond) == 0:
            conditional_results[f"thresh_{thresh}"] = {"n_tranches": 0}
            continue
        tr_cond = scale_tranches(tr_cond, VEGA_SCALE_BTC)
        ret_d = tranches_to_daily(tr_cond, btc.index)
        m = metrics_from_returns(ret_d, "1d")
        ts = tranche_stats(tr_cond)
        conditional_results[f"thresh_{thresh}"] = {
            "metrics": m, "tranche_stats": ts,
            "n_kept": len(tr_cond), "n_total": len(tr_btc_raw),
        }
        print(f"V3 Cond thresh={thresh}: CAGR={m.get('cagr',0):.1%}, "
              f"Sharpe={m.get('sharpe',0):.2f}, MDD={m.get('max_drawdown',0):.1%}, "
              f"N={len(tr_cond)}/{len(tr_btc_raw)}")

    # ----------------------------------------------------------------
    # VARIANT 4: Size-scaled BTC
    # ----------------------------------------------------------------
    tr_btc_sz = apply_size_scaling(scale_tranches(tr_btc_raw, VEGA_SCALE_BTC), btc_clean)
    ret_d_sz = tranches_to_daily(tr_btc_sz, btc.index)
    m_sz = metrics_from_returns(ret_d_sz, "1d")
    ts_sz = tranche_stats(tr_btc_sz)
    v4 = {"name": "BTC_size_scaled", "metrics": m_sz, "tranche_stats": ts_sz,
          "ret_daily": ret_d_sz, "tranches": tr_btc_sz}
    print(f"V4 Size-scaled: CAGR={m_sz.get('cagr',0):.1%}, Sharpe={m_sz.get('sharpe',0):.2f}")

    # ----------------------------------------------------------------
    # VARIANT 5: 50/50 BTC+ETH basket
    # ----------------------------------------------------------------
    # Align tranches to common dates
    all_dates_basket = btc.index.union(eth.index)
    ret_btc_d = tranches_to_daily(tr_btc, btc.index).reindex(all_dates_basket, fill_value=0.0)
    ret_eth_d = tranches_to_daily(tr_eth, eth.index).reindex(all_dates_basket, fill_value=0.0)
    ret_basket = (ret_btc_d + ret_eth_d) / 2.0
    m_basket = metrics_from_returns(ret_basket, "1d")
    ts_basket = {
        "btc_tranche_stats": tranche_stats(tr_btc),
        "eth_tranche_stats": tranche_stats(tr_eth),
    }
    v5 = {"name": "BTC_ETH_50_50", "metrics": m_basket, "tranche_stats": ts_basket,
          "ret_daily": ret_basket}
    print(f"V5 Basket: CAGR={m_basket.get('cagr',0):.1%}, Sharpe={m_basket.get('sharpe',0):.2f}")

    # ----------------------------------------------------------------
    # Laddered equity curve (BTC)
    # ----------------------------------------------------------------
    ret_ladder = build_laddered_daily(btc_clean, VEGA_SCALE_BTC)
    m_ladder = metrics_from_returns(ret_ladder, "1d")
    print(f"Laddered BTC: CAGR={m_ladder.get('cagr',0):.1%}, Sharpe={m_ladder.get('sharpe',0):.2f}")

    # ----------------------------------------------------------------
    # VRP persistence by year
    # ----------------------------------------------------------------
    vrp_yr_btc = vrp_by_year(tr_btc)
    vrp_yr_eth = vrp_by_year(tr_eth)
    print("\nVRP by year (BTC, raw decimal):")
    print(vrp_yr_btc.to_string(index=False))
    print("\nVRP by year (ETH, raw decimal):")
    print(vrp_yr_eth.to_string(index=False))

    # ----------------------------------------------------------------
    # Additivity test
    # ----------------------------------------------------------------
    print("\nLoading carry/trend for additivity test...")
    try:
        carry_d = load_carry_daily()
        trend_d = load_trend_daily()

        # Use BTC plain VRP daily returns
        vrp_daily_ref = v1["ret_daily"]

        corr_result = correlation_analysis(vrp_daily_ref, carry_d, trend_d)
        blend_result = blend_strategies(vrp_daily_ref, carry_d, trend_d)
        print(f"VRP vs carry corr: {corr_result.get('vrp_vs_carry', 'N/A'):.3f}")
        print(f"VRP vs trend corr: {corr_result.get('vrp_vs_trend', 'N/A'):.3f}")
        b3 = blend_result.get("blend_3way", {})
        b2 = blend_result.get("blend_2way", {})
        print(f"3-way blend Calmar: {b3.get('calmar', 'N/A')}")
        print(f"2-way blend Calmar: {b2.get('calmar', 'N/A')}")
    except Exception as e:
        print(f"Additivity test error: {e}")
        corr_result = {"error": str(e)}
        blend_result = {"error": str(e)}

    # ----------------------------------------------------------------
    # Save trades.csv (BTC plain)
    # ----------------------------------------------------------------
    trades_cols = ["entry_date", "exit_date", "iv", "rv_fwd", "vrp_raw", "cost", "pnl_raw", "pnl"]
    tr_btc_save = tr_btc[trades_cols].copy()
    tr_btc_save["entry_date"] = tr_btc_save["entry_date"].dt.strftime("%Y-%m-%d")
    tr_btc_save["exit_date"] = tr_btc_save["exit_date"].dt.strftime("%Y-%m-%d")
    tr_btc_save.to_csv(OUT / "trades.csv", index=False, float_format="%.6f")
    print(f"Saved trades.csv ({len(tr_btc_save)} tranches)")

    # ----------------------------------------------------------------
    # Save results.csv (daily equity: non-overlapping BTC, ETH, basket, laddered)
    # ----------------------------------------------------------------
    results = pd.DataFrame({
        "btc_plain": v1["ret_daily"],
        "eth_plain": v2["ret_daily"],
        "basket": ret_basket,
        "btc_laddered": ret_ladder,
        "btc_size_scaled": ret_d_sz,
    }).fillna(0.0)
    # add equity columns
    for col in ["btc_plain", "eth_plain", "basket", "btc_laddered", "btc_size_scaled"]:
        results[f"eq_{col}"] = (1.0 + results[col]).cumprod()
    results.index.name = "date"
    results.to_csv(OUT / "results.csv", float_format="%.8f")
    print(f"Saved results.csv ({len(results)} rows)")

    # ----------------------------------------------------------------
    # Save metrics.json
    # ----------------------------------------------------------------
    def clean(d):
        """Make dict JSON-serialisable by converting numpy/nan."""
        if isinstance(d, dict):
            return {k: clean(v) for k, v in d.items()}
        if isinstance(d, list):
            return [clean(x) for x in d]
        if isinstance(d, (np.floating, float)):
            if np.isnan(d) or np.isinf(d):
                return None
            return float(d)
        if isinstance(d, (np.integer, int)):
            return int(d)
        if isinstance(d, pd.Timestamp):
            return str(d)
        return d

    all_metrics = {
        "variants": {
            "btc_plain": clean({
                "metrics": v1["metrics"],
                "tranche_stats": v1["tranche_stats"],
                "vega_scale": VEGA_SCALE_BTC,
                "yearly_breakdown": v1["yearly"],
            }),
            "eth_plain": clean({
                "metrics": v2["metrics"],
                "tranche_stats": v2["tranche_stats"],
                "vega_scale": VEGA_SCALE_ETH,
                "yearly_breakdown": v2["yearly"],
            }),
            "btc_eth_basket": clean({
                "metrics": m_basket,
                "tranche_stats": ts_basket,
            }),
            "btc_laddered": clean({
                "metrics": m_ladder,
            }),
            "btc_size_scaled": clean({
                "metrics": m_sz,
                "tranche_stats": ts_sz,
            }),
            "btc_conditional": clean(conditional_results),
        },
        "cost_sensitivity_btc": clean(cost_sensitivity),
        "vrp_persistence": {
            "btc_by_year": clean(vrp_yr_btc.to_dict(orient="records")),
            "eth_by_year": clean(vrp_yr_eth.to_dict(orient="records")),
        },
        "additivity": {
            "correlation": clean(corr_result),
            "blend": clean(blend_result),
        },
        "parameters": {
            "horizon_days": H,
            "default_cost_vol_pts": float(COST_VPTS * 100),
            "target_ann_vol_pct": 15.0,
        },
    }

    save_metrics(OUT / "metrics.json", all_metrics)
    print("Saved metrics.json")

    # ----------------------------------------------------------------
    # Equity plots
    # ----------------------------------------------------------------
    # BTC plain non-overlapping
    eq_btc = (1 + v1["ret_daily"]).cumprod()
    equity_plot(eq_btc, "VRP Short-Vol: BTC Plain (non-overlapping, 2-vpt cost)",
                OUT / "equity.png")

    # BTC laddered (smoother)
    eq_ladder = (1 + ret_ladder).cumprod()
    equity_plot(eq_ladder, "VRP Short-Vol: BTC Laddered (daily overlap)",
                OUT / "equity_laddered.png")

    # Basket
    eq_basket = (1 + ret_basket).cumprod()
    equity_plot(eq_basket, "VRP Short-Vol: BTC+ETH 50/50 Basket",
                OUT / "equity_basket.png")

    print("Saved equity plots")

    # ----------------------------------------------------------------
    # Print summary table
    # ----------------------------------------------------------------
    print("\n" + "="*70)
    print("VARIANT SUMMARY")
    print("="*70)
    header = f"{'Variant':<25} {'CAGR':>7} {'Sharpe':>7} {'MDD':>8} {'Calmar':>7} {'WinRate':>8} {'WorstTranche':>13}"
    print(header)
    print("-"*70)
    for vname, vdata in [
        ("BTC plain", v1),
        ("ETH plain", v2),
        ("BTC+ETH 50/50", v5),
    ]:
        m = vdata["metrics"]
        ts = vdata["tranche_stats"]
        if isinstance(ts, dict) and "btc_tranche_stats" in ts:
            ts = ts["btc_tranche_stats"]
        cagr = m.get("cagr", 0)
        sharpe = m.get("sharpe", 0)
        mdd = m.get("max_drawdown", 0)
        calmar = m.get("calmar", 0)
        wr = ts.get("win_rate", 0) if ts else 0
        worst = ts.get("worst_return", 0) if ts else 0
        worst_d = ts.get("worst_date", "") if ts else ""
        print(f"{vname:<25} {cagr:>7.1%} {sharpe:>7.2f} {mdd:>8.1%} {calmar:>7.2f} {wr:>8.1%} {worst:>7.1%} {worst_d}")

    print("\nConditional BTC (thresh grid):")
    for k, v in conditional_results.items():
        if "metrics" in v:
            m = v["metrics"]
            n = v.get("n_kept", 0)
            tot = v.get("n_total", 0)
            print(f"  {k}: CAGR={m.get('cagr',0):.1%}, Sharpe={m.get('sharpe',0):.2f}, "
                  f"MDD={m.get('max_drawdown',0):.1%}, N={n}/{tot}")

    print("\nVRP by year (BTC):")
    print(vrp_yr_btc.to_string(index=False))

    if "vrp_vs_carry" in corr_result:
        print(f"\nAdditivity: VRP-carry corr={corr_result['vrp_vs_carry']:.3f}, "
              f"VRP-trend corr={corr_result['vrp_vs_trend']:.3f}")
    if "blend_3way" in blend_result and "blend_2way" in blend_result:
        print(f"3-way Calmar={blend_result['blend_3way'].get('calmar',None)}, "
              f"2-way Calmar={blend_result['blend_2way'].get('calmar',None)}")

    print("\nDone. All outputs in:", OUT)


if __name__ == "__main__":
    main()
