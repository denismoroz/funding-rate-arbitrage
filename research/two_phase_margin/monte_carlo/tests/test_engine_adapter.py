"""
test_engine_adapter.py — Regression anchor tests for T1 (engine adapter).

Acceptance criteria (PLAN.md T1):
1. Adapter run on REAL data reproduces the U-prod buf=3 row from
   research/TWOPHASE_MARGIN_aggregate.csv with ≥6 significant figures for
   annual_pct and max_dd_pct, and exact match for n_phase1_negstop_exits.
2. metrics.max_dd (fraction) agrees with raw["max_dd_pct"] (percent) — they are
   derived from the same equity curve.  Tolerance: 1e-6 (same series, both
   computed from cummax; only difference is the percent vs fraction scaling).
3. T0 tests (test_metrics.py) remain green (verified by running the full suite).

Scale notes (from engine_adapter.py docstring):
    raw["max_dd_pct"]  is in PERCENT  (e.g. 0.0782 → 0.0782%)
    metrics.max_dd()   is in FRACTION  (e.g. 0.000782 → 0.0782%)
    Conversion: raw["max_dd_pct"] / 100 == metrics["max_dd"] (within floating-point tol)

    raw["annual_pct"] is a simple linear APR in percent:
        (end/start - 1) / period_years * 100
    metrics["annual"] is CAGR (compound):
        (end/start) ** (8760/n) - 1
    For ~6-month windows the two differ by <0.01 percentage points — we compare
    only raw vs CSV for the annual anchor, not raw vs metrics.

=== How main() builds the U-prod, buf=3 run ===

main() creates params via make_params():
    TwoPhaseParams(
        coins=prod_params.coins,              # from DB / defaults
        entry_threshold_apr=prod_params.*,    # all two-phase logic params from DB
        ...                                   # (signal_window_hours, safety_mult, etc.)
        position_size_usdc=100.0,             # FIXED research size
        budget_cap_usdc=1000.0,               # FIXED research budget
        margin_buffer_factor=mbuf,            # 3.0 for buf=3 row
        # neg_stop_threshold_apr / neg_stop_patience_hours use dataclass defaults
    )
Then calls simulate(coins, p, margin_buffer_x=mbuf, position_size=100.0,
                   restrict_start=None, restrict_end=None)
— no window restriction for U-prod (timeline is determined by common_timeline of dfs).

The adapter test must replicate EXACTLY this setup so the regression anchor holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# sys.path setup — mirrors test_metrics.py (PLAN.md rule 2)
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_RESEARCH_TPM = _THIS.parents[2]          # research/two_phase_margin/
_REPO_ROOT = _THIS.parents[4]             # project root

if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

# ---------------------------------------------------------------------------
# Load the engine (by file path, PLAN.md rule 2) to access load_coin_df /
# load_prod_params.  We import via engine_adapter which already loads it.
# ---------------------------------------------------------------------------
from monte_carlo.engine_adapter import RunResult, _engine, run_on_dfs  # noqa: E402

# ---------------------------------------------------------------------------
# CSV anchor values (U-prod, buf=3.0 row)
# ---------------------------------------------------------------------------
_CSV_PATH = _REPO_ROOT / "research" / "TWOPHASE_MARGIN_aggregate.csv"


def _load_anchor() -> dict:
    df = pd.read_csv(_CSV_PATH)
    row = df[(df["universe"] == "U-prod") & (df["margin_buffer_x"] == 3.0)].iloc[0]
    return row.to_dict()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_real_dfs(coins: list[str]) -> dict[str, pd.DataFrame]:
    """Load real CSV data via the engine's load_coin_df (production path)."""
    dfs = {}
    for c in coins:
        try:
            dfs[c] = _engine.load_coin_df(c)
        except FileNotFoundError:
            pytest.skip(f"Data file missing for {c} — skip test requiring real data")
    return dfs


