"""
metrics.py — Pure functions for equity-curve analysis.

Input contract: equity is a pd.Series with a datetime index (hourly timestamps)
and values representing equity of the deployed capital (not budget_cap).

All functions are side-effect-free: no file I/O, no DB access, no randomness.

Design notes:
- annualized_return: CAGR-style formula.
- max_drawdown: returned as a positive fraction (e.g. 0.11% → 0.0011).
- calmar: annualized_return / max_drawdown.  When max_drawdown == 0 returns
  float('inf') — a monotonically growing curve has infinite Calmar, which is a
  RED FLAG in practice (perfect single-path artifact), so we want it visible.
- sharpe: based on hourly simple returns (pct_change), annualised by sqrt.
- periods_per_year defaults to 8760 throughout (365 * 24 hourly periods).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def annualized_return(equity: pd.Series, periods_per_year: int = 8760) -> float:
    """Compound Annual Growth Rate from an hourly equity series.

    Formula: (end / start) ** (periods_per_year / n_periods) - 1

    Edge cases:
    - len < 2  → 0.0  (no return can be computed)
    - start <= 0 → 0.0  (undefined / meaningless)
    """
    if len(equity) < 2:
        return 0.0
    start = float(equity.iloc[0])
    end = float(equity.iloc[-1])
    if start <= 0:
        return 0.0
    n = len(equity)
    ratio = end / start
    if ratio <= 0:
        # Equity went to zero or negative — treat as total loss, but CAGR formula
        # would require a real log; return -1.0 as a worst-case sentinel.
        return -1.0
    return ratio ** (periods_per_year / n) - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """Maximum relative drawdown from a running peak, returned as a positive fraction.

    Example: peak 1100, trough 989 → (1100 - 989) / 1100 ≈ 0.1009.
    If no drawdown exists (monotonically non-decreasing) → 0.0.

    Edge cases:
    - len < 2  → 0.0
    - all values equal → 0.0
    """
    if len(equity) < 2:
        return 0.0
    values = equity.to_numpy(dtype=float)
    running_peak = np.maximum.accumulate(values)
    # Avoid division by zero where running_peak == 0
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(running_peak > 0, (running_peak - values) / running_peak, 0.0)
    return float(np.max(dd))


def calmar(equity: pd.Series, periods_per_year: int = 8760) -> float:
    """Calmar ratio: annualized_return / max_drawdown.

    When max_drawdown == 0 returns float('inf').  A monotonically growing equity
    curve has no drawdown and therefore an infinite Calmar — this is intentionally
    surfaced as inf rather than 0 because it signals a suspicious single-path
    artifact (see PLAN.md §Architecture rule 5).
    """
    ann = annualized_return(equity, periods_per_year=periods_per_year)
    mdd = max_drawdown(equity)
    if mdd == 0.0:
        return float("inf")
    return ann / mdd


def sharpe(
    equity: pd.Series,
    periods_per_year: int = 8760,
    risk_free: float = 0.0,
) -> float:
    """Annualised Sharpe ratio based on hourly simple returns.

    Formula: (mean(r) - rf_per_period) / std(r) * sqrt(periods_per_year)

    where r = equity.pct_change().dropna() (simple hourly returns).

    Edge cases:
    - len < 2  → 0.0
    - std(r) == 0 (flat or single-point series) → 0.0
    """
    if len(equity) < 2:
        return 0.0
    r = equity.pct_change().dropna()
    if len(r) == 0:
        return 0.0
    std_r = float(r.std(ddof=1))
    if std_r == 0.0 or math.isnan(std_r):
        return 0.0
    rf_per_period = risk_free / periods_per_year
    mean_r = float(r.mean())
    return (mean_r - rf_per_period) / std_r * math.sqrt(periods_per_year)


def summarize(equity: pd.Series, periods_per_year: int = 8760) -> dict:
    """Return all metrics in a single dict.

    Keys: 'annual', 'max_dd', 'calmar', 'sharpe'.
    """
    return {
        "annual": annualized_return(equity, periods_per_year=periods_per_year),
        "max_dd": max_drawdown(equity),
        "calmar": calmar(equity, periods_per_year=periods_per_year),
        "sharpe": sharpe(equity, periods_per_year=periods_per_year),
    }
