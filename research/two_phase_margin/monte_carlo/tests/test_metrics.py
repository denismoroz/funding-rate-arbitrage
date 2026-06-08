"""
test_metrics.py — Tests for metrics.py (T0 acceptance criteria).

Import strategy (PLAN.md Подводный камень #2):
  research/two_phase_margin.py (file) and research/two_phase_margin/ (directory/package)
  share the same stem.  Doing `import two_phase_margin` from inside the package would
  be ambiguous.  We avoid the collision by importing monte_carlo.metrics directly —
  when pytest runs from the repo root with `uv run pytest`, the package
  research/two_phase_margin/monte_carlo is importable as `monte_carlo` only if its
  parent is on sys.path.

  Solution used here: this module inserts research/two_phase_margin into sys.path
  (see below) so that `import monte_carlo` resolves to the package, not to the
  sibling two_phase_margin.py file.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Import resolution — add research/two_phase_margin to sys.path so that
# `monte_carlo` refers to the package, not the sibling .py file.
# ---------------------------------------------------------------------------
_RESEARCH_TPM = Path(__file__).resolve().parents[2]  # research/two_phase_margin/
if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo.metrics import (  # noqa: E402
    annualized_return,
    calmar,
    max_drawdown,
    sharpe,
    summarize,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PERIODS_PER_YEAR = 8760  # default


def _linear_equity(start: float, end: float, n_hours: int) -> pd.Series:
    """Create a linearly increasing equity series with n_hours points."""
    idx = pd.date_range("2024-01-01", periods=n_hours, freq="h")
    values = np.linspace(start, end, n_hours)
    return pd.Series(values, index=idx)


def _flat_equity(value: float, n_hours: int) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=n_hours, freq="h")
    return pd.Series(np.full(n_hours, value), index=idx)


def _v_shape_equity(
    start: float,
    peak: float,
    trough: float,
    n_hours: int,
) -> pd.Series:
    """Rise to peak, drop to trough, recover back to ~peak.

    Actual shape: [start -> peak] in first third, [peak -> trough] in second,
    [trough -> peak] in last third.
    """
    n3 = n_hours // 3
    up1 = np.linspace(start, peak, n3)
    down = np.linspace(peak, trough, n3)
    up2 = np.linspace(trough, peak, n_hours - 2 * n3)
    values = np.concatenate([up1, down, up2])
    idx = pd.date_range("2024-01-01", periods=len(values), freq="h")
    return pd.Series(values, index=idx)


# ---------------------------------------------------------------------------
# annualized_return
# ---------------------------------------------------------------------------


class TestAnnualizedReturn:
    def test_linear_growth_matches_cagr(self):
        """Linearly growing equity: CAGR should match analytical calculation."""
        n = PERIODS_PER_YEAR  # exactly 1 year
        start, end = 1000.0, 1100.0
        equity = _linear_equity(start, end, n)
        expected = (end / start) ** (PERIODS_PER_YEAR / n) - 1  # = 0.10
        result = annualized_return(equity)
        assert result == pytest.approx(expected, rel=1e-9)

    def test_multi_year_cagr(self):
        """Two years of data → CAGR should be the geometric rate."""
        n = 2 * PERIODS_PER_YEAR
        start, end = 1000.0, 1210.0  # 10% per year compounded = 21% over 2y
        equity = _linear_equity(start, end, n)
        expected = (end / start) ** (PERIODS_PER_YEAR / n) - 1
        result = annualized_return(equity)
        assert result == pytest.approx(expected, rel=1e-9)

    def test_empty_series_returns_zero(self):
        equity = pd.Series([], dtype=float)
        assert annualized_return(equity) == 0.0

    def test_single_element_returns_zero(self):
        equity = pd.Series([1000.0])
        assert annualized_return(equity) == 0.0

    def test_zero_start_returns_zero(self):
        equity = _linear_equity(0.0, 100.0, 100)
        assert annualized_return(equity) == 0.0

    def test_negative_start_returns_zero(self):
        equity = _linear_equity(-100.0, 100.0, 100)
        assert annualized_return(equity) == 0.0

    def test_flat_equity_returns_zero(self):
        equity = _flat_equity(1000.0, 100)
        assert annualized_return(equity) == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_monotonic_increase_no_drawdown(self):
        equity = _linear_equity(1000.0, 1100.0, 1000)
        assert max_drawdown(equity) == pytest.approx(0.0, abs=1e-12)

    def test_v_shape_known_depth(self):
        """V-shape with peak=1000 and trough=900 → max_dd = 0.10."""
        equity = _v_shape_equity(start=800.0, peak=1000.0, trough=900.0, n_hours=300)
        expected_dd = (1000.0 - 900.0) / 1000.0  # = 0.10
        assert max_drawdown(equity) == pytest.approx(expected_dd, rel=1e-6)

    def test_v_shape_small_drawdown(self):
        """0.11% drawdown scenario (PLAN.md anchor Calmar 114)."""
        peak = 345.0
        trough = peak * (1 - 0.0011)  # 0.11% drawdown
        equity = _v_shape_equity(start=300.0, peak=peak, trough=trough, n_hours=300)
        expected_dd = (peak - trough) / peak
        assert max_drawdown(equity) == pytest.approx(expected_dd, rel=1e-4)

    def test_empty_series_returns_zero(self):
        equity = pd.Series([], dtype=float)
        assert max_drawdown(equity) == 0.0

    def test_single_element_returns_zero(self):
        equity = pd.Series([1000.0])
        assert max_drawdown(equity) == 0.0

    def test_flat_series_returns_zero(self):
        equity = _flat_equity(500.0, 200)
        assert max_drawdown(equity) == pytest.approx(0.0, abs=1e-12)

    def test_result_is_positive(self):
        """max_drawdown must always be a non-negative value."""
        equity = _v_shape_equity(start=800.0, peak=1000.0, trough=700.0, n_hours=300)
        dd = max_drawdown(equity)
        assert dd >= 0.0
        # 30% drawdown
        assert dd == pytest.approx(0.30, rel=1e-5)

    def test_returned_as_fraction_not_percent(self):
        """Ensure returned value is in [0,1] fraction, not percentage."""
        equity = _v_shape_equity(start=800.0, peak=1000.0, trough=500.0, n_hours=300)
        dd = max_drawdown(equity)
        assert 0.0 <= dd <= 1.0


# ---------------------------------------------------------------------------
# calmar
# ---------------------------------------------------------------------------


class TestCalmar:
    def test_calmar_equals_annual_over_maxdd(self):
        """On a curve with known metrics, calmar == annual / max_dd."""
        n = PERIODS_PER_YEAR  # 1 year
        start, peak, trough = 1000.0, 1200.0, 1080.0
        # Build: rise to peak, drop to trough, end exactly at trough to make
        # calmar well-defined and deterministic.
        up = np.linspace(start, peak, n // 2)
        down = np.linspace(peak, trough, n - n // 2)
        values = np.concatenate([up, down])
        idx = pd.date_range("2024-01-01", periods=len(values), freq="h")
        equity = pd.Series(values, index=idx)

        ann = annualized_return(equity)
        mdd = max_drawdown(equity)
        cal = calmar(equity)
        assert cal == pytest.approx(ann / mdd, rel=1e-9)

    def test_calmar_inf_on_monotonic(self):
        """Monotonically growing equity → max_dd == 0 → calmar == inf."""
        equity = _linear_equity(1000.0, 1100.0, 1000)
        assert calmar(equity) == float("inf")

    def test_calmar_inf_on_flat(self):
        """Flat equity → max_dd == 0 → calmar == inf.

        Note: annualized_return is 0.0 and max_dd is 0.0, so we get inf (0/0 case
        is resolved by the max_dd==0 check, returning inf regardless of numerator).
        """
        equity = _flat_equity(1000.0, 1000)
        assert calmar(equity) == float("inf")

    def test_empty_series(self):
        equity = pd.Series([], dtype=float)
        # annual=0, max_dd=0 → inf
        assert calmar(equity) == float("inf")


# ---------------------------------------------------------------------------
# sharpe
# ---------------------------------------------------------------------------


class TestSharpe:
    def test_positive_on_monotonic_growth(self):
        """Monotonically increasing equity → all pct_change > 0 → sharpe > 0."""
        equity = _linear_equity(1000.0, 1100.0, 500)
        assert sharpe(equity) > 0.0

    def test_zero_on_flat_equity(self):
        """Flat equity → pct_change == 0 → std == 0 → sharpe == 0."""
        equity = _flat_equity(1000.0, 500)
        assert sharpe(equity) == pytest.approx(0.0, abs=1e-12)

    def test_empty_series_returns_zero(self):
        equity = pd.Series([], dtype=float)
        assert sharpe(equity) == 0.0

    def test_single_element_returns_zero(self):
        equity = pd.Series([1000.0])
        assert sharpe(equity) == 0.0

    def test_two_elements_no_crash(self):
        """Two-element series should not raise (pct_change gives 1 return)."""
        equity = pd.Series([1000.0, 1010.0])
        result = sharpe(equity)
        # std of a single observation is NaN/0 → returns 0.0
        assert result == 0.0

    def test_negative_sharpe_on_declining(self):
        """Monotonically declining equity → mean return < 0 → sharpe < 0."""
        equity = _linear_equity(1100.0, 1000.0, 500)
        assert sharpe(equity) < 0.0


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_returns_all_keys(self):
        equity = _linear_equity(1000.0, 1100.0, PERIODS_PER_YEAR)
        result = summarize(equity)
        assert set(result.keys()) == {"annual", "max_dd", "calmar", "sharpe"}

    def test_values_consistent_with_individual(self):
        """summarize must return the same values as individual functions."""
        n = PERIODS_PER_YEAR
        equity = _v_shape_equity(start=900.0, peak=1000.0, trough=850.0, n_hours=n)
        result = summarize(equity)
        assert result["annual"] == pytest.approx(annualized_return(equity), rel=1e-12)
        assert result["max_dd"] == pytest.approx(max_drawdown(equity), rel=1e-12)
        assert result["calmar"] == pytest.approx(calmar(equity), rel=1e-12)
        assert result["sharpe"] == pytest.approx(sharpe(equity), rel=1e-12)

    def test_empty_series_no_exception(self):
        equity = pd.Series([], dtype=float)
        result = summarize(equity)
        assert result["annual"] == 0.0
        assert result["max_dd"] == 0.0
        assert result["sharpe"] == 0.0
        # calmar: max_dd==0 → inf
        assert result["calmar"] == float("inf")

    def test_single_element_no_exception(self):
        equity = pd.Series([345.0])
        result = summarize(equity)
        assert result["annual"] == 0.0
        assert result["max_dd"] == 0.0
        assert result["sharpe"] == 0.0
        assert result["calmar"] == float("inf")
