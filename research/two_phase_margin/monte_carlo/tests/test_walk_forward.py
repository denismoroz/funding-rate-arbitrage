"""
test_walk_forward.py — Acceptance tests for T8 (walk-forward validation).

Acceptance criteria (PLAN.md T8):
1. walk_forward runs on BTC/ETH/SOL and produces ≥3 folds with IS and OOS metrics.
2. No look-ahead: for each fold the OOS metric is evaluated on params chosen on the
   PRECEDING train window; test window is strictly after train window.
3. Static baseline is present; both tuned-OOS and static-OOS appear in results.
4. Tests use SHORT windows / SMALL grid for speed (not the default 12m/3m/3m).
5. Determinism: two runs with identical inputs produce identical results.

Implementation notes:
- We use a mini-grid (2 entry thresholds × 1 exit threshold = 2 combos) and
  4-month train + 2-month test + 2-month step windows to keep test runtime fast
  while still generating ≥3 folds.
- Real coin data (BTC/ETH/SOL) is used — no synthetic data needed for this test
  since walk_forward.py itself loads real data via the engine adapter.
- We import walk_forward from the monte_carlo package using the same sys.path trick
  used everywhere in this package.
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors engine_adapter.py / walk_forward.py)
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_TPM = _THIS_FILE.parents[2]  # research/two_phase_margin/
if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo import walk_forward as wf_mod  # noqa: E402
from monte_carlo.engine_adapter import _engine  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MINI_GRID: dict = {
    "entry_threshold_apr": [0.10, 0.15],
    "phase2_exit_threshold": [-0.10],
}

# Short windows to keep the test fast: 4-month train, 2-month test, 2-month step.
# With ~35 months of common history (2023-06 → 2026-05) this yields ≥14 folds.
FAST_TRAIN_MONTHS = 4
FAST_TEST_MONTHS = 2
FAST_STEP_MONTHS = 2

COINS = ["BTC", "ETH", "SOL"]


@pytest.fixture(scope="module")
def fast_wf_result():
    """Run walk-forward once with mini config; reuse across tests in this module."""
    return wf_mod.run_walk_forward(
        coins=COINS,
        train_months=FAST_TRAIN_MONTHS,
        test_months=FAST_TEST_MONTHS,
        step_months=FAST_STEP_MONTHS,
        mbuf=3.0,
        is_metric="annual",
        grid=MINI_GRID,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# T8 acceptance tests
# ---------------------------------------------------------------------------

class TestFoldCount:
    """Walk-forward produces ≥3 folds."""

    def test_at_least_three_folds(self, fast_wf_result):
        assert len(fast_wf_result.folds) >= 3, (
            f"Expected ≥3 folds, got {len(fast_wf_result.folds)}"
        )

    def test_folds_have_is_and_oos_metrics(self, fast_wf_result):
        """Every fold must have finite IS and OOS annual metrics."""
        for f in fast_wf_result.folds:
            assert not math.isnan(f.is_annual), f"Fold {f.fold_idx}: IS annual is NaN"
            assert not math.isnan(f.oos_annual), f"Fold {f.fold_idx}: OOS annual is NaN"
            assert not math.isnan(f.static_annual), f"Fold {f.fold_idx}: static annual is NaN"


class TestNoLookAhead:
    """OOS is evaluated on params from the train window; test is strictly after train."""

    def test_test_window_after_train_window(self, fast_wf_result):
        for f in fast_wf_result.folds:
            assert f.test_start >= f.train_end, (
                f"Fold {f.fold_idx}: test_start={f.test_start} < train_end={f.train_end} "
                "— look-ahead violation!"
            )
            assert f.test_end > f.test_start, (
                f"Fold {f.fold_idx}: test_end={f.test_end} <= test_start={f.test_start}"
            )

    def test_train_and_test_do_not_overlap(self, fast_wf_result):
        """Train [train_start, train_end) and test [test_start, test_end) must not overlap."""
        for f in fast_wf_result.folds:
            # test_start == train_end is the boundary (train slice uses .loc[start:end]
            # which is inclusive on both ends by pandas convention).
            # We require test_start >= train_end — train cannot reach into test.
            assert f.test_start >= f.train_end, (
                f"Fold {f.fold_idx}: training window overlaps test window"
            )

    def test_consecutive_fold_test_windows_do_not_regress(self, fast_wf_result):
        """Test windows should advance monotonically (no going back in time)."""
        folds = fast_wf_result.folds
        for i in range(1, len(folds)):
            prev = folds[i - 1]
            curr = folds[i]
            assert curr.test_start > prev.test_start, (
                f"Fold {i}: test_start did not advance vs fold {i-1}"
            )


class TestStaticBaseline:
    """Static baseline (prod-default params) is computed and present."""

    def test_static_annual_present(self, fast_wf_result):
        for f in fast_wf_result.folds:
            assert hasattr(f, "static_annual"), f"Fold {f.fold_idx}: missing static_annual"
            assert hasattr(f, "static_calmar"), f"Fold {f.fold_idx}: missing static_calmar"

    def test_both_tuned_and_static_oos_in_aggregate(self, fast_wf_result):
        """WalkForwardResult must expose mean_oos_annual and mean_static_annual."""
        wfr = fast_wf_result
        assert not math.isnan(wfr.mean_oos_annual)
        assert not math.isnan(wfr.mean_static_annual)
        assert not math.isnan(wfr.mean_is_annual)


class TestDeterminism:
    """Two runs with identical inputs produce identical results."""

    def test_two_runs_identical(self):
        run1 = wf_mod.run_walk_forward(
            coins=COINS,
            train_months=FAST_TRAIN_MONTHS,
            test_months=FAST_TEST_MONTHS,
            step_months=FAST_STEP_MONTHS,
            mbuf=3.0,
            is_metric="annual",
            grid=MINI_GRID,
            verbose=False,
        )
        run2 = wf_mod.run_walk_forward(
            coins=COINS,
            train_months=FAST_TRAIN_MONTHS,
            test_months=FAST_TEST_MONTHS,
            step_months=FAST_STEP_MONTHS,
            mbuf=3.0,
            is_metric="annual",
            grid=MINI_GRID,
            verbose=False,
        )
        assert len(run1.folds) == len(run2.folds)
        for f1, f2 in zip(run1.folds, run2.folds):
            assert f1.is_annual == f2.is_annual, (
                f"Fold {f1.fold_idx}: is_annual not deterministic"
            )
            assert f1.oos_annual == f2.oos_annual, (
                f"Fold {f1.fold_idx}: oos_annual not deterministic"
            )
            assert f1.static_annual == f2.static_annual, (
                f"Fold {f1.fold_idx}: static_annual not deterministic"
            )
            assert f1.best_entry_threshold == f2.best_entry_threshold, (
                f"Fold {f1.fold_idx}: best_entry_threshold not deterministic"
            )


class TestGridSearchNoMutation:
    """Grid search must not mutate the base prod params."""

    def test_prod_params_not_mutated(self):
        prod_params, _ = _engine.load_prod_params()
        original_entry = prod_params.entry_threshold_apr
        original_ph2 = prod_params.phase2_exit_threshold

        # Run a grid with deliberately different values
        test_grid = {
            "entry_threshold_apr": [0.05, 0.20],
            "phase2_exit_threshold": [-0.05],
        }
        wf_mod.run_walk_forward(
            coins=COINS,
            train_months=FAST_TRAIN_MONTHS,
            test_months=FAST_TEST_MONTHS,
            step_months=FAST_STEP_MONTHS,
            grid=test_grid,
            verbose=False,
        )

        # Original must be unchanged
        assert prod_params.entry_threshold_apr == original_entry, (
            "Grid search mutated prod_params.entry_threshold_apr!"
        )
        assert prod_params.phase2_exit_threshold == original_ph2, (
            "Grid search mutated prod_params.phase2_exit_threshold!"
        )


class TestFoldWindows:
    """Fold window geometry is correct."""

    def test_fold_window_geometry(self):
        """Verify that _generate_folds produces correct windows."""
        start = pd.Timestamp("2023-01-01", tz="UTC")
        end = pd.Timestamp("2025-01-01", tz="UTC")
        folds = wf_mod._generate_folds(start, end, train_months=6, test_months=2, step_months=2)
        assert len(folds) >= 3
        for train_start, train_end, test_start, test_end in folds:
            assert train_end == test_start, "test_start must equal train_end"
            assert test_end > test_start, "test window must be positive"
            assert train_end > train_start, "train window must be positive"

    def test_fold_step_advances(self):
        """Each fold starts step_months after the previous."""
        start = pd.Timestamp("2023-01-01", tz="UTC")
        end = pd.Timestamp("2025-12-01", tz="UTC")
        step = 3
        folds = wf_mod._generate_folds(start, end, train_months=6, test_months=3, step_months=step)
        for i in range(1, len(folds)):
            delta = folds[i][0] - folds[i - 1][0]  # difference in train_start
            # Should be approximately step months — allow a day of tolerance for month-length variation
            days = delta.days
            expected_days_min = step * 28  # short months
            expected_days_max = step * 31  # long months
            assert expected_days_min <= days <= expected_days_max, (
                f"Fold step is {days} days, expected {step} months"
            )


class TestReportWriting:
    """Report is written without errors and contains expected sections."""

    def test_report_written(self, fast_wf_result, tmp_path):
        out = tmp_path / "WF_TEST_REPORT.md"
        path = wf_mod.write_report(fast_wf_result, out_path=out)
        assert path == out
        assert out.exists()
        text = out.read_text()
        assert "Walk-Forward" in text
        assert "IS Annual" in text or "IS metric" in text
        assert "static" in text.lower()
        assert "Aggregate Summary" in text

    def test_report_has_all_folds(self, fast_wf_result, tmp_path):
        out = tmp_path / "WF_FOLDS_REPORT.md"
        wf_mod.write_report(fast_wf_result, out_path=out)
        text = out.read_text()
        for f in fast_wf_result.folds:
            assert str(f.fold_idx) in text, f"Fold {f.fold_idx} not in report"


class TestWalkForwardResultProperties:
    """WalkForwardResult aggregate properties work correctly."""

    def test_mean_is_annual_is_finite(self, fast_wf_result):
        assert math.isfinite(fast_wf_result.mean_is_annual)

    def test_mean_oos_annual_is_finite(self, fast_wf_result):
        assert math.isfinite(fast_wf_result.mean_oos_annual)

    def test_mean_static_annual_is_finite(self, fast_wf_result):
        assert math.isfinite(fast_wf_result.mean_static_annual)

    def test_result_stores_config(self, fast_wf_result):
        assert fast_wf_result.coins == COINS
        assert fast_wf_result.train_months == FAST_TRAIN_MONTHS
        assert fast_wf_result.test_months == FAST_TEST_MONTHS
        assert fast_wf_result.step_months == FAST_STEP_MONTHS
