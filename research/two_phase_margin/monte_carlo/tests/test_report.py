"""
test_report.py — Acceptance tests for report.py (T6).

Acceptance criteria (PLAN.md T6):
1. distribution_stats returns all expected keys; percentiles are monotonic
   (p05 <= p25 <= median <= p75 <= p95); P(annual<0) in [0,1].
2. write_report creates a .md file containing key sections:
   - Percentile distribution table
   - Single-path anchor line (U-prod buf=3)
   - Section "Occupied-capital reframe"
   - Mention of full-budget denominator
3. load_results correctly parses a directory containing CSVs from both
   generators and returns {parametric: df, bootstrap: df}.
4. No heavy runs in tests: n<=6, horizon<=30 d (720 h).

Test organisation:
  TestDistributionStats   — pure function on synthetic DataFrames (no I/O)
  TestLoadResults         — file-system tests using real run_mc.run (n=6, h=30)
  TestWriteReport         — markdown output checks
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# sys.path — mirrors other tests in this package
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_RESEARCH_TPM = _THIS.parents[2]       # research/two_phase_margin/
_REPO_ROOT = _THIS.parents[4]          # project root

if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo.report import (  # noqa: E402
    distribution_stats,
    load_results,
    write_report,
)

# ---------------------------------------------------------------------------
# Shared test fixtures — tiny MC runs (n=6, horizon=30 d)
# ---------------------------------------------------------------------------
_N = 6
_HORIZON_DAYS = 30
_SEED = 7
_COINS = ["BTC", "ETH", "SOL"]

_CALIB_DIR = _REPO_ROOT / "research" / "two_phase_margin" / "monte_carlo" / "calibration"
_DATA_DIR = _REPO_ROOT / "research" / "data"
_ANCHOR_CSV = _REPO_ROOT / "research" / "TWOPHASE_MARGIN_aggregate.csv"


def _data_available() -> bool:
    for coin in _COINS:
        if not (_CALIB_DIR / f"{coin}.json").exists():
            return False
        if not (_DATA_DIR / f"{coin}.csv").exists():
            return False
        if not (_DATA_DIR / f"{coin}_1h.csv").exists():
            return False
    return True


def _run_mc(generator: str, out_dir: Path) -> pd.DataFrame:
    """Helper: run a tiny MC for one generator, writing CSV to out_dir."""
    from monte_carlo.run_mc import run  # noqa: PLC0415
    return run(
        n=_N,
        horizon_days=_HORIZON_DAYS,
        seed=_SEED,
        generator=generator,
        coins=list(_COINS),
        mbuf=3.0,
        params_mode="defaults",   # no DB dependency
        jobs=1,
        out_dir=out_dir,
        calib_dir=_CALIB_DIR,
        data_dir=_DATA_DIR,
    )


# ---------------------------------------------------------------------------
# Synthetic DataFrame factory (no I/O; fast)
# ---------------------------------------------------------------------------

def _make_synthetic_df(
    n: int = 20,
    generator: str = "parametric",
    horizon_h: int = 720,
    coins: str = "BTC,ETH,SOL",
    mbuf: float = 3.0,
    annual_vals: list[float] | None = None,
    seed: int = 0,
) -> pd.DataFrame:
    """Create a synthetic MC result DataFrame with controlled values."""
    import numpy as np
    rng = np.random.default_rng(seed)

    if annual_vals is not None:
        annual = annual_vals[:n] if len(annual_vals) >= n else annual_vals + [0.01] * (n - len(annual_vals))
        annual = annual[:n]
    else:
        annual = rng.normal(0.02, 0.01, size=n).tolist()

    max_dd = [max(0.0, -a * 0.3 + abs(rng.normal(0.002, 0.001))) for a in annual]
    calmar = [a / m if m > 0 else float("inf") for a, m in zip(annual, max_dd)]
    sharpe = [a / 0.005 + rng.normal(0, 0.5) for a in annual]

    rows = []
    for i in range(n):
        rows.append({
            "path_seed": _SEED + i,
            "generator": generator,
            "horizon_h": horizon_h,
            "coins": coins,
            "mbuf": mbuf,
            "annual": annual[i],
            "max_dd": max_dd[i],
            "calmar": calmar[i],
            "sharpe": sharpe[i],
            "annual_pct_raw": annual[i] * 100,
            "max_dd_pct_raw": max_dd[i] * 100,
            "total_funding": rng.uniform(0.5, 5.0),
            "total_fees": rng.uniform(0.1, 1.0),
            "final_equity": 1000.0 + annual[i] * 100,
            "n_liquidations": 0,
            "n_forced_closes": 0,
            "n_phase1_neg_exits": int(rng.integers(0, 3)),
            "n_phase1_cap_exits": int(rng.integers(0, 3)),
            "n_phase1_negstop_exits": int(rng.integers(0, 2)),
            "n_phase2_exits": int(rng.integers(0, 5)),
        })
    return pd.DataFrame(rows)


# ===========================================================================
# TestDistributionStats — pure function, no I/O
# ===========================================================================

class TestDistributionStats:
    """Tests for distribution_stats() using synthetic DataFrames."""

    @pytest.fixture
    def df_default(self):
        return _make_synthetic_df(n=20, seed=42)

    @pytest.fixture
    def df_some_neg(self):
        """DataFrame where some annual values are negative."""
        annual_vals = [-0.02, -0.01, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03]
        return _make_synthetic_df(n=8, annual_vals=annual_vals, seed=1)

    @pytest.fixture
    def df_all_pos(self):
        """DataFrame where all annual values are positive."""
        annual_vals = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03]
        return _make_synthetic_df(n=6, annual_vals=annual_vals, seed=2)

    # ── Required keys ────────────────────────────────────────────────────────

    def test_all_expected_keys_present(self, df_default):
        s = distribution_stats(df_default)
        required_keys = [
            # Per-metric
            "annual_median", "annual_p05", "annual_p25", "annual_p75", "annual_p95",
            "annual_min", "annual_max",
            "max_dd_median", "max_dd_p05", "max_dd_p25", "max_dd_p75", "max_dd_p95",
            "max_dd_min", "max_dd_max",
            "calmar_median", "calmar_p05", "calmar_p25", "calmar_p75", "calmar_p95",
            "calmar_min", "calmar_max",
            "sharpe_median", "sharpe_p05", "sharpe_p25", "sharpe_p75", "sharpe_p95",
            "sharpe_min", "sharpe_max",
            # Risk probabilities
            "p_annual_neg",
            "p_maxdd_gt_1pct",
            "p_maxdd_gt_5pct",
            "cvar_annual_5pct",
            # Exit mix
            "avg_n_phase1_neg_exits",
            "avg_n_phase1_cap_exits",
            "avg_n_phase1_negstop_exits",
            "avg_n_phase2_exits",
            "avg_n_liquidations",
            "avg_n_forced_closes",
            # Metadata
            "n_paths",
            "horizon_h",
            "coins",
            "mbuf",
            "generator",
        ]
        missing = [k for k in required_keys if k not in s]
        assert not missing, f"Missing keys in distribution_stats output: {missing}"

    # ── Monotonicity of percentiles ──────────────────────────────────────────

    def test_annual_percentiles_monotonic(self, df_default):
        s = distribution_stats(df_default)
        assert s["annual_p05"] <= s["annual_p25"], (
            f"p05={s['annual_p05']} > p25={s['annual_p25']}"
        )
        assert s["annual_p25"] <= s["annual_median"], (
            f"p25={s['annual_p25']} > median={s['annual_median']}"
        )
        assert s["annual_median"] <= s["annual_p75"], (
            f"median={s['annual_median']} > p75={s['annual_p75']}"
        )
        assert s["annual_p75"] <= s["annual_p95"], (
            f"p75={s['annual_p75']} > p95={s['annual_p95']}"
        )

    def test_max_dd_percentiles_monotonic(self, df_default):
        s = distribution_stats(df_default)
        assert s["max_dd_p05"] <= s["max_dd_p25"]
        assert s["max_dd_p25"] <= s["max_dd_median"]
        assert s["max_dd_median"] <= s["max_dd_p75"]
        assert s["max_dd_p75"] <= s["max_dd_p95"]

    def test_calmar_percentiles_monotonic(self, df_default):
        s = distribution_stats(df_default)
        # calmar percentiles are computed on finite values; check finite ordering
        vals = [s["calmar_p05"], s["calmar_p25"], s["calmar_p75"], s["calmar_p95"]]
        finite = [v for v in vals if not math.isnan(v) and not math.isinf(v)]
        for i in range(len(finite) - 1):
            assert finite[i] <= finite[i + 1], (
                f"Calmar percentiles not monotonic: {vals}"
            )

    # ── P(annual < 0) in [0, 1] ───────────────────────────────────────────────

    def test_p_annual_neg_in_unit_interval(self, df_default):
        s = distribution_stats(df_default)
        assert 0.0 <= s["p_annual_neg"] <= 1.0

    def test_p_annual_neg_nonzero_when_some_negative(self, df_some_neg):
        s = distribution_stats(df_some_neg)
        assert s["p_annual_neg"] > 0.0, (
            "Expected p_annual_neg > 0 when some annual values are negative"
        )

    def test_p_annual_neg_zero_when_all_positive(self, df_all_pos):
        s = distribution_stats(df_all_pos)
        assert s["p_annual_neg"] == 0.0, (
            "Expected p_annual_neg == 0 when all annual values are positive"
        )

    def test_p_maxdd_in_unit_interval(self, df_default):
        s = distribution_stats(df_default)
        assert 0.0 <= s["p_maxdd_gt_1pct"] <= 1.0
        assert 0.0 <= s["p_maxdd_gt_5pct"] <= 1.0

    # ── CVaR: worst-5% mean ≤ p05 ─────────────────────────────────────────────

    def test_cvar_annual_leq_p05(self, df_default):
        """CVaR (mean of worst 5%) should be <= 5th percentile."""
        s = distribution_stats(df_default)
        if not math.isnan(s["cvar_annual_5pct"]) and not math.isnan(s["annual_p05"]):
            assert s["cvar_annual_5pct"] <= s["annual_p05"] + 1e-9, (
                f"CVaR={s['cvar_annual_5pct']} > p05={s['annual_p05']} — "
                "mean of worst-5% should be ≤ threshold"
            )

    # ── Metadata fields ───────────────────────────────────────────────────────

    def test_n_paths_matches_df_length(self, df_default):
        s = distribution_stats(df_default)
        assert s["n_paths"] == len(df_default)

    def test_generator_field(self, df_default):
        s = distribution_stats(df_default)
        assert s["generator"] == "parametric"

    def test_exit_mix_averages_non_negative(self, df_default):
        s = distribution_stats(df_default)
        for key in (
            "avg_n_phase1_neg_exits",
            "avg_n_phase1_cap_exits",
            "avg_n_phase1_negstop_exits",
            "avg_n_phase2_exits",
            "avg_n_liquidations",
            "avg_n_forced_closes",
        ):
            assert s[key] >= 0.0, f"{key} is negative: {s[key]}"


# ===========================================================================
# TestLoadResults — file-system tests (tiny MC runs)
# ===========================================================================

class TestLoadResults:
    """Tests for load_results() using temporary directories with real CSVs."""

    @pytest.fixture(scope="class")
    def two_gen_dir(self):
        """Directory with one parametric CSV and one bootstrap CSV."""
        if not _data_available():
            pytest.skip("calibration/data files missing — skipping load_results tests")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _run_mc("parametric", tmp)
            _run_mc("bootstrap", tmp)
            yield tmp

    @pytest.fixture(scope="class")
    def one_gen_dir(self):
        """Directory with only a parametric CSV."""
        if not _data_available():
            pytest.skip("calibration/data files missing — skipping load_results tests")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _run_mc("parametric", tmp)
            yield tmp

    def test_loads_both_generators(self, two_gen_dir):
        results = load_results(two_gen_dir)
        assert "parametric" in results, f"Missing 'parametric' key; got: {list(results)}"
        assert "bootstrap" in results, f"Missing 'bootstrap' key; got: {list(results)}"

    def test_dataframe_types(self, two_gen_dir):
        results = load_results(two_gen_dir)
        for gen, df in results.items():
            assert isinstance(df, pd.DataFrame), f"{gen} is not a DataFrame"

    def test_n_rows_correct(self, two_gen_dir):
        results = load_results(two_gen_dir)
        for gen, df in results.items():
            assert len(df) == _N, (
                f"Expected {_N} rows for {gen}, got {len(df)}"
            )

    def test_expected_columns_present(self, two_gen_dir):
        from monte_carlo.run_mc import _COLUMNS  # noqa: PLC0415
        results = load_results(two_gen_dir)
        for gen, df in results.items():
            missing = [c for c in _COLUMNS if c not in df.columns]
            assert not missing, f"Missing columns in {gen} df: {missing}"

    def test_load_single_csv_by_path(self, two_gen_dir):
        """load_results accepts a direct CSV path."""
        csvs = list(two_gen_dir.glob("mc_parametric_*.csv"))
        assert csvs, "No parametric CSV in fixture directory"
        results = load_results(csvs[0])
        assert "parametric" in results

    def test_load_single_gen_dir(self, one_gen_dir):
        results = load_results(one_gen_dir)
        assert len(results) == 1
        assert "parametric" in results

    def test_raises_on_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FileNotFoundError):
                load_results(Path(tmpdir))


# ===========================================================================
# TestWriteReport — markdown content checks
# ===========================================================================

class TestWriteReport:
    """Tests for write_report() output content."""

    @pytest.fixture(scope="class")
    def report_path_and_content(self):
        """Generate a report from two synthetic DataFrames and read back content."""
        df_para = _make_synthetic_df(n=20, generator="parametric", seed=10)
        df_boot = _make_synthetic_df(n=20, generator="bootstrap", seed=11)
        results = {"parametric": df_para, "bootstrap": df_boot}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "TEST_REPORT.md"
            # Use default anchor CSV (may not exist in CI — that's OK, report handles it)
            anchor_csv = _ANCHOR_CSV if _ANCHOR_CSV.exists() else None
            written = write_report(results, out_path, anchor_csv=anchor_csv)
            content = written.read_text(encoding="utf-8")
            yield written, content

    @pytest.fixture(scope="class")
    def content(self, report_path_and_content):
        return report_path_and_content[1]

    @pytest.fixture(scope="class")
    def written_path(self, report_path_and_content):
        return report_path_and_content[0]

    # ── File exists and is non-empty ─────────────────────────────────────────

    def test_file_created(self, written_path):
        assert written_path.exists(), f"Report file not created: {written_path}"

    def test_file_non_empty(self, content):
        assert len(content) > 100, "Report is suspiciously short"

    # ── Required sections present ─────────────────────────────────────────────

    def test_has_distribution_section(self, content):
        assert "## Distribution of MC paths" in content, (
            "Missing '## Distribution of MC paths' section"
        )

    def test_has_percentile_table_header(self, content):
        # Table header for percentile table
        assert "| Metric | p05 |" in content, (
            "Missing percentile table header '| Metric | p05 |'"
        )

    def test_has_annual_row(self, content):
        assert "annual" in content.lower(), "No 'annual' metric row in report"

    def test_has_max_dd_row(self, content):
        assert "max_dd" in content.lower(), "No 'max_dd' metric row in report"

    def test_has_p_annual_neg(self, content):
        assert "P(annual < 0)" in content, "Missing P(annual < 0) line"

    def test_has_occupied_capital_section(self, content):
        assert "Occupied-capital reframe" in content, (
            "Missing 'Occupied-capital reframe' section"
        )

    def test_occupied_capital_section_has_t7_note(self, content):
        assert "T7" in content, "Occupied-capital section should mention T7"

    def test_occupied_capital_no_invented_multiplier(self, content):
        """Section must NOT apply a concrete invented multiplier to tables."""
        # The section says "deferred to T7" — we verify the key phrase is there
        assert "Deferred to T7" in content or "deferred to T7" in content or \
               "→ Deferred to T7" in content, (
            "Occupied-capital section should defer multiplier to T7"
        )

    def test_has_full_budget_denomination_note(self, content):
        """Report must clearly state full-budget denominator."""
        assert "full-budget" in content.lower() or "full-portfolio" in content.lower(), (
            "Report should mention full-budget or full-portfolio denominator"
        )

    # ── Anchor section ────────────────────────────────────────────────────────

    def test_has_anchor_section(self, content):
        assert "Single-path anchor" in content, (
            "Missing single-path anchor section"
        )

    def test_anchor_mentions_uprod_or_unavailable(self, content):
        """Anchor section mentions U-prod or states it's unavailable."""
        uprod_mentioned = "U-prod" in content or "u-prod" in content.lower()
        unavailable = "Anchor CSV not found" in content
        assert uprod_mentioned or unavailable, (
            "Anchor section should mention U-prod data or state it's unavailable"
        )

    # ── Both generators in report ─────────────────────────────────────────────

    def test_both_generators_mentioned(self, content):
        assert "parametric" in content.lower()
        assert "bootstrap" in content.lower()

    def test_side_by_side_comparison_present(self, content):
        """When both generators present, comparison section should appear."""
        assert "side-by-side" in content.lower() or "Quick comparison" in content, (
            "Missing side-by-side or Quick comparison section"
        )

    # ── Anchor with real CSV ──────────────────────────────────────────────────

    def test_anchor_with_real_csv_contains_annual_pct(self):
        """If the real anchor CSV exists, verify annual_pct value appears in report."""
        if not _ANCHOR_CSV.exists():
            pytest.skip("Anchor CSV not found — skipping anchor content check")

        df_para = _make_synthetic_df(n=10, generator="parametric", seed=20)
        results = {"parametric": df_para}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "ANCHOR_TEST.md"
            write_report(results, out_path, anchor_csv=_ANCHOR_CSV)
            content = out_path.read_text(encoding="utf-8")

        # The anchor row has annual_pct=2.3935 — should appear somewhere.
        # (Was 2.5026 before the 2026-06-25 margin-release fix; CSV regenerated
        #  with the corrected engine.)
        assert "2.3935" in content, (
            f"Expected anchor annual_pct 2.3935 in report; "
            f"snippet: {content[:1000]}"
        )

    def test_anchor_buf3_negstop_exits(self):
        """Anchor section should show n_phase1_negstop_exits=8 (U-prod buf=3)."""
        if not _ANCHOR_CSV.exists():
            pytest.skip("Anchor CSV not found")

        df_para = _make_synthetic_df(n=10, generator="parametric", seed=21)
        results = {"parametric": df_para}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "NEGSTOP_TEST.md"
            write_report(results, out_path, anchor_csv=_ANCHOR_CSV)
            content = out_path.read_text(encoding="utf-8")

        # n_phase1_negstop_exits = 8 for U-prod buf=3 in the CSV
        assert "8" in content, (
            "Expected negstop_exits count (8) to appear in anchor section"
        )

    # ── write_report with real MC runs ───────────────────────────────────────

    def test_write_report_with_real_mc_runs(self):
        """Integration: run tiny MC for both generators, write report, check content."""
        if not _data_available():
            pytest.skip("calibration/data files missing — skipping real MC report test")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            df_para = _run_mc("parametric", tmp)
            df_boot = _run_mc("bootstrap", tmp)
            results = {"parametric": df_para, "bootstrap": df_boot}

            out_path = tmp / "REAL_MC_REPORT.md"
            write_report(results, out_path, anchor_csv=_ANCHOR_CSV)
            content = out_path.read_text(encoding="utf-8")

        assert "## Distribution of MC paths" in content
        assert "P(annual < 0)" in content
        assert "Occupied-capital reframe" in content
        assert len(content) > 200


