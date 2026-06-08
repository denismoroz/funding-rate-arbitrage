"""
test_calibration.py — Tests for calibrate_stats.py (T2 acceptance).

Recomputes calibration on the REAL research/data/ history into a temp dir, so the
test exercises the extraction logic itself (not the committed JSON artifacts).

Import resolution: put research/two_phase_margin on sys.path so `monte_carlo`
resolves to the package, not the sibling two_phase_margin.py file (PLAN.md rule 2).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_RESEARCH_TPM = Path(__file__).resolve().parents[2]  # research/two_phase_margin/
if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo.calibrate_stats import calibrate_all  # noqa: E402

_DATA_DIR = _RESEARCH_TPM.parent / "data"  # research/data (TPM = research/two_phase_margin)
_COINS = ["BTC", "ETH", "SOL", "HYPE", "PURR"]

_REQUIRED_FIELDS = {
    "log_return_mean_h", "log_return_std_h", "excess_kurtosis", "jump_freq",
    "price_history_start", "price_history_end", "price_history_note",
    "funding_mean_h", "funding_std_h", "funding_ar1_phi",
    "negative_hours_share", "funding_mean_annual_pct",
    "regime_criterion", "regime_hot_funding_annual_pct",
    "regime_cold_funding_annual_pct", "regime_transition_freq", "regime_note",
    "cold_window_funding_annual_pct", "corr_price_funding",
}


@pytest.fixture(scope="module")
def calib(tmp_path_factory):
    """Calibrate all coins once into a temp dir; shared across tests in this module."""
    out = tmp_path_factory.mktemp("calibration")
    return calibrate_all(_COINS, _DATA_DIR, out), out


# ── Structure / ranges ────────────────────────────────────────────────────────

@pytest.mark.parametrize("coin", _COINS)
def test_all_required_fields_present(calib, coin):
    results, _ = calib
    assert _REQUIRED_FIELDS.issubset(results[coin].keys()), \
        f"{coin}: missing {_REQUIRED_FIELDS - set(results[coin].keys())}"


@pytest.mark.parametrize("coin", _COINS)
def test_field_ranges(calib, coin):
    results, _ = calib
    c = results[coin]
    assert 0.0 <= c["negative_hours_share"] <= 1.0
    assert c["funding_std_h"] >= 0.0
    assert c["log_return_std_h"] >= 0.0
    assert -1.0 < c["funding_ar1_phi"] < 1.0
    assert 0.0 <= c["jump_freq"] <= 1.0
    assert -1.0 <= c["corr_price_funding"] <= 1.0
    # regime transition freq is a probability when present
    rtf = c["regime_transition_freq"]
    if rtf is not None:
        assert 0.0 <= rtf <= 1.0


def test_files_written(calib):
    _, out = calib
    for coin in _COINS:
        assert (out / f"{coin}.json").exists()
    assert (out / "_cross_funding_corr.json").exists()


def test_short_price_history_flagged(calib):
    """HYPE/PURR have OHLCV only from 2025-11 → price_history_note must be non-empty."""
    results, _ = calib
    for coin in ("HYPE", "PURR"):
        assert results[coin]["price_history_note"], \
            f"{coin}: expected a short-history note"
    # Majors have long history → empty note
    for coin in ("BTC", "ETH", "SOL"):
        assert results[coin]["price_history_note"] == ""


# ── Sanity anchors (the anti-garbage-in floor) ────────────────────────────────

def test_sol_cold_window_anchor(calib):
    """SOL cold-window funding ≈ 2.71% (regime_comparison.csv hl_cold=2.708)."""
    results, _ = calib
    sol_cold = results["SOL"]["cold_window_funding_annual_pct"]
    assert sol_cold == pytest.approx(2.708, abs=1.5), \
        f"SOL cold-window {sol_cold:.3f}% off anchor 2.708% by >1.5pp"


def test_btc_cold_window_anchor(calib):
    """BTC cold-window funding ≈ 9.2% (soft anchor, ±2pp)."""
    results, _ = calib
    btc_cold = results["BTC"]["cold_window_funding_annual_pct"]
    assert btc_cold == pytest.approx(9.202, abs=2.0)


def test_negative_hours_share_nontrivial(calib):
    """The whole point of the generator is to reproduce negative funding — make sure
    the calibration actually sees it (SOL is the most negative coin in our set)."""
    results, _ = calib
    assert results["SOL"]["negative_hours_share"] > 0.10
    # SOL cold regime mean should be near zero / negative (it flips negative in cold)
    assert results["SOL"]["regime_cold_funding_annual_pct"] < 5.0


# ── Cross-coin funding correlation matrix ─────────────────────────────────────

def test_cross_corr_matrix_valid(calib):
    import json
    _, out = calib
    data = json.loads((out / "_cross_funding_corr.json").read_text())
    coins = data["coins"]
    m = data["matrix"]
    n = len(coins)
    assert coins == _COINS
    assert len(m) == n and all(len(row) == n for row in m)
    for i in range(n):
        assert m[i][i] == pytest.approx(1.0, abs=1e-9)        # unit diagonal
        for j in range(n):
            assert m[i][j] == pytest.approx(m[j][i], abs=1e-9)  # symmetric
            assert -1.0 <= m[i][j] <= 1.0
    assert data["common_hours"] > 0
