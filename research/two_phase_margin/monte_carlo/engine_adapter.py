"""
engine_adapter.py — Thin wrapper around research/two_phase_margin.py::simulate().

Реализуется в T1.

Design rules (see PLAN.md §Architecture):
- Load the engine STRICTLY by file path via importlib.util.spec_from_file_location
  to avoid the two_phase_margin.py vs two_phase_margin/ package name collision
  (PLAN.md Подводный камень #2).
- NEVER reimplement any exit/margin logic — call simulate() as-is.
- Return a RunResult dataclass: equity series of deployed capital + metrics dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class RunResult:
    """Result of a single simulation run through the engine adapter."""

    equity: pd.Series  # hourly equity of deployed capital
    metrics: dict      # output of metrics.summarize(equity)
    raw: Any           # raw simulate() return value for debugging


def run_on_dfs(
    dfs: dict[str, pd.DataFrame],
    params: Any,
    mbuf: float,
    coins: list[str],
) -> RunResult:
    """Run the two_phase engine on the provided synthetic or real dfs.

    Arguments:
        dfs:    dict[coin → DataFrame[close, fundingRate, ...]] hourly-indexed.
        params: strategy parameters (same object accepted by simulate()).
        mbuf:   margin buffer multiplier (e.g. 3.0 for U-prod).
        coins:  list of coin symbols to trade.

    Returns:
        RunResult with equity series (deployed capital, hourly) and summarized
        metrics from metrics.summarize().

    Реализуется в T1.
    """
    raise NotImplementedError("engine_adapter.run_on_dfs реализуется в T1")
