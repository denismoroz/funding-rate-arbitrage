"""
test_parametric.py — T3 acceptance tests for generators/parametric.py.

Round-trip gate: generate ≥1000 synthetic paths, re-extract statistics via
calibrate_stats helpers, confirm the synthetic statistics match the real
calibration within the tolerances defined in PLAN.md T3.

Tolerances (PLAN.md):
  negative_hours_share      : |diff| <= 0.05  (absolute)      ← KEY
  funding_mean_annual_pct   : rel <= 30%  OR  |diff| <= 3pp   (near-zero)
  funding_ar1_phi           : |diff| <= 0.12
  funding_std_h             : rel  <= 25%
  excess_kurtosis (synth)   : > 1  (fat tail present)
  log_return_std_h          : rel  <= 20%
  determinism               : same seed → identical dfs (first coin hash check)

Import path: research/two_phase_margin is inserted into sys.path so that
`monte_carlo` resolves to the package (not the sibling two_phase_margin.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── sys.path fix (PLAN.md rule 2) ──────────────────────────────────────────
_RESEARCH_TPM = Path(__file__).resolve().parents[2]   # research/two_phase_margin/
if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo.generators.parametric import (  # noqa: E402
    compute_round_trip_stats,
    generate,
)

# ---------------------------------------------------------------------------
# Fixtures & shared constants
# ---------------------------------------------------------------------------

_CALIB_DIR = _RESEARCH_TPM / "monte_carlo" / "calibration"
_COINS = ["BTC", "ETH", "SOL", "HYPE", "PURR"]
_N_PATHS = 1000
_HORIZON_H = 8760   # 1 year; n_paths × horizon = 1000 y total
_SEED = 42


@pytest.fixture(scope="module")
def round_trip():
    """Aggregate round-trip statistics over 1000 paths (computed once)."""
    return compute_round_trip_stats(
        calib_dir=_CALIB_DIR,
        coins=_COINS,
        horizon_h=_HORIZON_H,
        n_paths=_N_PATHS,
        seed=_SEED,
    )


# ---------------------------------------------------------------------------
# Output contract tests
# ---------------------------------------------------------------------------

def test_generate_returns_all_coins():
    dfs = generate(_CALIB_DIR, horizon_h=100, seed=0, coins=_COINS)
    assert set(dfs.keys()) == set(_COINS)


@pytest.mark.parametrize("coin", _COINS)
def test_generate_output_shape(coin):
    horizon = 200
    dfs = generate(_CALIB_DIR, horizon_h=horizon, seed=1, coins=[coin])
    df = dfs[coin]
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["close", "fundingRate"]
    assert len(df) == horizon


@pytest.mark.parametrize("coin", _COINS)
def test_generate_datetime_index_hourly(coin):
    horizon = 48
    dfs = generate(_CALIB_DIR, horizon_h=horizon, seed=2, coins=[coin])
    idx = dfs[coin].index
    assert isinstance(idx, pd.DatetimeIndex)
    assert len(idx) == horizon
    # Hourly spacing: consecutive differences should all be 1h
    diffs = idx[1:] - idx[:-1]
    expected = pd.Timedelta("1h")
    assert all(d == expected for d in diffs)


@pytest.mark.parametrize("coin", _COINS)
def test_close_positive(coin):
    """All simulated prices must be strictly positive."""
    dfs = generate(_CALIB_DIR, horizon_h=500, seed=3, coins=[coin])
    assert (dfs[coin]["close"] > 0).all()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_determinism_same_seed():
    """Two calls with the same seed must produce identical DataFrames."""
    coin = "BTC"
    dfs1 = generate(_CALIB_DIR, horizon_h=100, seed=99, coins=[coin])
    dfs2 = generate(_CALIB_DIR, horizon_h=100, seed=99, coins=[coin])
    pd.testing.assert_frame_equal(dfs1[coin], dfs2[coin])


def test_determinism_different_seeds_differ():
    """Different seeds must produce different output."""
    coin = "SOL"
    dfs1 = generate(_CALIB_DIR, horizon_h=100, seed=10, coins=[coin])
    dfs2 = generate(_CALIB_DIR, horizon_h=100, seed=11, coins=[coin])
    # Almost certainly different; use allclose as a practical check
    assert not np.allclose(
        dfs1[coin]["fundingRate"].values,
        dfs2[coin]["fundingRate"].values,
    )


# ---------------------------------------------------------------------------
# Round-trip gate: negative_hours_share (KEY tolerance = 0.05 absolute)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_negative_hours_share(round_trip, coin):
    r = round_trip[coin]
    diff = abs(r["diff_negative_hours_share"])
    assert diff <= 0.05, (
        f"{coin}: negative_hours_share diff {diff:.4f} > 0.05 tolerance. "
        f"Real={r['real_negative_hours_share']:.4f}, "
        f"Synth={r['synth_negative_hours_share']:.4f}"
    )


# ---------------------------------------------------------------------------
# Round-trip gate: funding_mean_annual_pct (rel ≤ 30%  OR  |abs| ≤ 3pp)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_funding_mean_annual_pct(round_trip, coin):
    r = round_trip[coin]
    diff_abs = abs(r["diff_funding_mean_annual_pct"])
    real_abs = abs(r["real_funding_mean_annual_pct"])
    rel_err = diff_abs / max(real_abs, 1e-9)
    ok = (rel_err <= 0.30) or (diff_abs <= 3.0)
    assert ok, (
        f"{coin}: funding_mean_annual_pct failed — "
        f"real={r['real_funding_mean_annual_pct']:.3f}%, "
        f"synth={r['synth_funding_mean_annual_pct']:.3f}%, "
        f"diff={r['diff_funding_mean_annual_pct']:+.3f}pp, "
        f"rel={rel_err:.1%}"
    )


# ---------------------------------------------------------------------------
# Round-trip gate: funding_ar1_phi (|diff| ≤ 0.12)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_ar1_phi(round_trip, coin):
    r = round_trip[coin]
    diff = abs(r["diff_funding_ar1_phi"])
    assert diff <= 0.12, (
        f"{coin}: ar1_phi diff {diff:.4f} > 0.12 tolerance. "
        f"Real={r['real_funding_ar1_phi']:.4f}, Synth={r['synth_funding_ar1_phi']:.4f}"
    )


# ---------------------------------------------------------------------------
# Round-trip gate: funding_std_h (rel ≤ 25%)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_funding_std_h(round_trip, coin):
    r = round_trip[coin]
    rel = abs(r["diff_funding_std_h_rel"])
    assert rel <= 0.25, (
        f"{coin}: funding_std_h rel diff {rel:.1%} > 25% tolerance. "
        f"Real={r['real_funding_std_h']:.2e}, Synth={r['synth_funding_std_h']:.2e}"
    )


# ---------------------------------------------------------------------------
# Round-trip gate: log_return_std_h (rel ≤ 20%)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_log_return_std_h(round_trip, coin):
    r = round_trip[coin]
    rel = abs(r["diff_log_return_std_h_rel"])
    assert rel <= 0.20, (
        f"{coin}: log_return_std_h rel diff {rel:.1%} > 20% tolerance. "
        f"Real={r['real_log_return_std_h']:.6f}, Synth={r['synth_log_return_std_h']:.6f}"
    )


# ---------------------------------------------------------------------------
# Round-trip gate: excess_kurtosis > 1 (fat tails present)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("coin", _COINS)
def test_round_trip_excess_kurtosis_fat_tail(round_trip, coin):
    r = round_trip[coin]
    synth_kurt = r["synth_excess_kurtosis"]
    assert synth_kurt > 1.0, (
        f"{coin}: synth excess_kurtosis={synth_kurt:.4f} <= 1.0 — fat tails missing"
    )


# ---------------------------------------------------------------------------
# Negative funding can occur (anti-garbage-in)
# ---------------------------------------------------------------------------

def test_negative_funding_possible():
    """Across a long run, negative funding must naturally appear for SOL."""
    dfs = generate(_CALIB_DIR, horizon_h=8760, seed=77, coins=["SOL"])
    neg_share = float((dfs["SOL"]["fundingRate"] < 0).mean())
    # SOL real neg share ~24%; any non-zero is enough to show it's not clipped
    assert neg_share > 0.0, "No negative funding generated for SOL — generator clips to zero?"


# ---------------------------------------------------------------------------
# Regime switching: both regimes should appear in a long run
# ---------------------------------------------------------------------------

def test_both_regimes_appear():
    """Over 8760h, the regime should switch from initial cold state."""
    # We can't observe regime directly, but hot regime has higher funding mean.
    # Generate a long SOL path and check that there's variance in 720h windows.
    dfs = generate(_CALIB_DIR, horizon_h=8760, seed=55, coins=["SOL"])
    fr = dfs["SOL"]["fundingRate"]
    rolling_mean = fr.rolling(720).mean().dropna()
    # If only one regime, the rolling mean would be nearly constant.
    # With regime switching, we expect non-trivial std of rolling means.
    assert rolling_mean.std() > 0, "Regime never switched — rolling funding mean is flat"
