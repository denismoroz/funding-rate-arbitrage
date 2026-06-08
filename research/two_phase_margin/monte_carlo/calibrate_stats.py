"""
calibrate_stats.py — Extract stylized facts from real price+funding history.

T2 implementation.

Output: calibration/{coin}.json per coin with fields documented below.
Also writes calibration/_cross_funding_corr.json with inter-coin funding
correlation matrix (on common intersection of dates).

UNIVERSE: BTC, ETH, SOL, HYPE, PURR

Data sources:
  research/data/{coin}.csv      — funding rates; cols: coin, fundingRate, time, ...
  research/data/{coin}_1h.csv   — price (hourly OHLCV or close+fundingRate):
      BTC/ETH/SOL: time, open, high, low, close, volume
      HYPE/PURR:   time, close, fundingRate

Regime definition:
  hot/cold regime is determined by a 30-day (720h) rolling mean of hourly funding.
  A given hour is "hot" if its rolling_mean > rolling_median (computed over the
  full sample), and "cold" otherwise.  This is a symmetric split (50/50 by
  construction on the full sample) that captures persistent high-funding vs
  low/negative-funding periods without arbitrary dollar thresholds.

  NOTE: For anchoring purposes, the SOL cold window 2025-01-01→2026-04-30
  corresponds to the empirically observed low-funding / negative-funding period.
  The regime_hot/cold_funding_annual_pct fields reflect this rolling-mean regime
  definition, not the hard-cutoff window.  The sanity check in tests/ uses the
  hard-cutoff cold-window anchor (~2.52% for SOL).

Sanity anchors (from research/drift/regime_comparison.csv):
  SOL: cold-window (2025-01-01→2026-04-30) annual ≈ 2.71%  [tolerance ±1.5 pp]
  BTC: cold-window annual ≈ 9.2%                            [tolerance ±2 pp]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGIME_WINDOW_H = 720       # 30-day rolling window for regime detection
JUMP_THRESHOLD_SIGMA = 5.0  # |log_return| > k*sigma counts as a jump
HOURS_PER_YEAR = 8760

# Cold-window anchor for sanity checks
COLD_WINDOW_START = "2025-01-01"
COLD_WINDOW_END = "2026-04-30"


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_funding(coin: str, data_dir: Path) -> pd.Series:
    """Load hourly funding rate series, indexed by UTC datetime.

    The raw CSV has one row per funding interval.  For HL data the interval is
    1 hour, so no aggregation is needed.  Timestamps are parsed with format='mixed'
    to handle both sub-second and whole-second ISO8601 strings.  The series is
    sorted and de-duplicated (keep last) on the time index.

    Returns a Series named 'fundingRate' with a DatetimeTZAware index (UTC).
    """
    path = data_dir / f"{coin}.csv"
    df = pd.read_csv(path, usecols=["time", "fundingRate"])
    df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    df = df.set_index("time")
    # Resample to a clean hourly grid; take mean for any sub-hour entries.
    # (Most rows are already exactly 1h apart — this just normalises edge cases.)
    s = df["fundingRate"].resample("1h").mean()
    return s.dropna()


def _load_price(coin: str, data_dir: Path) -> pd.Series:
    """Load hourly close-price series, indexed by UTC datetime.

    BTC/ETH/SOL: {coin}_1h.csv with columns time, open, high, low, close, volume
    HYPE/PURR:   {coin}_1h.csv with columns time, close, fundingRate

    Returns a Series named 'close' with a DatetimeTZAware index (UTC).
    """
    path = data_dir / f"{coin}_1h.csv"
    df = pd.read_csv(path, usecols=lambda c: c in {"time", "close"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    df = df.set_index("time")
    s = df["close"].resample("1h").last()
    return s.dropna()


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _log_returns(price: pd.Series) -> pd.Series:
    """Compute hourly log-returns: ln(P_t / P_{t-1})."""
    return np.log(price / price.shift(1)).dropna()


def _ar1_phi(series: pd.Series) -> float:
    """Estimate AR(1) coefficient via OLS: series_t = phi * series_{t-1} + eps.

    Returns phi in (-1, 1).
    """
    y = series.values[1:]
    x = series.values[:-1]
    # OLS: phi = cov(x, y) / var(x)
    if len(x) < 2:
        return float("nan")
    x_dm = x - x.mean()
    y_dm = y - y.mean()
    denom = float(np.dot(x_dm, x_dm))
    if denom == 0.0:
        return 0.0
    phi = float(np.dot(x_dm, y_dm) / denom)
    # Clip to valid AR(1) range to avoid numerical edge cases
    return float(np.clip(phi, -0.9999, 0.9999))


def _regime_stats(
    funding: pd.Series,
) -> dict[str, Any]:
    """Compute hot/cold regime statistics using a 30-day rolling mean.

    Regime label per hour:
      rolling_mean = 720h rolling mean of funding (min_periods=24 to allow
      early-window regime assignment).
      threshold = median of rolling_mean over the full sample.
      hot  if rolling_mean > threshold
      cold if rolling_mean <= threshold

    Returns dict with:
      regime_hot_funding_annual_pct
      regime_cold_funding_annual_pct
      regime_transition_freq
      regime_criterion  (documentation string)
      regime_note       (empty or short note if data is too short)
    """
    rolling = funding.rolling(REGIME_WINDOW_H, min_periods=24).mean()
    threshold = float(rolling.median())

    hot_mask = rolling > threshold
    cold_mask = ~hot_mask

    n_hot = hot_mask.sum()
    n_cold = cold_mask.sum()
    total = len(funding)

    result: dict[str, Any] = {
        "regime_criterion": (
            f"rolling_{REGIME_WINDOW_H}h_mean > median(rolling_mean)  →  hot; else cold"
        ),
    }

    if total < REGIME_WINDOW_H * 2:
        # Insufficient data for meaningful regime split
        result["regime_hot_funding_annual_pct"] = None
        result["regime_cold_funding_annual_pct"] = None
        result["regime_transition_freq"] = None
        result["regime_note"] = (
            f"Insufficient data for regime split (only {total} hours < 2×{REGIME_WINDOW_H}h window)"
        )
        return result

    hot_mean = float(funding[hot_mask].mean()) if n_hot > 0 else float("nan")
    cold_mean = float(funding[cold_mask].mean()) if n_cold > 0 else float("nan")

    result["regime_hot_funding_annual_pct"] = hot_mean * HOURS_PER_YEAR * 100
    result["regime_cold_funding_annual_pct"] = cold_mean * HOURS_PER_YEAR * 100

    # Transition frequency: fraction of consecutive-hour pairs where regime changes
    labels = hot_mask.values.astype(int)
    transitions = int(np.sum(labels[1:] != labels[:-1]))
    result["regime_transition_freq"] = transitions / (total - 1) if total > 1 else 0.0

    result["regime_note"] = ""
    return result


def _cold_window_annual_pct(funding: pd.Series) -> float:
    """Compute annualized funding on the hard-cutoff cold window (anchor check)."""
    mask = (funding.index >= COLD_WINDOW_START) & (funding.index <= COLD_WINDOW_END)
    subset = funding[mask]
    if len(subset) == 0:
        return float("nan")
    return float(subset.mean()) * HOURS_PER_YEAR * 100


# ---------------------------------------------------------------------------
# Per-coin calibration
# ---------------------------------------------------------------------------

def calibrate_coin(
    coin: str,
    data_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Compute and persist calibration statistics for a single coin.

    Arguments:
        coin:       coin symbol (e.g. 'SOL').
        data_dir:   directory containing {coin}.csv / {coin}_1h.csv files.
        output_dir: directory to write {coin}.json calibration output.

    Returns:
        calibration dict (same content as the written JSON).

    Fields written (all on HOURLY data):

    PRICE (from close of {coin}_1h.csv):
      log_return_mean_h       — mean of hourly log-returns
      log_return_std_h        — std of hourly log-returns
      excess_kurtosis         — excess kurtosis of log-returns (fat tails)
      jump_freq               — fraction of hours where |log_return| > 5*sigma
      price_history_start     — first date of price data (ISO8601 date string)
      price_history_end       — last date of price data
      price_history_note      — note for coins with short price history

    FUNDING (from fundingRate of {coin}.csv; HL rate is per-hour):
      funding_mean_h          — mean hourly funding rate
      funding_std_h           — std of hourly funding rate
      funding_ar1_phi         — AR(1) coefficient (speed of mean-reversion)
      negative_hours_share    — fraction of hours with fundingRate < 0
      funding_mean_annual_pct — funding_mean_h * 8760 * 100 (human-readable)

    REGIMES:
      regime_criterion            — text description of regime split rule
      regime_hot_funding_annual_pct
      regime_cold_funding_annual_pct
      regime_transition_freq      — fraction of hours with regime change
      regime_note                 — any caveats (e.g. insufficient data)

    ANCHOR (for sanity tests only, not used by generator):
      cold_window_funding_annual_pct  — funding mean on 2025-01-01→2026-04-30

    CORRELATIONS:
      corr_price_funding  — Pearson corr of log_return and fundingRate (inner join)
    """
    funding = _load_funding(coin, data_dir)
    price = _load_price(coin, data_dir)

    # ---- PRICE stats -------------------------------------------------------
    log_ret = _log_returns(price)

    lr_mean = float(log_ret.mean())
    lr_std = float(log_ret.std(ddof=1))

    # Excess kurtosis (Fisher definition: normal = 0)
    n = len(log_ret)
    if n >= 4:
        excess_kurt = float(log_ret.kurtosis())  # pandas uses Fisher definition
    else:
        excess_kurt = float("nan")

    # Jump frequency: fraction of |r| > JUMP_THRESHOLD_SIGMA * sigma
    if lr_std > 0 and n > 0:
        jump_freq = float((log_ret.abs() > JUMP_THRESHOLD_SIGMA * lr_std).mean())
    else:
        jump_freq = float("nan")

    price_start = str(price.index.min().date())
    price_end = str(price.index.max().date())

    # Note for HYPE/PURR which have short price history
    if price.index.min() >= pd.Timestamp("2025-01-01", tz="UTC"):
        price_history_note = (
            f"Short price history ({price_start} → {price_end}, "
            f"~{(price.index.max() - price.index.min()).days} days). "
            "Log-return stats have wide confidence intervals."
        )
    else:
        price_history_note = ""

    # ---- FUNDING stats -----------------------------------------------------
    fund_mean = float(funding.mean())
    fund_std = float(funding.std(ddof=1))
    fund_phi = _ar1_phi(funding)
    neg_share = float((funding < 0).mean())
    fund_annual = fund_mean * HOURS_PER_YEAR * 100

    # ---- REGIME stats ------------------------------------------------------
    regime = _regime_stats(funding)

    # ---- ANCHOR (cold-window hard-cutoff) ---------------------------------
    cold_window_annual = _cold_window_annual_pct(funding)

    # ---- CORRELATIONS (within-coin: log_return vs fundingRate) ------------
    # Align on common hourly timestamps
    aligned = pd.DataFrame({"log_return": log_ret, "funding": funding}).dropna()
    if len(aligned) >= 10:
        corr_price_funding = float(aligned["log_return"].corr(aligned["funding"]))
    else:
        corr_price_funding = float("nan")

    # ---- Assemble output dict ---------------------------------------------
    calib: dict[str, Any] = {
        # PRICE
        "log_return_mean_h": lr_mean,
        "log_return_std_h": lr_std,
        "excess_kurtosis": excess_kurt,
        "jump_freq": jump_freq,
        "price_history_start": price_start,
        "price_history_end": price_end,
        "price_history_note": price_history_note,
        # FUNDING
        "funding_mean_h": fund_mean,
        "funding_std_h": fund_std,
        "funding_ar1_phi": fund_phi,
        "negative_hours_share": neg_share,
        "funding_mean_annual_pct": fund_annual,
        # REGIME
        "regime_criterion": regime["regime_criterion"],
        "regime_hot_funding_annual_pct": regime["regime_hot_funding_annual_pct"],
        "regime_cold_funding_annual_pct": regime["regime_cold_funding_annual_pct"],
        "regime_transition_freq": regime["regime_transition_freq"],
        "regime_note": regime["regime_note"],
        # ANCHOR
        "cold_window_funding_annual_pct": cold_window_annual,
        # CORRELATION
        "corr_price_funding": corr_price_funding,
    }

    # ---- Write JSON --------------------------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{coin}.json"
    with open(out_path, "w") as f:
        json.dump(calib, f, indent=2)

    return calib


