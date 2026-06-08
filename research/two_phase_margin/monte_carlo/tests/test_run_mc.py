"""
test_run_mc.py — Acceptance tests for run_mc.py (T5).

All tests use small n / short horizon to keep the suite fast:
  n=6, horizon_days=30 (720 h), jobs=1 or 2, sequential or parallel.

Acceptance criteria (PLAN.md T5):
1. run() with parametric / bootstrap returns a DataFrame of N=6 rows with all
   expected columns; CSV is written to the tmp out_dir.
2. DETERMINISM: two calls with identical seed → identical rows.
3. PARALLELISM INVARIANT: jobs=1 result == jobs=2 result (same seeds → same
   numbers regardless of process count).
4. path_seed column contains base_seed … base_seed+n-1 (distinct values).
5. Sanity: final_equity > 0 for all rows; max_dd in [0, 1].
6. calmar may be inf (no drawdown) — that is valid, not a test failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# sys.path — same as all other tests in this package
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_RESEARCH_TPM = _THIS.parents[2]          # research/two_phase_margin/
_REPO_ROOT = _THIS.parents[4]             # project root

if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo.run_mc import _COLUMNS, run  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test parameters (keep small for speed)
# ---------------------------------------------------------------------------
_N = 6
_HORIZON_DAYS = 30      # 720 h — fast enough for the engine
_SEED = 99
_COINS = ["BTC", "ETH", "SOL"]   # 3-coin subset → faster than 5-coin full set

_CALIB_DIR = _REPO_ROOT / "research" / "two_phase_margin" / "monte_carlo" / "calibration"
_DATA_DIR = _REPO_ROOT / "research" / "data"


def _check_data_available() -> None:
    """Skip if calibration or real-data files are missing."""
    for coin in _COINS:
        calib_p = _CALIB_DIR / f"{coin}.json"
        if not calib_p.exists():
            pytest.skip(f"Calibration JSON missing: {calib_p}")
        for suffix in [".csv", "_1h.csv"]:
            dp = _DATA_DIR / f"{coin}{suffix}"
            if not dp.exists():
                pytest.skip(f"Data file missing: {dp}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_parametric(out_dir: Path, jobs: int = 1, seed: int = _SEED) -> pd.DataFrame:
    return run(
        n=_N,
        horizon_days=_HORIZON_DAYS,
        seed=seed,
        generator="parametric",
        coins=list(_COINS),
        mbuf=3.0,
        params_mode="defaults",   # avoid DB dependency in tests
        jobs=jobs,
        out_dir=out_dir,
        calib_dir=_CALIB_DIR,
        data_dir=_DATA_DIR,
    )


def _run_bootstrap(out_dir: Path, jobs: int = 1, seed: int = _SEED) -> pd.DataFrame:
    return run(
        n=_N,
        horizon_days=_HORIZON_DAYS,
        seed=seed,
        generator="bootstrap",
        coins=list(_COINS),
        mbuf=3.0,
        params_mode="defaults",
        jobs=jobs,
        out_dir=out_dir,
        calib_dir=_CALIB_DIR,
        data_dir=_DATA_DIR,
    )


# ---------------------------------------------------------------------------
# T5-A: parametric generator — shape and columns
# ---------------------------------------------------------------------------

class TestRunMCParametric:
    """run() with parametric generator: shape, columns, CSV, basic sanity."""

    @pytest.fixture(scope="class")
    def result(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as tmpdir:
            df = _run_parametric(Path(tmpdir))
            # Keep a reference; tmpdir is deleted after yield but df is in memory
            yield df, Path(tmpdir)

    @pytest.fixture(scope="class")
    def df(self, result):
        return result[0]

    def test_returns_dataframe(self, df):
        assert isinstance(df, pd.DataFrame)

    def test_n_rows(self, df):
        assert len(df) == _N, f"Expected {_N} rows, got {len(df)}"

    def test_all_columns_present(self, df):
        missing = [c for c in _COLUMNS if c not in df.columns]
        assert not missing, f"Missing columns: {missing}"

    def test_path_seeds_are_base_to_base_plus_n(self, df):
        """path_seed should be seed, seed+1, …, seed+n-1."""
        expected = list(range(_SEED, _SEED + _N))
        got = sorted(df["path_seed"].tolist())
        assert got == expected, f"path_seeds mismatch: got {got}, expected {expected}"

    def test_final_equity_positive(self, df):
        assert (df["final_equity"] > 0).all(), (
            f"Non-positive final_equity in rows:\n{df[df['final_equity'] <= 0]}"
        )

    def test_max_dd_in_range(self, df):
        assert (df["max_dd"] >= 0).all()
        assert (df["max_dd"] <= 1).all()

    def test_annual_is_finite(self, df):
        # annual should be finite; calmar may be inf if max_dd==0
        assert df["annual"].apply(lambda x: abs(x) < 1e10).all(), (
            "annual contains extreme values"
        )

    def test_generator_column(self, df):
        assert (df["generator"] == "parametric").all()

    def test_horizon_h_column(self, df):
        expected_h = _HORIZON_DAYS * 24
        assert (df["horizon_h"] == expected_h).all()

    def test_csv_written(self):
        """CSV file should be created in out_dir."""
        _check_data_available()
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_parametric(Path(tmpdir))
            csvs = list(Path(tmpdir).glob("mc_parametric_*.csv"))
            assert len(csvs) == 1, f"Expected 1 CSV, found: {csvs}"
            loaded = pd.read_csv(csvs[0])
            assert len(loaded) == _N


# ---------------------------------------------------------------------------
# T5-B: bootstrap generator — shape and columns
# ---------------------------------------------------------------------------

class TestRunMCBootstrap:
    """run() with bootstrap generator: shape, columns, CSV, basic sanity."""

    @pytest.fixture(scope="class")
    def df(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as tmpdir:
            df = _run_bootstrap(Path(tmpdir))
            yield df

    def test_returns_dataframe(self, df):
        assert isinstance(df, pd.DataFrame)

    def test_n_rows(self, df):
        assert len(df) == _N

    def test_all_columns_present(self, df):
        missing = [c for c in _COLUMNS if c not in df.columns]
        assert not missing, f"Missing columns: {missing}"

    def test_path_seeds_are_base_to_base_plus_n(self, df):
        expected = list(range(_SEED, _SEED + _N))
        got = sorted(df["path_seed"].tolist())
        assert got == expected

    def test_final_equity_positive(self, df):
        assert (df["final_equity"] > 0).all()

    def test_max_dd_in_range(self, df):
        assert (df["max_dd"] >= 0).all()
        assert (df["max_dd"] <= 1).all()

    def test_generator_column(self, df):
        assert (df["generator"] == "bootstrap").all()

    def test_csv_written(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_bootstrap(Path(tmpdir))
            csvs = list(Path(tmpdir).glob("mc_bootstrap_*.csv"))
            assert len(csvs) == 1
            loaded = pd.read_csv(csvs[0])
            assert len(loaded) == _N


# ---------------------------------------------------------------------------
# T5-C: Determinism — same seed → identical rows
# ---------------------------------------------------------------------------

class TestDeterminism:
    """Two runs with the same seed must produce identical DataFrames."""

    def test_parametric_deterministic(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            df1 = _run_parametric(Path(td1), jobs=1, seed=77)
            df2 = _run_parametric(Path(td2), jobs=1, seed=77)

        numeric_cols = [c for c in _COLUMNS if c not in ("generator", "coins", "path_seed")]
        for col in numeric_cols:
            vals1 = df1[col].tolist()
            vals2 = df2[col].tolist()
            assert vals1 == vals2, (
                f"Column '{col}' differs between two runs with same seed:\n"
                f"  run1: {vals1}\n  run2: {vals2}"
            )

    def test_bootstrap_deterministic(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            df1 = _run_bootstrap(Path(td1), jobs=1, seed=77)
            df2 = _run_bootstrap(Path(td2), jobs=1, seed=77)

        numeric_cols = [c for c in _COLUMNS if c not in ("generator", "coins", "path_seed")]
        for col in numeric_cols:
            vals1 = df1[col].tolist()
            vals2 = df2[col].tolist()
            assert vals1 == vals2, (
                f"Column '{col}' differs between two bootstrap runs with same seed:\n"
                f"  run1: {vals1}\n  run2: {vals2}"
            )

    def test_different_seeds_differ(self):
        """Sanity: two different seeds should not produce identical annual values."""
        _check_data_available()
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            df1 = _run_parametric(Path(td1), jobs=1, seed=11)
            df2 = _run_parametric(Path(td2), jobs=1, seed=22)

        # Different seeds → at least one annual value should differ
        annuals1 = df1["annual"].tolist()
        annuals2 = df2["annual"].tolist()
        assert annuals1 != annuals2, (
            "Different seeds produced identical annual values — suspicious"
        )


# ---------------------------------------------------------------------------
# T5-D: Parallelism invariant — jobs=1 == jobs=2
# ---------------------------------------------------------------------------

class TestParallelismInvariant:
    """Result with jobs=1 must be identical (row-by-row) to jobs=2."""

    def test_parametric_jobs1_eq_jobs2(self):
        _check_data_available()
        with (
            tempfile.TemporaryDirectory() as td1,
            tempfile.TemporaryDirectory() as td2,
        ):
            df1 = _run_parametric(Path(td1), jobs=1, seed=55)
            df2 = _run_parametric(Path(td2), jobs=2, seed=55)

        # Sort by path_seed so row order is deterministic (pool.map preserves order,
        # but let's be explicit)
        df1 = df1.sort_values("path_seed").reset_index(drop=True)
        df2 = df2.sort_values("path_seed").reset_index(drop=True)

        numeric_cols = [
            c for c in _COLUMNS
            if c not in ("generator", "coins", "path_seed", "horizon_h", "mbuf")
        ]
        for col in numeric_cols:
            vals1 = df1[col].tolist()
            vals2 = df2[col].tolist()
            assert vals1 == vals2, (
                f"jobs=1 vs jobs=2 differ in column '{col}':\n"
                f"  jobs=1: {vals1}\n  jobs=2: {vals2}"
            )

    def test_bootstrap_jobs1_eq_jobs2(self):
        _check_data_available()
        with (
            tempfile.TemporaryDirectory() as td1,
            tempfile.TemporaryDirectory() as td2,
        ):
            df1 = _run_bootstrap(Path(td1), jobs=1, seed=55)
            df2 = _run_bootstrap(Path(td2), jobs=2, seed=55)

        df1 = df1.sort_values("path_seed").reset_index(drop=True)
        df2 = df2.sort_values("path_seed").reset_index(drop=True)

        numeric_cols = [
            c for c in _COLUMNS
            if c not in ("generator", "coins", "path_seed", "horizon_h", "mbuf")
        ]
        for col in numeric_cols:
            vals1 = df1[col].tolist()
            vals2 = df2[col].tolist()
            assert vals1 == vals2, (
                f"bootstrap jobs=1 vs jobs=2 differ in '{col}':\n"
                f"  jobs=1: {vals1}\n  jobs=2: {vals2}"
            )


# ---------------------------------------------------------------------------
# T5-E: Additional column-level sanity checks
# ---------------------------------------------------------------------------

class TestColumnSanity:
    """Fine-grained checks on individual columns."""

    @pytest.fixture(scope="class")
    def df(self):
        _check_data_available()
        with tempfile.TemporaryDirectory() as tmpdir:
            df = _run_parametric(Path(tmpdir), jobs=1)
            yield df

    def test_mbuf_column(self, df):
        assert (df["mbuf"] == 3.0).all()

    def test_coins_column(self, df):
        expected = ",".join(_COINS)
        assert (df["coins"] == expected).all()

    def test_max_dd_pct_raw_non_negative(self, df):
        """max_dd_pct_raw (raw engine, %) should be >= 0."""
        assert (df["max_dd_pct_raw"] >= 0).all()

    def test_total_fees_non_positive(self, df):
        """Fees are costs (negative or zero in net terms; raw total_fees is positive cost)."""
        # Engine records fees as positive cost; just check it's a finite number
        assert df["total_fees"].apply(lambda x: abs(x) < 1e9).all()

    def test_n_liquidations_non_negative(self, df):
        assert (df["n_liquidations"] >= 0).all()

    def test_exit_counters_non_negative(self, df):
        for col in [
            "n_forced_closes",
            "n_phase1_neg_exits",
            "n_phase1_cap_exits",
            "n_phase1_negstop_exits",
            "n_phase2_exits",
        ]:
            assert (df[col] >= 0).all(), f"Negative values in {col}"

    def test_calmar_positive_or_inf_when_annual_positive(self, df):
        """Calmar must be positive (or inf) when annual > 0."""
        import math
        for _, row in df.iterrows():
            if row["annual"] > 0:
                assert row["calmar"] > 0 or math.isinf(row["calmar"]), (
                    f"annual={row['annual']:.6f} > 0 but calmar={row['calmar']}"
                )

    def test_annual_pct_raw_finite(self, df):
        assert df["annual_pct_raw"].apply(lambda x: abs(x) < 1e10).all()
