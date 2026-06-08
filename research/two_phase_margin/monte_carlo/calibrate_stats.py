"""
calibrate_stats.py — Extract stylized facts from real price+funding history.

Реализуется в T2.

Output: calibration/{coin}.json per coin with:
  price:   hourly log-return mu, sigma; excess kurtosis; jump frequency (|r| > k*sigma)
  funding: mean, sigma, AR(1) phi; fraction of negative hours (MANDATORY)
  regimes: hot/cold mean funding, transition frequency
  corr:    price-return <-> funding-rate correlation
  cross:   inter-coin funding correlations (for joint generation in T3/T5)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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

    Реализуется в T2.
    """
    raise NotImplementedError("calibrate_stats.calibrate_coin реализуется в T2")


def calibrate_all(
    coins: list[str],
    data_dir: Path,
    output_dir: Path,
) -> dict[str, dict]:
    """Calibrate all coins and write per-coin JSON files.

    Also writes cross-coin funding correlation matrix.

    Реализуется в T2.
    """
    raise NotImplementedError("calibrate_stats.calibrate_all реализуется в T2")