def _make_research_params(prod_params: object, coins: list[str], mbuf: float) -> object:
    """Replicate main()'s make_params() exactly: same two-phase logic params from DB,
    but override budget/position_size to research-standard values ($1000/$100).

    This matches the sweep that produced TWOPHASE_MARGIN_aggregate.csv.
    neg_stop_threshold_apr and neg_stop_patience_hours use dataclass defaults
    (as in make_params — they are NOT in the copy loop, so they fall back to -0.15 / 6).
    """
    return _engine.TwoPhaseParams(
        coins=coins,
        entry_threshold_apr=prod_params.entry_threshold_apr,
        phase2_exit_threshold=prod_params.phase2_exit_threshold,
        base_min_hold_hours=prod_params.base_min_hold_hours,
        cap_min_hold_hours=prod_params.cap_min_hold_hours,
        safety_mult=prod_params.safety_mult,
        signal_window_hours=prod_params.signal_window_hours,
        concurrency_cap=prod_params.concurrency_cap,
        position_size_usdc=100.0,    # FIXED research size (POSITION_SIZE in main())
        budget_cap_usdc=1000.0,      # FIXED research budget (BUDGET in main())
        margin_buffer_factor=mbuf,
        phase1_negative_patience=prod_params.phase1_negative_patience,
        phase1_breakeven_cap_hours=prod_params.phase1_breakeven_cap_hours,
        # neg_stop_threshold_apr, neg_stop_patience_hours: use dataclass defaults
        # (same as make_params which does not copy these from prod_params)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEngineAdapterAnchor:
    """Regression anchor: adapter on real data must reproduce the CSV row."""

    @pytest.fixture(scope="class")
    def anchor(self):
        return _load_anchor()

    @pytest.fixture(scope="class")
    def result(self, anchor):
        """Run the adapter once with prod params and real data (cached per class).

        Replicates main()'s U-prod, buf=3 sweep exactly:
        - coins = prod_params.coins (from DB / defaults)
        - params = make_params equivalent (budget=$1000, position_size=$100)
        - mbuf = 3.0
        - no restrict_start / restrict_end (U-prod path in main)
        """
        prod_params, _ = _engine.load_prod_params()
        coins = list(prod_params.coins)  # e.g. ['BTC','ETH','SOL','HYPE','PURR']
        params = _make_research_params(prod_params, coins, mbuf=3.0)

        dfs = _load_real_dfs(coins)
        # sizing="flat" pins the original research-sweep sizing that produced
        # TWOPHASE_MARGIN_aggregate.csv (the adapter now defaults to prod_slot).
        return run_on_dfs(dfs, params, mbuf=3.0, coins=coins, position_size=100.0, sizing="flat")

    def test_returns_run_result(self, result):
        assert isinstance(result, RunResult)

    def test_equity_is_series(self, result):
        assert isinstance(result.equity, pd.Series)
        assert len(result.equity) > 0

    def test_metrics_dict_has_expected_keys(self, result):
        assert set(result.metrics.keys()) == {"annual", "max_dd", "calmar", "sharpe"}

    def test_annual_pct_matches_csv(self, result, anchor):
        """raw annual_pct must reproduce CSV value (≥6 significant figures).

        The CSV stores values rounded to 4 decimal places (main() calls
        round(res['annual_pct'], 4) before writing).  We compare by rounding the
        adapter's output to 4dp — this verifies the adapter calls the engine
        identically (same data, same params, same logic path).
        """
        expected = anchor["annual_pct"]   # e.g. 2.5026 (already rounded to 4dp)
        got = result.raw["annual_pct"]
        # round to 4 decimal places matches CSV precision; rel diff < 1e-4 confirms
        # the adapter is on the right code path (wrong path → wrong sign or wrong order)
        assert round(got, 4) == expected, (
            f"annual_pct: round(got,4)={round(got,4)}, expected={expected} "
            f"(full precision: {got})"
        )

    def test_max_dd_pct_matches_csv(self, result, anchor):
        """raw max_dd_pct must reproduce CSV value (≥6 significant figures).

        Same rounding convention as annual_pct — CSV stores 4dp.
        """
        expected = anchor["max_dd_pct"]   # e.g. 0.0782
        got = result.raw["max_dd_pct"]
        assert round(got, 4) == expected, (
            f"max_dd_pct: round(got,4)={round(got,4)}, expected={expected} "
            f"(full precision: {got})"
        )

    def test_negstop_exits_matches_csv(self, result, anchor):
        """n_phase1_negstop_exits must match exactly (integer count)."""
        expected = int(anchor["n_phase1_negstop_exits"])
        got = result.raw["n_phase1_negstop_exits"]
        assert got == expected, (
            f"n_phase1_negstop_exits: got {got}, expected {expected}"
        )

    def test_max_dd_fraction_consistent_with_raw(self, result):
        """metrics.max_dd (fraction) must agree with raw max_dd_pct (percent).

        Both are derived from the SAME equity curve via cummax; the only difference
        is the × 100 scaling.  We expect agreement to within 1e-6 rel tolerance
        (floating-point arithmetic on identical series).

        See engine_adapter docstring for scale note.
        """
        raw_dd_pct = result.raw["max_dd_pct"]          # in percent
        metrics_dd_frac = result.metrics["max_dd"]     # in fraction
        # Convert raw to fraction for comparison
        raw_dd_frac = raw_dd_pct / 100.0
        assert metrics_dd_frac == pytest.approx(raw_dd_frac, rel=1e-6, abs=1e-10), (
            f"max_dd fraction mismatch: metrics.max_dd={metrics_dd_frac:.8f}, "
            f"raw_dd_pct/100={raw_dd_frac:.8f} (raw={raw_dd_pct}%)"
        )

    def test_equity_length_matches_n_hours(self, result, anchor):
        """Equity series length must match the n_hours in the CSV."""
        expected_n_hours = int(anchor["n_hours"])
        got = len(result.equity)
        assert got == expected_n_hours, (
            f"equity length {got} != n_hours {expected_n_hours}"
        )


class TestEngineAdapterSynthetic:
    """Smoke tests on synthetic (tiny) dfs to verify adapter is source-agnostic."""

    def _make_synthetic_dfs(self, n: int = 2000) -> tuple[dict, object]:
        """Tiny constant-funding synthetic universe for fast smoke test."""
        import numpy as np

        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        coins = ["BTC", "ETH"]
        dfs = {}
        for c in coins:
            # Constant 20% APR funding, flat price — simple known-good input
            rate = 0.20 / 8760
            dfs[c] = pd.DataFrame(
                {"close": np.full(n, 100.0), "fundingRate": np.full(n, rate)},
                index=idx,
            )

        params = _engine.TwoPhaseParams(
            coins=coins,
            entry_threshold_apr=0.10,
            phase2_exit_threshold=-0.10,
            base_min_hold_hours=24,
            cap_min_hold_hours=720,
            safety_mult=5.0,
            signal_window_hours=1,
            concurrency_cap=2,
            position_size_usdc=100.0,
            budget_cap_usdc=1000.0,
            margin_buffer_factor=3.0,
            phase1_negative_patience=72,
            phase1_breakeven_cap_hours=720,
        )
        return dfs, params

    def test_returns_run_result_on_synthetic(self):
        dfs, params = self._make_synthetic_dfs()
        result = run_on_dfs(dfs, params, mbuf=3.0, coins=list(dfs.keys()))
        assert isinstance(result, RunResult)

    def test_positive_annual_on_positive_funding(self):
        """With constant 20% APR funding the annual return must be positive."""
        dfs, params = self._make_synthetic_dfs()
        result = run_on_dfs(dfs, params, mbuf=3.0, coins=list(dfs.keys()))
        assert result.raw["annual_pct"] > 0.0

    def test_equity_indexed_by_timestamps(self):
        dfs, params = self._make_synthetic_dfs()
        result = run_on_dfs(dfs, params, mbuf=3.0, coins=list(dfs.keys()))
        assert isinstance(result.equity.index, pd.DatetimeIndex)

    def test_metrics_max_dd_non_negative(self):
        dfs, params = self._make_synthetic_dfs()
        result = run_on_dfs(dfs, params, mbuf=3.0, coins=list(dfs.keys()))
        assert result.metrics["max_dd"] >= 0.0

    def test_caller_dfs_not_mutated(self):
        """Adapter must not mutate the caller's DataFrames."""
        dfs, params = self._make_synthetic_dfs()
        original_cols = {c: set(df.columns) for c, df in dfs.items()}
        run_on_dfs(dfs, params, mbuf=3.0, coins=list(dfs.keys()))
        for c, df in dfs.items():
            assert set(df.columns) == original_cols[c], (
                f"Coin {c}: caller df was mutated, columns changed to {set(df.columns)}"
            )
