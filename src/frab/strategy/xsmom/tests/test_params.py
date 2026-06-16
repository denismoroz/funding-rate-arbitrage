"""Unit tests for XsmomParams."""
from __future__ import annotations

import math

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


# ── sizing_breakdown ─────────────────────────────────────────────────────────

class TestSizingBreakdown:
    """Tests for the envelope sizing formula (single source of truth)."""

    # ── reserve floor vs 8% ────────────────────────────────────────────────

    def test_reserve_uses_8_pct_when_above_floor(self):
        """budget=1000 → 8% = 80 > 20 → reserve=80."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=1000.0)
        assert bd["reserve"] == pytest.approx(80.0)

    def test_reserve_uses_floor_when_8pct_below_20(self):
        """budget=200 → 8% = 16 < 20 → reserve=20."""
        p = _make_params(budget_cap=200.0)
        bd = p.sizing_breakdown(6, wallet=200.0)
        assert bd["reserve"] == pytest.approx(20.0)

    def test_reserve_exactly_at_crossover(self):
        """budget=250 → 8% = 20 == 20 → reserve=20."""
        p = _make_params(budget_cap=250.0)
        bd = p.sizing_breakdown(6, wallet=250.0)
        assert bd["reserve"] == pytest.approx(20.0)

    def test_reserve_just_above_crossover(self):
        """budget=260 → 8% = 20.8 > 20 → reserve=20.8."""
        p = _make_params(budget_cap=260.0)
        bd = p.sizing_breakdown(6, wallet=260.0)
        assert bd["reserve"] == pytest.approx(20.8)

    # ── wallet cap (effective) ─────────────────────────────────────────────

    def test_effective_capped_by_wallet_below_budget(self):
        """wallet < budget → effective = wallet, book shrinks."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=400.0)
        assert bd["effective"] == pytest.approx(400.0)
        assert bd["book"] == pytest.approx(400.0 - 80.0)  # reserve = 8% of budget = 80

    def test_effective_uses_budget_when_wallet_exceeds(self):
        """wallet > budget → effective = budget_cap."""
        p = _make_params(budget_cap=500.0)
        bd = p.sizing_breakdown(6, wallet=2000.0)
        assert bd["effective"] == pytest.approx(500.0)
        assert bd["book"] == pytest.approx(500.0 - 40.0)  # 8% of 500

    def test_effective_equals_budget_when_wallet_none(self):
        """wallet=None (preview mode) → effective = budget_cap."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=None)
        assert bd["effective"] == pytest.approx(1000.0)

    # ── book and per_side ──────────────────────────────────────────────────

    def test_book_is_effective_minus_reserve(self):
        """budget=1000, wallet=1000 → book = 1000 - 80 = 920."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=1000.0)
        assert bd["book"] == pytest.approx(920.0)
        assert bd["per_side"] == pytest.approx(460.0)
        assert bd["long"] == pytest.approx(460.0)
        assert bd["short"] == pytest.approx(460.0)

    def test_book_floor_is_zero(self):
        """When wallet is tiny, book doesn't go negative."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=10.0)
        assert bd["book"] >= 0.0

    # ── min-leg clamp reduces k ────────────────────────────────────────────

    def test_min_leg_clamp_reduces_k(self):
        """Very small book → k is reduced so each leg stays >= MIN_LEG."""
        # budget=100, reserve=20, book=80, per_side=40
        # auto k=6//3=2, max_k=floor(40/12)=3, so k=min(2,3)=2, per_leg=20 >= 12 → ok
        # Let's use budget=50, wallet=50, reserve=20, book=30, per_side=15
        # auto k=2, max_k=floor(15/12)=1 → k=min(2,1)=1, per_leg=15 >= 12 → ok
        p = _make_params(budget_cap=50.0, n_positions=None)
        bd = p.sizing_breakdown(6, wallet=50.0)
        assert bd["k"] == 1   # clamped from k_req=2
        assert bd["k_requested"] == 2
        assert bd["per_leg"] >= 12.0
        assert bd["min_leg_ok"] is True

    def test_min_leg_ok_false_when_book_too_small(self):
        """When even k=1 gives per_leg < MIN_LEG, min_leg_ok is False."""
        # budget=30, wallet=30, reserve=20, book=10, per_side=5 → per_leg=5 < 12
        p = _make_params(budget_cap=30.0)
        bd = p.sizing_breakdown(6, wallet=30.0)
        assert bd["min_leg_ok"] is False
        assert bd["per_leg"] < bd["min_leg"]

    def test_k_requested_exposed(self):
        """k_requested reflects compute_k before the min-leg clamp."""
        p = _make_params(budget_cap=1000.0, n_positions=None)
        bd = p.sizing_breakdown(6, wallet=1000.0)
        assert bd["k_requested"] == p.compute_k(6)

    # ── free computation ───────────────────────────────────────────────────

    def test_free_is_wallet_minus_book(self):
        """free = wallet - book (non-negative)."""
        p = _make_params(budget_cap=500.0)
        # wallet=2000, effective=500, reserve=40, book=460
        bd = p.sizing_breakdown(6, wallet=2000.0)
        assert bd["free"] == pytest.approx(2000.0 - bd["book"])

    def test_free_is_none_when_wallet_none(self):
        """wallet=None → free=None (preview mode)."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=None)
        assert bd["free"] is None

    def test_free_floor_is_zero(self):
        """free never goes negative even if wallet is tiny."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=5.0)
        # book = max(0, effective - reserve) → effective = min(1000, 5)=5
        # reserve = max(20, 0.08*1000) = 80; book = max(0, 5-80) = 0 → free=5-0=5
        assert bd["free"] is not None
        assert bd["free"] >= 0.0

    # ── min_leg constant exposed ───────────────────────────────────────────

    def test_min_leg_constant(self):
        """min_leg key is present and equals the spec value of 12.0."""
        p = _make_params(budget_cap=1000.0)
        bd = p.sizing_breakdown(6, wallet=1000.0)
        assert bd["min_leg"] == pytest.approx(12.0)

    # ── happy-path nominal check ───────────────────────────────────────────

    def test_nominal_1000_budget_6_coins(self):
        """budget=1000, wallet=1000, 6-coin universe → sensible breakdown."""
        p = _make_params(budget_cap=1000.0, n_positions=None)
        bd = p.sizing_breakdown(6, wallet=1000.0)
        # reserve = 8% of 1000 = 80; book = 920; per_side = 460
        # auto k = 6//3 = 2; max_k = floor(460/12) = 38 → k=2
        # per_leg = 460/2 = 230
        assert bd["reserve"] == pytest.approx(80.0)
        assert bd["book"] == pytest.approx(920.0)
        assert bd["k"] == 2
        assert bd["per_leg"] == pytest.approx(230.0)
        assert bd["min_leg_ok"] is True
        assert bd["free"] == pytest.approx(80.0)  # wallet - book = 1000 - 920 = 80 (= reserve)

    def test_manual_n_positions_respected(self):
        """Manual n_positions=4 → k=2 regardless of universe size."""
        p = _make_params(budget_cap=1000.0, n_positions=4)
        bd = p.sizing_breakdown(20, wallet=1000.0)
        assert bd["k_requested"] == 2  # n_positions=4 → k=4//2=2
        assert bd["k"] == 2
