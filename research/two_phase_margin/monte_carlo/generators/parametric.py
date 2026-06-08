"""
generators/parametric.py — Parametric synthetic dfs generator.

Реализуется в T3.

Model:
  Price:   GBM with calibrated (mu, sigma) + Poisson jump-diffusion for fat tails.
  Funding: AR(1)/OU process around a regime mean with Markov hot<->cold switching.
           MUST be able to produce negative funding (key anti-garbage-in requirement).
  Correlation: price-return <-> funding-rate cross-correlation respected.
  Cross-coin:  joint crash scenarios via correlated Brownian motions.

Round-trip gate (mandatory for T3 acceptance):
  Generate >= 1000 paths, re-extract statistics via calibrate_stats, confirm that
  negative-hours share, funding mean/sigma/phi, regime means, return sigma and
  tails all match the input calibration within tolerance.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def generate(
    calib: dict[str, Any],
    horizon_h: int,
    seed: int,
    coins: list[str],
) -> dict[str, pd.DataFrame]:
    """Generate a synthetic dfs dict from calibration parameters.

    Arguments:
        calib:     per-coin calibration dict (output of calibrate_stats.calibrate_all).
        horizon_h: simulation horizon in hours.
        seed:      random seed for determinism.
        coins:     list of coin symbols to generate.

    Returns:
        dict[coin → DataFrame] with columns ['close', 'fundingRate'] and an
        hourly DatetimeIndex starting at a fixed reference date.

    Реализуется в T3.
    """
    raise NotImplementedError("generators.parametric.generate реализуется в T3")
