"""Unit tests for TwoPhaseParams compute_size_for / compute_required_margin_for / compute_footprint."""
from __future__ import annotations

import pytest

from frab.constants import CoinMarginSpec
from frab.settings import Settings
from frab.strategy.two_phase.params import TwoPhaseParams


def _make_params(budget=100.0, K=3, buffer=3.0) -> TwoPhaseParams:
    return TwoPhaseParams(
        budget_cap_usdc=budget,
        concurrency_cap=K,
        margin_buffer_factor=buffer,
    )


def _make_settings(mocker, leverage: int, maint_ratio: float = 0.025):
    settings = mocker.MagicMock(spec=Settings)
    settings.get_coin_spec.return_value = CoinMarginSpec(leverage=leverage, maint_ratio=maint_ratio)
    return settings


def test_compute_size_for_btc_high_leverage(mocker):
    """budget=100, K=3, buffer=3, BTC leverage=20 → size = (100/3) / (1+3/20) ≈ 28.985507."""
    params = _make_params(budget=100, K=3, buffer=3)
    settings = _make_settings(mocker, leverage=20)
    result = params.compute_size_for("BTC", settings)
    assert result == pytest.approx(28.985507, rel=1e-5)


def test_compute_size_for_sol_mid_leverage(mocker):
    """budget=100, K=3, buffer=3, SOL leverage=10 → size = (100/3) / (1+3/10) ≈ 25.641026."""
    params = _make_params(budget=100, K=3, buffer=3)
    settings = _make_settings(mocker, leverage=10)
    result = params.compute_size_for("SOL", settings)
    assert result == pytest.approx(25.641026, rel=1e-5)


def test_compute_size_for_low_leverage(mocker):
    """budget=100, K=3, buffer=3, DOGE leverage=5 → size = (100/3) / (1+3/5) ≈ 20.833333."""
    params = _make_params(budget=100, K=3, buffer=3)
    settings = _make_settings(mocker, leverage=5)
    result = params.compute_size_for("DOGE", settings)
    assert result == pytest.approx(20.833333, rel=1e-5)


@pytest.mark.parametrize("leverage,coin", [(20, "BTC"), (10, "SOL"), (5, "DOGE")])
def test_compute_required_margin_for_keeps_invariant(mocker, leverage, coin):
    """For any coin, size + required_margin == budget/K."""
    params = _make_params(budget=100, K=3, buffer=3)
    settings = _make_settings(mocker, leverage=leverage)
    size = params.compute_size_for(coin, settings)
    margin = params.compute_required_margin_for(coin, settings)
    assert size + margin == pytest.approx(params.budget_cap_usdc / params.concurrency_cap, rel=1e-9)


def test_compute_footprint_equals_budget_div_k():
    """compute_footprint() == budget_cap_usdc / concurrency_cap."""
    params = _make_params(budget=100, K=3)
    assert params.compute_footprint() == pytest.approx(100.0 / 3, rel=1e-9)


def test_compute_footprint_independent_of_coin():
    """compute_footprint() takes no coin argument — same value regardless of coin."""
    params = _make_params(budget=900, K=3)
    assert params.compute_footprint() == pytest.approx(300.0, rel=1e-9)


def test_from_dict_ignores_perp_leverage():
    """from_dict silently ignores legacy perp_leverage key."""
    params = TwoPhaseParams.from_dict({"perp_leverage": 5.0, "coins": ["BTC"]})
    assert params.coins == ["BTC"]