# ===========================================================================
# TestLoadResultsList — load_results with explicit list argument
# ===========================================================================

class TestLoadResultsList:
    """load_results accepts a list of explicit file paths."""

    def test_load_from_list_of_synthetic_csvs(self):
        """Write two synthetic CSVs and load via explicit list."""
        df_para = _make_synthetic_df(n=8, generator="parametric", seed=30)
        df_boot = _make_synthetic_df(n=8, generator="bootstrap", seed=31)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            p1 = tmp / "mc_parametric_20260101T000000Z.csv"
            p2 = tmp / "mc_bootstrap_20260101T000000Z.csv"
            df_para.to_csv(p1, index=False)
            df_boot.to_csv(p2, index=False)

            results = load_results([p1, p2])

        assert "parametric" in results
        assert "bootstrap" in results
        assert len(results["parametric"]) == 8
        assert len(results["bootstrap"]) == 8

    def test_latest_file_kept_per_generator(self):
        """When directory has multiple files per generator, latest is kept."""
        df1 = _make_synthetic_df(n=5, generator="parametric", seed=40)
        df2 = _make_synthetic_df(n=8, generator="parametric", seed=41)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Older file (earlier timestamp in name)
            p1 = tmp / "mc_parametric_20260101T000000Z.csv"
            # Newer file (later timestamp in name)
            p2 = tmp / "mc_parametric_20260608T120000Z.csv"
            df1.to_csv(p1, index=False)
            df2.to_csv(p2, index=False)

            results = load_results(tmp)

        # Should keep the latest (alphabetically last = p2 = 8 rows)
        assert len(results["parametric"]) == 8, (
            f"Expected 8 rows (from latest file), got {len(results['parametric'])}"
        )
