"""XSMOM momentum-ensemble signal evaluator.

Pure functions only — no DB, no exchange. Mirrors the math in
research/cross_sectional/crypto/signals.py::momentum_ensemble exactly.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd


_DAY_MS = 86_400_000  # milliseconds in one UTC day


def compute_scores(
    closes_by_coin: dict[str, list[tuple[int, float]]],
    lookbacks: tuple[int, ...] = (14, 21, 30, 45, 60),
) -> dict[str, float]:
    """Return {coin: ensemble_score} for the most recent aligned day.

    Mirrors research momentum_ensemble: per lookback, momentum = close[t]/close[t-lb]-1,
    cross-sectional z-score across coins on that day, then mean across lookbacks.
    A coin's score is absent if ANY lookback leg is undefined (insufficient history).
    Coins with a defined ensemble score are returned; coins lacking history are omitted.

    Alignment: all coins are reindexed onto a continuous daily grid (step = DAY_MS).
    Missing candles on the grid become NaN — no forward-fill. This matches research
    behaviour where pre-listing rows are NaN (not fabricated).

    day_ms convention: close_ms as returned by HL (candle T field). All coins share
    the same HL daily grid so cross-sectional alignment is exact.
    """
    if not closes_by_coin or not lookbacks:
        return {}

    # ── Build aligned panel ──────────────────────────────────────────────────
    # Collect all unique day_ms values across all coins.
    all_days: set[int] = set()
    for rows in closes_by_coin.values():
        for day_ms, _ in rows:
            all_days.add(day_ms)

    if not all_days:
        return {}

    day_min = min(all_days)
    day_max = max(all_days)
    # Construct a regular daily grid from min to max (step = 1 day in ms).
    full_grid = np.arange(day_min, day_max + _DAY_MS, _DAY_MS, dtype=np.int64)
    grid_index = pd.Index(full_grid, name="day_ms")

    # Build a DataFrame[day_ms x coin] — NaN where a coin has no candle that day.
    series: dict[str, pd.Series] = {}
    for coin, rows in closes_by_coin.items():
        if not rows:
            continue
        idx = pd.Index([r[0] for r in rows], name="day_ms", dtype=np.int64)
        vals = pd.Series([r[1] for r in rows], index=idx, name=coin, dtype=float)
        # Reindex onto the full grid; missing entries stay NaN (no fill).
        series[coin] = vals.reindex(grid_index)

    if not series:
        return {}

    price = pd.DataFrame(series)  # (T, C)  — columns = coins

    # ── Replicate momentum_ensemble from research/signals.py ─────────────────
    # momentum(panel, lb) = price / price.shift(lb) - 1
    # zscore_cross_section: per-row standardize across coins (ddof=0)
    # ensemble = mean of z-scored legs, NaN if any leg NaN for that coin

    def _zscore_cs(df: pd.DataFrame) -> pd.DataFrame:
        mean = df.mean(axis=1)
        std = df.std(axis=1, ddof=0)
        return df.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)

    legs: list[pd.DataFrame] = []
    for lb in lookbacks:
        mom = price / price.shift(lb) - 1.0
        legs.append(_zscore_cs(mom))

    # Align (identical grid by construction, but be explicit like research).
    idx, cols = legs[0].index, legs[0].columns
    legs = [leg.reindex(index=idx, columns=cols) for leg in legs]

    arr = np.stack([leg.values for leg in legs], axis=0)  # (n_lb, T, C)
    all_present = ~np.isnan(arr).any(axis=0)              # (T, C)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean_arr = np.nanmean(arr, axis=0)
    mean_arr = np.where(all_present, mean_arr, np.nan)

    ensemble = pd.DataFrame(mean_arr, index=idx, columns=cols)

    # ── Extract most recent row with at least one defined score ──────────────
    # Drop rows that are entirely NaN (no coin has a score on that day).
    defined_rows = ensemble.dropna(how="all")
    if defined_rows.empty:
        return {}

    latest_row = defined_rows.iloc[-1]
    # Return only coins with a finite (non-NaN) score.
    return {
        coin: float(score)
        for coin, score in latest_row.items()
        if pd.notna(score)
    }
