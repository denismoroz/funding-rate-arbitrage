"""Unit tests for XsmomParams."""
from __future__ import annotations

import pytest

from frab.strategy.xsmom.params import XsmomParams


def _make_params(**kwargs) -> XsmomParams:
    defaults = dict(
        budget_cap=1000.0,
        universe=("BTC", "ETH", "SOL", "HYPE", "ZEC", "PURR"),
        leverage=1,
        margin_buffer_factor=3.0,
    )
    defaults.update(kwargs)
    return XsmomParams(**defaults)


# ── compute_k ────────────────────────────────────────────────────────────────

class TestComputeK:
    def test_auto_tercile_standard(self):
        """Auto mode: 6 coins → k=2 (6//3=2)."""
        p = _make_params(n_positions=None)
        assert p.compute_k(6) == 2

    def test_auto_tercile_7_coins(self):
        """Auto mode: 7 coins → k=2 (7//3=2)."""
        p = _make_params(n_positions=None)
        assert p.compute_k(7) == 2

    def test_auto_tercile_3_coins(self):
        """Auto mode: 3 coins → k=1 (3//3=1)."""
        p = _make_params(n_positions=None)
        assert p.compute_k(3) == 1

    def test_auto_minimum_1(self):
        """Auto mode: 1 coin → k=1 (floor to 1)."""
        p = _make_params(n_positions=None)
        assert p.compute_k(1) == 1

    def test_manual_even_n_positions(self):
        """Manual n_positions=6 → k=3."""
        p = _make_params(n_positions=6, auto=False)
        assert p.compute_k(10) == 3

    def test_manual_n_positions_2(self):
        """Manual n_positions=2 → k=1."""
        p = _make_params(n_positions=2, auto=False)
        assert p.compute_k(10) == 1

    def test_k_clamped_to_half_universe(self):
        """k must not exceed universe_len // 2."""
        # universe_len=4, manual n_positions=100 → k=50, but max_k=2
        p = _make_params(n_positions=100, auto=False)
        assert p.compute_k(4) == 2

    def test_k_minimum_1_when_small_universe(self):
        """universe_len=2, auto → k=1 (2//3=0 → max(1,0)=1)."""
        p = _make_params(n_positions=None)
        assert p.compute_k(2) == 1


# ── compute_notional_per_position ────────────────────────────────────────────

class TestComputeNotional:
    def test_basic(self):
        """budget=1000, k=2 → notional = (1000/2) / 2 = 250."""
        p = _make_params(budget_cap=1000.0)
        assert p.compute_notional_per_position(2) == pytest.approx(250.0)

    def test_k_1(self):
        """budget=1000, k=1 → notional = 500."""
        p = _make_params(budget_cap=1000.0)
        assert p.compute_notional_per_position(1) == pytest.approx(500.0)

    def test_k_5(self):
        """budget=2000, k=5 → notional = (2000/2)/5 = 200."""
        p = _make_params(budget_cap=2000.0)
        assert p.compute_notional_per_position(5) == pytest.approx(200.0)


# ── compute_required_margin ──────────────────────────────────────────────────

class TestComputeRequiredMargin:
    def test_leverage_1(self):
        """leverage=1, buffer=3, notional=300 → required=900."""
        p = _make_params(leverage=1, margin_buffer_factor=3.0)
        assert p.compute_required_margin(300.0) == pytest.approx(900.0)

    def test_leverage_10(self):
        """leverage=10, buffer=3, notional=1000 → required=(1000/10)*3=300."""
        p = _make_params(leverage=10, margin_buffer_factor=3.0)
        assert p.compute_required_margin(1000.0) == pytest.approx(300.0)

    def test_buffer_factor_2(self):
        """leverage=5, buffer=2, notional=500 → required=(500/5)*2=200."""
        p = _make_params(leverage=5, margin_buffer_factor=2.0)
        assert p.compute_required_margin(500.0) == pytest.approx(200.0)


# ── from_dict / to_dict round-trip ───────────────────────────────────────────

class TestSerialization:
    def test_from_dict_to_dict_round_trip(self):
        """from_dict(to_dict(p)) == p."""
        p = _make_params(
            budget_cap=5000.0,
            n_positions=4,
            auto=False,
            universe=("BTC", "ETH", "SOL"),
            leverage=3,
            margin_buffer_factor=2.5,
            lookbacks=(7, 14, 30),
            rebalance_days=14,
            anchor_dow=0,
        )
        d = p.to_dict()
        p2 = XsmomParams.from_dict(d)
        assert p2 == p

    def test_to_dict_tuples_become_lists(self):
        """JSON-serialisable: tuples → lists."""
        p = _make_params(universe=("BTC", "ETH"), lookbacks=(7, 14))
        d = p.to_dict()
        assert isinstance(d["universe"], list)
        assert isinstance(d["lookbacks"], list)

    def test_from_dict_lists_become_tuples(self):
        """Lists in dict → tuples in dataclass."""
        d = {
            "budget_cap": 1000.0,
            "universe": ["BTC", "ETH"],
            "lookbacks": [14, 21, 30],
        }
        p = XsmomParams.from_dict(d)
        assert isinstance(p.universe, tuple)
        assert isinstance(p.lookbacks, tuple)

    def test_from_dict_unknown_keys_ignored(self):
        """Unknown keys in dict are silently ignored."""
        d = {
            "budget_cap": 1000.0,
            "universe": ["BTC"],
            "nonexistent_key": "ignored",
        }
        p = XsmomParams.from_dict(d)
        assert p.budget_cap == 1000.0

    def test_asdict_alias(self):
        """asdict() is an alias for to_dict()."""
        p = _make_params()
        assert p.asdict() == p.to_dict()

    def test_frozen_structural_equality(self):
        """Two params with same values are equal (frozen dataclass)."""
        p1 = _make_params(budget_cap=999.0)
        p2 = _make_params(budget_cap=999.0)
        assert p1 == p2

    def test_frozen_different_values_not_equal(self):
        p1 = _make_params(budget_cap=100.0)
        p2 = _make_params(budget_cap=200.0)
        assert p1 != p2
