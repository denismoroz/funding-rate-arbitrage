"""
generators/bootstrap.py — Stationary block bootstrap generator.

Реализуется в T4.

Method: circular / stationary block bootstrap on JOINT (price + funding, all coins
simultaneously) hourly blocks — preserving cross-coin correlations and
autocorrelation structure of real data.  No parametric distribution assumptions.

Acceptance criteria (T4):
  - Marginal distributions and ACF of funding preserved (check lags 1..24).
  - Blocks are drawn synchronously across all coins.
  - Deterministic for a fixed seed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def generate(
    real_dfs: dict[str, pd.DataFrame],
    horizon_h: int,
    seed: int,
    coins: list[str],
) -> dict[str, pd.DataFrame]:
    """Generate a bootstrap-resampled synthetic dfs dict.

    Arguments:
        real_dfs:  real history dfs (same format as engine_adapter input).
        horizon_h: target horizon in hours.
        seed:      random seed for determinism.
        coins:     list of coin symbols.

    Returns:
        dict[coin → DataFrame] with columns ['close', 'fundingRate'] and hourly index.

    Реализуется в T4.
    """
    raise NotImplementedError("generators.bootstrap.generate реализуется в T4")
