"""Tests for MarginManager pre-flight integration in StrategyA and TwoPhaseDynamic.

Covers:
1. margin_manager=None (default) — existing behavior unchanged.
2. can_open=True — OPEN happens AFTER successful transfer; transfer called with correct amount.
3. can_open=False — OPEN skipped; transfer NOT called; submit NOT called; counter incremented.
4. Transfer raises Exception — OPEN skipped; submit NOT called; counter incremented.

Both StrategyA and TwoPhaseDynamic are tested.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from frab.engine.margin_manager import MarginManager, PerCoinSpec
from frab.exchanges.atomic import AtomicExecutor, PairedOpenResult
from frab.exchanges.base import FillReport, FundingTick, Leg, Quote, Side
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.two_phase_dynamic import TwoPhaseDynamic, TwoPhaseDynamicParams

HOUR = timedelta(hours=1)
T0 = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

# A funding rate that produces an annualized rate well above both strategies'
# default entry thresholds (strategy_a: 0.30; two_phase_dynamic: 0.10).
# 0.0001/hr * 8760 = 0.876 annual.
OPEN_RATE = 0.0001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quote(coin: str, mark: float = 100.0) -> Quote:
    return Quote(coin=coin, ts=T0, bid=mark, ask=mark, mark=mark, spot=None)


def _funding(coin: str, ts: datetime, rate: float = OPEN_RATE) -> FundingTick:
    return FundingTick(
        coin=coin, ts=ts, rate=rate, premium=None,
        annualized_pct=rate * 8760 * 100,
    )


def _fill(coin: str, leg: Leg, side: Side, qty: float = 10.0, price: float = 100.0, fee: float = 0.0) -> FillReport:
    return FillReport(
        coin=coin, leg=leg, side=side, ts=T0,
        qty=qty, price=price, fee=fee, slippage_bps=0.0,
    )


def _paired_open_ok(coin: str = "BTC") -> PairedOpenResult:
    return PairedOpenResult(
        status="ok",
        perp_fill=_fill(coin, Leg.PERP, Side.SELL),
        spot_fill=_fill(coin, Leg.SPOT, Side.BUY),
        perp_attempts=1, spot_attempts=1, errors=(),
    )


def make_margin_manager(
    coin: str = "BTC",
    position_size_usd: float = 1000.0,
    leverage: int = 5,
    maint_ratio: float = 0.05,
    margin_buffer_x: float = 1.5,
    budget_cap_usd: float = 100_000.0,
) -> MarginManager:
    return MarginManager(
        per_coin_params={coin: PerCoinSpec(
            position_size_usd=position_size_usd,
            leverage=leverage,
            maint_ratio=maint_ratio,
        )},
        margin_buffer_x=margin_buffer_x,
        top_up_trigger=1.2,
        healthy_ratio=2.0,
        budget_cap_usd=budget_cap_usd,
    )


def make_mock_executor(mocker) -> MagicMock:
    ex = mocker.MagicMock(spec=AtomicExecutor)
    ex.open_paired = mocker.AsyncMock(return_value=_paired_open_ok())
    ex.close_paired = mocker.AsyncMock()
    ex.transfer_spot_to_perp = mocker.AsyncMock(return_value={"status": "ok"})
    ex.transfer_perp_to_spot = mocker.AsyncMock(return_value={"status": "ok"})
    return ex


# ---------------------------------------------------------------------------
# StrategyA tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strategy_a_no_margin_manager_behaves_identically(mocker):
    """margin_manager=None (default) — no transfer calls, OPEN proceeds normally."""
    ex = make_mock_executor(mocker)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        ex,
    )
    assert strat._margin_manager is None

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_not_called()
    assert strat.n_skipped_opens_capital == 0


@pytest.mark.asyncio
async def test_strategy_a_margin_manager_can_open_true_calls_transfer(mocker):
    """can_open=True — transfer called with required margin, then OPEN proceeds."""
    ex = make_mock_executor(mocker)
    mgr = make_margin_manager(leverage=5, margin_buffer_x=1.5)
    # required_margin = position_size / leverage * buffer = 1000 / 5 * 1.5 = 300
    expected_margin = 300.0

    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",), concurrency_cap=1, signal_window_hours=1,
            position_size_usdc=1000.0,
        ),
        ex,
        margin_manager=mgr,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_called_once_with(pytest.approx(expected_margin))
    assert strat.perp_cash == pytest.approx(expected_margin)
    # _cash was decremented by required_margin (transfer), then further decremented by spot buy
    # initial cash = 1 * 1000 * 2 = 2000; after transfer: 2000 - 300 = 1700
    # after spot buy (qty=10, price=100, fee=0): 1700 - 1000 = 700
    assert strat.cash == pytest.approx(700.0)
    assert strat.n_skipped_opens_capital == 0


@pytest.mark.asyncio
async def test_strategy_a_open_qty_uses_margin_manager_size(mocker):
    """When MarginManager is set and has its own position_size_usd, the OPEN
    qty is computed from MarginManager's size — not the strategy's uniform one.

    StrategyParams has position_size_usdc=1000 but MarginManager has 250 →
    spot leg qty should be 250 / mark, not 1000 / mark.
    """
    ex = make_mock_executor(mocker)
    # MarginManager configured with explicit position_size=250 (overrides strategy 1000)
    mgr = make_margin_manager(position_size_usd=250.0, leverage=5, margin_buffer_x=2.0)
    # required_margin = 250 / 5 * 2 = 100
    expected_margin = 100.0

    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",), concurrency_cap=1, signal_window_hours=1,
            position_size_usdc=1000.0,
        ),
        ex,
        margin_manager=mgr,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    # The spot OrderRequest passed to open_paired should have qty = 250/100 = 2.5
    # (not 1000/100 = 10 — the legacy strategy value)
    open_call = ex.open_paired.await_args_list[0]
    spot_req = open_call.args[1]  # open_paired(perp_req, spot_req)
    assert spot_req.qty == pytest.approx(2.5)
    ex.transfer_spot_to_perp.assert_called_once_with(pytest.approx(expected_margin))


@pytest.mark.asyncio
async def test_strategy_a_margin_manager_can_open_false_skips_open(mocker):
    """can_open=False — OPEN skipped, transfer NOT called, counter incremented."""
    ex = make_mock_executor(mocker)
    # Budget too small to allow open
    mgr = make_margin_manager(budget_cap_usd=1.0)

    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        ex,
        margin_manager=mgr,
    )
    initial_cash = strat.cash

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ()
    ex.transfer_spot_to_perp.assert_not_called()
    ex.open_paired.assert_not_called()
    assert strat.n_skipped_opens_capital == 1
    assert strat.cash == pytest.approx(initial_cash)  # no cash change


@pytest.mark.asyncio
async def test_strategy_a_transfer_raises_skips_open(mocker):
    """Transfer exception — OPEN skipped; submit NOT called; counter incremented."""
    ex = make_mock_executor(mocker)
    ex.transfer_spot_to_perp = mocker.AsyncMock(side_effect=RuntimeError("transfer failed"))
    mgr = make_margin_manager()

    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        ex,
        margin_manager=mgr,
    )
    initial_cash = strat.cash

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ()
    ex.open_paired.assert_not_called()
    assert strat.n_skipped_opens_capital == 1
    assert strat.cash == pytest.approx(initial_cash)
    assert strat.perp_cash == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_strategy_a_margin_manager_none_default_no_regression(mocker):
    """Confirm that not passing margin_manager gives same result as legacy code."""
    ex = make_mock_executor(mocker)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        ex,
        # No margin_manager kwarg at all — backward compat
    )
    assert strat._margin_manager is None
    assert strat._perp_cash == 0.0
    assert strat._n_skipped_opens_capital == 0

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    # Exact same behavior: opens, no transfer
    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_a_multi_coin_partial_skip(mocker):
    """Two coins: first passes margin check, second fails. Only first is opened."""
    perp_btc = _fill("BTC", Leg.PERP, Side.SELL)
    spot_btc = _fill("BTC", Leg.SPOT, Side.BUY)

    async def open_gen(perp_req, spot_req):
        coin = perp_req.coin
        return PairedOpenResult(
            status="ok",
            perp_fill=_fill(coin, Leg.PERP, Side.SELL),
            spot_fill=_fill(coin, Leg.SPOT, Side.BUY),
            perp_attempts=1, spot_attempts=1, errors=(),
        )

    ex = make_mock_executor(mocker)
    ex.open_paired = mocker.AsyncMock(side_effect=open_gen)

    # Only BTC in margin_manager; ETH is unknown coin → can_open returns False
    mgr = make_margin_manager(coin="BTC", budget_cap_usd=100_000.0)

    strat = StrategyA(
        StrategyAParams(
            coins=("BTC", "ETH"), concurrency_cap=2, signal_window_hours=1,
            position_size_usdc=1000.0,
        ),
        ex,
        margin_manager=mgr,
    )

    quotes = {"BTC": _quote("BTC"), "ETH": _quote("ETH")}
    await strat.on_minute_tick(T0, quotes)
    funding = {
        "BTC": _funding("BTC", T0),
        "ETH": _funding("ETH", T0),
    }
    report = await strat.on_hour_tick(T0, funding)

    # ETH skipped (unknown coin in mgr), BTC opened
    assert "BTC" in report.opened
    assert "ETH" not in report.opened
    assert strat.n_skipped_opens_capital == 1  # ETH skipped


# ---------------------------------------------------------------------------
# TwoPhaseDynamic tests
# ---------------------------------------------------------------------------

def _tpd_params(**kwargs) -> TwoPhaseDynamicParams:
    defaults = dict(
        coins=("BTC",), entry_threshold=0.10, signal_window_hours=1,
        base_min_hold_hours=24, safety_mult=5.0, cap_min_hold_hours=720,
        phase1_negative_patience=72, phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10, concurrency_cap=1,
        position_size_usdc=1000.0, fee_round_trip_annual=18.396,
    )
    defaults.update(kwargs)
    return TwoPhaseDynamicParams(**defaults)


@pytest.mark.asyncio
async def test_tpd_no_margin_manager_behaves_identically(mocker):
    """TwoPhaseDynamic: margin_manager=None — no transfer, OPEN proceeds normally."""
    ex = make_mock_executor(mocker)
    strat = TwoPhaseDynamic(_tpd_params(), ex)
    assert strat._margin_manager is None

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_not_called()
    assert strat.n_skipped_opens_capital == 0


@pytest.mark.asyncio
async def test_tpd_margin_manager_can_open_true_calls_transfer(mocker):
    """TwoPhaseDynamic: can_open=True — transfer called, OPEN proceeds."""
    ex = make_mock_executor(mocker)
    mgr = make_margin_manager(leverage=5, margin_buffer_x=1.5)
    expected_margin = 300.0  # 1000 / 5 * 1.5

    strat = TwoPhaseDynamic(_tpd_params(position_size_usdc=1000.0), ex, margin_manager=mgr)

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_called_once_with(pytest.approx(expected_margin))
    assert strat.perp_cash == pytest.approx(expected_margin)
    assert strat.n_skipped_opens_capital == 0


@pytest.mark.asyncio
async def test_tpd_margin_manager_can_open_false_skips_open(mocker):
    """TwoPhaseDynamic: can_open=False — OPEN skipped, counter incremented."""
    ex = make_mock_executor(mocker)
    mgr = make_margin_manager(budget_cap_usd=1.0)  # too small

    strat = TwoPhaseDynamic(_tpd_params(), ex, margin_manager=mgr)
    initial_cash = strat.cash

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ()
    ex.transfer_spot_to_perp.assert_not_called()
    ex.open_paired.assert_not_called()
    assert strat.n_skipped_opens_capital == 1
    assert strat.cash == pytest.approx(initial_cash)


@pytest.mark.asyncio
async def test_tpd_transfer_raises_skips_open(mocker):
    """TwoPhaseDynamic: transfer exception — OPEN skipped, counter incremented."""
    ex = make_mock_executor(mocker)
    ex.transfer_spot_to_perp = mocker.AsyncMock(side_effect=ConnectionError("network down"))
    mgr = make_margin_manager()

    strat = TwoPhaseDynamic(_tpd_params(), ex, margin_manager=mgr)

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ()
    ex.open_paired.assert_not_called()
    assert strat.n_skipped_opens_capital == 1
    assert strat.perp_cash == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_tpd_margin_manager_none_default_no_regression(mocker):
    """TwoPhaseDynamic: no margin_manager kwarg → backward compat preserved."""
    ex = make_mock_executor(mocker)
    strat = TwoPhaseDynamic(_tpd_params(), ex)
    assert strat._margin_manager is None
    assert strat._perp_cash == 0.0
    assert strat._n_skipped_opens_capital == 0

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC")})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0)})

    assert report.opened == ("BTC",)
    ex.transfer_spot_to_perp.assert_not_called()
