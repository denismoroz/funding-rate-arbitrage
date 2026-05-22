"""Tests for MarginManager pure-logic class."""
from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from frab.engine.margin_manager import (
    PERP_TAKER,
    SPOT_TAKER,
    MarginManager,
    OpenPosition,
    PerCoinSpec,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BTC_SPEC = PerCoinSpec(position_size_usd=100.0, leverage=10, maint_ratio=0.01)
ETH_SPEC = PerCoinSpec(position_size_usd=200.0, leverage=5, maint_ratio=0.02)
SOL_SPEC = PerCoinSpec(position_size_usd=50.0, leverage=20, maint_ratio=0.005)


def make_manager(**overrides) -> MarginManager:
    """Return a MarginManager with sensible defaults, optionally overridden."""
    defaults = dict(
        per_coin_params={"BTC": BTC_SPEC, "ETH": ETH_SPEC},
        margin_buffer_x=3.0,
        top_up_trigger=2.0,
        healthy_ratio=3.0,
        budget_cap_usd=1000.0,
    )
    defaults.update(overrides)
    return MarginManager(**defaults)


def btc_open(
    spot_units: float = 1.0,
    short_size: float = 1.0,
    entry_perp_price: float = 100.0,
    required_margin: float = 30.0,
) -> OpenPosition:
    return OpenPosition(
        coin="BTC",
        spot_units=spot_units,
        short_size=short_size,
        entry_perp_price=entry_perp_price,
        required_margin=required_margin,
    )


def eth_open(
    spot_units: float = 2.0,
    short_size: float = 2.0,
    entry_perp_price: float = 100.0,
    required_margin: float = 120.0,
) -> OpenPosition:
    return OpenPosition(
        coin="ETH",
        spot_units=spot_units,
        short_size=short_size,
        entry_perp_price=entry_perp_price,
        required_margin=required_margin,
    )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_per_coin_spec_frozen(self):
        spec = PerCoinSpec(100.0, 10, 0.01)
        with pytest.raises(FrozenInstanceError):
            spec.leverage = 99  # type: ignore[misc]

    def test_open_position_frozen(self):
        pos = btc_open()
        with pytest.raises(FrozenInstanceError):
            pos.coin = "ETH"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_empty_per_coin_params(self):
        with pytest.raises(ValueError, match="not be empty"):
            MarginManager(
                per_coin_params={},
                margin_buffer_x=3.0,
                top_up_trigger=2.0,
                healthy_ratio=3.0,
                budget_cap_usd=1000.0,
            )

    def test_margin_buffer_x_below_one(self):
        with pytest.raises(ValueError, match="margin_buffer_x"):
            make_manager(margin_buffer_x=0.5)

    def test_top_up_trigger_equals_one(self):
        with pytest.raises(ValueError, match="top_up_trigger"):
            make_manager(top_up_trigger=1.0)

    def test_top_up_trigger_equals_healthy_ratio(self):
        with pytest.raises(ValueError, match="top_up_trigger"):
            make_manager(top_up_trigger=3.0, healthy_ratio=3.0)

    def test_budget_cap_usd_zero(self):
        with pytest.raises(ValueError, match="budget_cap_usd"):
            make_manager(budget_cap_usd=0.0)

    def test_bad_spec_negative_leverage(self):
        with pytest.raises(ValueError, match="leverage"):
            make_manager(
                per_coin_params={"BTC": PerCoinSpec(100.0, 0, 0.01)}
            )

    def test_bad_spec_zero_maint_ratio(self):
        with pytest.raises(ValueError, match="maint_ratio"):
            make_manager(
                per_coin_params={"BTC": PerCoinSpec(100.0, 10, 0.0)}
            )

    def test_bad_spec_zero_position_size(self):
        with pytest.raises(ValueError, match="position_size_usd"):
            make_manager(
                per_coin_params={"BTC": PerCoinSpec(0.0, 10, 0.01)}
            )

    def test_valid_construction(self):
        m = make_manager()
        assert m.margin_buffer_x == 3.0
        assert m.healthy_ratio == 3.0


# ---------------------------------------------------------------------------
# compute_pair_footprint
# ---------------------------------------------------------------------------


class TestComputePairFootprint:
    def test_btc_known_math(self):
        # $100 / 10x * 3.0 buffer = $30 margin; spot = $100
        m = make_manager()
        spot, margin = m.compute_pair_footprint("BTC")
        assert spot == pytest.approx(100.0)
        assert margin == pytest.approx(30.0)  # 100/10*3

    def test_eth_known_math(self):
        # $200 / 5x * 3.0 buffer = $120 margin
        m = make_manager()
        spot, margin = m.compute_pair_footprint("ETH")
        assert spot == pytest.approx(200.0)
        assert margin == pytest.approx(120.0)  # 200/5*3

    def test_unknown_coin_raises(self):
        m = make_manager()
        with pytest.raises(KeyError):
            m.compute_pair_footprint("SOL")

    def test_buffer_x_affects_margin(self):
        m = make_manager(margin_buffer_x=1.0)
        _, margin = m.compute_pair_footprint("BTC")
        assert margin == pytest.approx(10.0)  # 100/10*1


# ---------------------------------------------------------------------------
# compute_required_margin_for_open
# ---------------------------------------------------------------------------


class TestComputeRequiredMarginForOpen:
    def test_equals_perp_margin_from_footprint(self):
        m = make_manager()
        _, expected = m.compute_pair_footprint("BTC")
        assert m.compute_required_margin_for_open("BTC") == pytest.approx(expected)

    def test_unknown_coin_raises(self):
        m = make_manager()
        with pytest.raises(KeyError):
            m.compute_required_margin_for_open("DOGE")


# ---------------------------------------------------------------------------
# compute_total_maintenance
# ---------------------------------------------------------------------------


class TestComputeTotalMaintenance:
    def test_empty_opens_returns_zero(self):
        m = make_manager()
        result = m.compute_total_maintenance([], {"BTC": 100.0})
        assert result == 0.0

    def test_one_position_known_value(self):
        # short_size=2.0, price=100.0, maint_ratio=0.01 => 2*100*0.01 = 2.0
        m = make_manager()
        pos = btc_open(short_size=2.0, entry_perp_price=100.0)
        result = m.compute_total_maintenance([pos], {"BTC": 100.0})
        assert result == pytest.approx(2.0)

    def test_two_positions(self):
        m = make_manager()
        b = btc_open(short_size=1.0)  # 1*100*0.01 = 1.0
        e = eth_open(short_size=2.0)  # 2*200*0.02 = 8.0
        result = m.compute_total_maintenance([b, e], {"BTC": 100.0, "ETH": 200.0})
        assert result == pytest.approx(9.0)

    def test_missing_price_raises(self):
        m = make_manager()
        pos = btc_open()
        with pytest.raises(KeyError):
            m.compute_total_maintenance([pos], {})

    def test_missing_spec_raises(self):
        # Manager with only ETH; position claims BTC
        m = make_manager(per_coin_params={"ETH": ETH_SPEC})
        pos = btc_open()
        with pytest.raises(KeyError):
            m.compute_total_maintenance([pos], {"BTC": 100.0})


# ---------------------------------------------------------------------------
# compute_perp_unrealized
# ---------------------------------------------------------------------------


class TestComputePerpUnrealized:
    def test_price_unchanged_zero_pnl(self):
        m = make_manager()
        pos = btc_open(short_size=1.0, entry_perp_price=100.0)
        result = m.compute_perp_unrealized([pos], {"BTC": 100.0})
        assert result == pytest.approx(0.0)

    def test_price_rose_negative_pnl(self):
        # short: price went from 100 -> 110, loss = 1*(100-110) = -10
        m = make_manager()
        pos = btc_open(short_size=1.0, entry_perp_price=100.0)
        result = m.compute_perp_unrealized([pos], {"BTC": 110.0})
        assert result == pytest.approx(-10.0)

    def test_price_fell_positive_pnl(self):
        # short: price went from 100 -> 90, gain = 1*(100-90) = 10
        m = make_manager()
        pos = btc_open(short_size=1.0, entry_perp_price=100.0)
        result = m.compute_perp_unrealized([pos], {"BTC": 90.0})
        assert result == pytest.approx(10.0)

    def test_missing_price_raises(self):
        m = make_manager()
        pos = btc_open()
        with pytest.raises(KeyError):
            m.compute_perp_unrealized([pos], {})

    def test_empty_opens_zero(self):
        m = make_manager()
        assert m.compute_perp_unrealized([], {}) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_margin_ratio
# ---------------------------------------------------------------------------


class TestComputeMarginRatio:
    def test_empty_opens_returns_inf(self):
        m = make_manager()
        ratio = m.compute_margin_ratio(500.0, [], {})
        assert math.isinf(ratio)

    def test_healthy_state(self):
        # maint = 1*100*0.01 = 1.0; unrealized = 0; ratio = 300/1 = 300
        m = make_manager()
        pos = btc_open(short_size=1.0, entry_perp_price=100.0)
        ratio = m.compute_margin_ratio(300.0, [pos], {"BTC": 100.0})
        assert ratio == pytest.approx(300.0)

    def test_near_liquidation(self):
        # price moved to 150; maint = 1*150*0.01 = 1.5
        # unrealized = 1*(100-150) = -50
        # ratio = (10 + -50) / 1.5 = -40/1.5 = -26.667
        m = make_manager()
        pos = btc_open(short_size=1.0, entry_perp_price=100.0)
        ratio = m.compute_margin_ratio(10.0, [pos], {"BTC": 150.0})
        assert ratio == pytest.approx(-40.0 / 1.5)

    def test_missing_price_raises(self):
        m = make_manager()
        pos = btc_open()
        with pytest.raises(KeyError):
            m.compute_margin_ratio(100.0, [pos], {})


# ---------------------------------------------------------------------------
# compute_budget_committed
# ---------------------------------------------------------------------------


class TestComputeBudgetCommitted:
    def test_no_opens(self):
        m = make_manager()
        result = m.compute_budget_committed([], perp_cash=50.0)
        assert result == pytest.approx(50.0)

    def test_one_open(self):
        # BTC position_size_usd=100; perp_cash=30 => 130
        m = make_manager()
        result = m.compute_budget_committed([btc_open()], perp_cash=30.0)
        assert result == pytest.approx(130.0)

    def test_two_opens(self):
        # BTC=100 + ETH=200 + perp_cash=50 => 350
        m = make_manager()
        result = m.compute_budget_committed(
            [btc_open(), eth_open()], perp_cash=50.0
        )
        assert result == pytest.approx(350.0)


# ---------------------------------------------------------------------------
# can_open
# ---------------------------------------------------------------------------


class TestCanOpen:
    def test_feasible(self):
        m = make_manager()
        # BTC: spot_cost=100, fee=100*0.0007=0.07, margin=30 => need=130.07
        ok, reason = m.can_open("BTC", spot_cash=200.0, opens=[], perp_cash=0.0)
        assert ok is True
        assert reason is None

    def test_rejected_insufficient_spot_cash(self):
        m = make_manager()
        # BTC needs ~130.07; provide only 100
        ok, reason = m.can_open("BTC", spot_cash=100.0, opens=[], perp_cash=0.0)
        assert ok is False
        assert "spot_cash" in reason

    def test_rejected_by_budget(self):
        # Budget cap=200; BTC=100 already open (committed=100+30=130 perp_cash)
        # opening ETH would add 200+120=320, total=450 > 200
        m = make_manager(budget_cap_usd=200.0)
        ok, reason = m.can_open(
            "ETH",
            spot_cash=1000.0,
            opens=[btc_open()],
            perp_cash=30.0,
        )
        assert ok is False
        assert "budget" in reason

    def test_unknown_coin(self):
        m = make_manager()
        ok, reason = m.can_open("DOGE", spot_cash=999.0, opens=[], perp_cash=0.0)
        assert ok is False
        assert "unknown" in reason

    def test_already_open(self):
        m = make_manager()
        ok, reason = m.can_open(
            "BTC", spot_cash=999.0, opens=[btc_open()], perp_cash=0.0
        )
        assert ok is False
        assert "already open" in reason

    def test_boundary_exact_cash(self):
        # Exactly at the threshold: should pass
        m = make_manager()
        spec = m._params["BTC"]
        margin = m.compute_required_margin_for_open("BTC")
        fee = SPOT_TAKER * spec.position_size_usd
        exact = spec.position_size_usd + fee + margin
        ok, _ = m.can_open("BTC", spot_cash=exact, opens=[], perp_cash=0.0)
        assert ok is True


# ---------------------------------------------------------------------------
# select_weakest_for_close
# ---------------------------------------------------------------------------


class TestSelectWeakestForClose:
    def test_empty_opens_returns_none(self):
        m = make_manager()
        result = m.select_weakest_for_close([], {"BTC": 1.0})
        assert result is None

    def test_returns_lowest_signal(self):
        m = make_manager()
        signals = {"BTC": 0.5, "ETH": 0.1}
        result = m.select_weakest_for_close([btc_open(), eth_open()], signals)
        assert result == "ETH"

    def test_three_positions_sorted(self):
        m = make_manager(
            per_coin_params={"BTC": BTC_SPEC, "ETH": ETH_SPEC, "SOL": SOL_SPEC}
        )
        sol_pos = OpenPosition(
            coin="SOL",
            spot_units=10.0,
            short_size=10.0,
            entry_perp_price=50.0,
            required_margin=7.5,
        )
        signals = {"BTC": 0.8, "ETH": 0.3, "SOL": 0.05}
        result = m.select_weakest_for_close(
            [btc_open(), eth_open(), sol_pos], signals
        )
        assert result == "SOL"

    def test_missing_signal_raises(self):
        m = make_manager()
        with pytest.raises(KeyError):
            m.select_weakest_for_close([btc_open()], {})


# ---------------------------------------------------------------------------
# compute_top_up_amount
# ---------------------------------------------------------------------------


class TestComputeTopUpAmount:
    def test_positive_top_up(self):
        # healthy_ratio=3.0, maint=100 => target=300; perp_cash=200 => need 100
        m = make_manager()
        result = m.compute_top_up_amount(perp_cash=200.0, total_maintenance=100.0)
        assert result == pytest.approx(100.0)

    def test_already_healthy_returns_zero(self):
        # healthy_ratio=3.0, maint=100 => target=300; perp_cash=400 => clamped 0
        m = make_manager()
        result = m.compute_top_up_amount(perp_cash=400.0, total_maintenance=100.0)
        assert result == pytest.approx(0.0)

    def test_exact_healthy_returns_zero(self):
        m = make_manager()
        result = m.compute_top_up_amount(perp_cash=300.0, total_maintenance=100.0)
        assert result == pytest.approx(0.0)

    def test_zero_maintenance(self):
        m = make_manager()
        result = m.compute_top_up_amount(perp_cash=0.0, total_maintenance=0.0)
        assert result == pytest.approx(0.0)