# ---------------------------------------------------------------------------
# Cross-coin funding correlations
# ---------------------------------------------------------------------------

def _cross_funding_corr(
    coins: list[str],
    data_dir: Path,
) -> dict[str, Any]:
    """Compute pairwise Pearson correlations of hourly funding rates.

    Uses the intersection of dates common to all coins.

    Returns a dict with:
      coins: list of coin names (in order)
      matrix: list-of-lists correlation matrix (same order as coins)
      common_start: first date of common intersection
      common_end:   last date of common intersection
      common_hours: number of overlapping hours
    """
    series_map: dict[str, pd.Series] = {}
    for coin in coins:
        series_map[coin] = _load_funding(coin, data_dir)

    # Align on common index
    df = pd.concat(series_map, axis=1).dropna()
    df.columns = coins

    matrix = df.corr().values.tolist()

    return {
        "coins": coins,
        "matrix": matrix,
        "common_start": str(df.index.min().date()) if len(df) > 0 else None,
        "common_end": str(df.index.max().date()) if len(df) > 0 else None,
        "common_hours": int(len(df)),
    }


# ---------------------------------------------------------------------------
# Calibrate all coins
# ---------------------------------------------------------------------------

def calibrate_all(
    coins: list[str],
    data_dir: Path,
    output_dir: Path,
) -> dict[str, dict]:
    """Calibrate all coins and write per-coin JSON files.

    Also writes calibration/_cross_funding_corr.json with the inter-coin
    funding correlation matrix on the common date intersection.

    Arguments:
        coins:      list of coin symbols to calibrate.
        data_dir:   directory with {coin}.csv and {coin}_1h.csv files.
        output_dir: directory to write output JSON files.

    Returns:
        dict mapping coin → calibration dict.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}
    for coin in coins:
        calib = calibrate_coin(coin, data_dir, output_dir)
        results[coin] = calib

    # Cross-coin funding correlation matrix
    cross = _cross_funding_corr(coins, data_dir)
    cross_path = output_dir / "_cross_funding_corr.json"
    with open(cross_path, "w") as f:
        json.dump(cross, f, indent=2)

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _print_summary_table(results: dict[str, dict]) -> None:
    """Print a human-readable summary table of key calibration statistics."""
    header = (
        f"{'Coin':<6} | {'Fund% ann':>10} | {'Neg hrs%':>8} | {'AR1 phi':>8} | "
        f"{'Hot ann%':>9} | {'Cold ann%':>10} | {'Price start':>11} | {'Ex Kurt':>8}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for coin, c in results.items():
        neg_pct = c["negative_hours_share"] * 100
        hot = c.get("regime_hot_funding_annual_pct")
        cold = c.get("regime_cold_funding_annual_pct")
        hot_s = f"{hot:9.2f}" if hot is not None else f"{'N/A':>9}"
        cold_s = f"{cold:10.2f}" if cold is not None else f"{'N/A':>10}"
        print(
            f"{coin:<6} | {c['funding_mean_annual_pct']:10.2f} | {neg_pct:7.2f}% | "
            f"{c['funding_ar1_phi']:8.4f} | {hot_s} | {cold_s} | "
            f"{c['price_history_start']:>11} | {c['excess_kurtosis']:8.2f}"
        )
    print(sep)


if __name__ == "__main__":
    # file is research/two_phase_margin/monte_carlo/calibrate_stats.py →
    # parents[2] = research/, so research/data is parents[2]/"data".
    DATA_DIR = Path(__file__).resolve().parents[2] / "data"
    OUTPUT_DIR = Path(__file__).resolve().parent / "calibration"

    COINS = ["BTC", "ETH", "SOL", "HYPE", "PURR"]

    print(f"Calibrating {len(COINS)} coins from {DATA_DIR} ...")
    results = calibrate_all(COINS, DATA_DIR, OUTPUT_DIR)

    print(f"\nWrote {len(COINS)} JSON files + _cross_funding_corr.json → {OUTPUT_DIR}")
    print()
    _print_summary_table(results)

    # Sanity anchors
    print()
    print("=== Sanity anchors ===")
    sol_cold = results["SOL"]["cold_window_funding_annual_pct"]
    btc_cold = results["BTC"]["cold_window_funding_annual_pct"]
    sol_diff = abs(sol_cold - 2.708)
    btc_diff = abs(btc_cold - 9.202)
    sol_ok = sol_diff <= 1.5
    btc_ok = btc_diff <= 2.0
    print(f"SOL cold-window (2025-01→2026-04): {sol_cold:.4f}%  anchor 2.708%  diff {sol_diff:.4f}pp  {'OK' if sol_ok else 'FAIL'}")
    print(f"BTC cold-window (2025-01→2026-04): {btc_cold:.4f}%  anchor 9.202%  diff {btc_diff:.4f}pp  {'OK' if btc_ok else 'FAIL'}")
