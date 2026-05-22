"""Tests for Strategy.margin_watchdog on StrategyA and TwoPhaseDynamic.

Covers:
- margin_manager=None → None
- no open positions → None
- missing quote for an open coin → None
- healthy ratio → NONE action
- ratio<trigger with cash sufficient → TOP_UP
- ratio<trigger with transfer failing → falls through to FORCED_CLOSE
- ratio<trigger with cash insufficient → FORCED_CLOSE
- ratio<1.0 → EMERGENCY
- weakest coin selected by lowest smoothed_signal
- _close_position failure → action collapses to NONE
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.engine.margin_manager import MarginManager, PerCoinSpec
from frab.exchanges.atomic import AtomicExecutor, PairedCloseResult
from frab.exchanges.base import FillReport, Leg, Quote, Side
from frab.strategies.base import WatchdogAction
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.strategy_a import _PositionRecord as _StratARecord
from frab.strategies.two_phase_dynamic import (
    TwoPhaseDynamic,
    TwoPhaseDynamicParams,
)
from frab.strategies.two_phase_dynamic import _PositionRecord as _TwoPhaseRecord

T0 = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _quote(coin: str, mark: float) -> Quote:
    return Quote(coin=coin, ts=T0, bid=mark, ask=mark, mark=mark, spot=None)


def _fill(coin: str, leg: Leg, side: Side, qty: float, price: float) -> FillReport:
    return FillReport(
        coin=coin, leg=leg, side=side, ts=T0,
        qty=qty, price=price, fee=0.0, slippage_bps=0.0,
    )


def _close_ok(coin: str, qty: float, price: float) -> PairedCloseResult:
    return PairedCloseResult(
        status="ok",
        perp_fill=_fill(coin, Leg.PERP, Side.BUY, qty=qty, price=price),
        spot_fill=_fill(coin, Leg.SPOT, Side.SELL, qty=qty, price=price),
        perp_attempts=1, spot_attempts=1, errors=(),
    )


def _close_failed() -> PairedCloseResult:
    return PairedCloseResult(
        status="failed",
        perp_fill=None, spot_fill=None,
        perp_attempts=0, spot_attempts=0, errors=("forced",),
    )


def _make_manager(
    coins: tuple[str, ...] = ("BTC",),
    leverage: int = 5,
    maint_ratio: float = 0.05,
    margin_buffer_x: float = 1.5,
    top_up_trigger: float = 1.5,
    healthy_ratio: float = 2.0,
    position_size_usd: float = 1000.0,
    budget_cap_usd: float = 100_000.0,
) -> MarginManager:
    return MarginManager(
        per_coin_params={
            c: PerCoinSpec(
                position_size_usd=position_size_usd,
                leverage=leverage,
                maint_ratio=maint_ratio,
            ) for c in coins
        },
        margin_buffer_x=margin_buffer_x,
        top_up_trigger=top_up_trigger,
        healthy_ratio=healthy_ratio,
        budget_cap_usd=budget_cap_usd,
    )


def _make_executor(mocker):
    ex = mocker.MagicMock(spec=AtomicExecutor)
    ex.open_paired = mocker.AsyncMock()
    ex.close_paired = mocker.AsyncMock()
    ex.transfer_spot_to_perp = mocker.AsyncMock(return_value={"status": "ok"})
    ex.transfer_perp_to_spot = mocker.AsyncMock(return_value={"status": "ok"})
    return ex


def _seed_strat_a_position(
    strat: StrategyA, coin: str, qty: float, entry_price: float, mark: float,
) -> None:
    strat._positions[coin] = _StratARecord(
        opened_at=T0,
        spot_qty=qty,
        perp_qty=qty,
        entry_spot_price=entry_price,
        entry_perp_price=entry_price,
    )
    strat._last_quotes[coin] = _quote(coin, mark)


def _seed_twophase_position(
    strat: TwoPhaseDynamic, coin: str, qty: float, entry_price: float, mark: float,
) -> None:
    strat._positions[coin] = _TwoPhaseRecord(
        opened_at=T0,
        spot_qty=qty,
        perp_qty=qty,
        entry_spot_price=entry_price,
        entry_perp_price=entry_price,
    )
    strat._last_quotes[coin] = _quote(coin, mark)


# ---------------------------------------------------------------------------
# StrategyA tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strategy_a_no_margin_manager_returns_none(mocker):
    ex = _make_executor(mocker)
    strat = StrategyA(StrategyAParams(coins=("BTC",), concurrency_cap=1), ex)
    assert await strat.margin_watchdog(T0) is None


@pytest.mark.asyncio
async def test_strategy_a_no_positions_returns_none(mocker):
    ex = _make_executor(mocker)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex,
        margin_manager=_make_manager(),
    )
    assert await strat.margin_watchdog(T0) is None


@pytest.mark.asyncio
async def test_strategy_a_missing_quote_returns_none(mocker):
    ex = _make_executor(mocker)
    mgr = _make_manager()
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    strat._positions["BTC"] = _StratARecord(
        opened_at=T0, spot_qty=10.0, perp_qty=10.0,
        entry_spot_price=100.0, entry_perp_price=100.0,
    )
    assert await strat.margin_watchdog(T0) is None


@pytest.mark.asyncio
async def test_strategy_a_healthy_ratio_returns_none_action(mocker):
    ex = _make_executor(mocker)
    # leverage=5, buffer=1.5, position=1000 → required_margin=300
    # maint_ratio=0.05, qty=10, price=100 → maint=50
    # perp_cash=300, unrealized=0 → ratio = 300/50 = 6.0
    # top_up_trigger=1.5 → 6.0 >= 1.5 → healthy
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 300.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.NONE
    assert report.ratio == pytest.approx(6.0)
    ex.transfer_spot_to_perp.assert_not_called()
    ex.close_paired.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_a_top_up_when_cash_sufficient(mocker):
    ex = _make_executor(mocker)
    # maint=50, healthy=2.0 → target perp_cash = 100
    # set perp_cash=60 → ratio = 60/50 = 1.2 < trigger(1.5)
    # top_up = 2.0*50 - 60 = 40
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0
    strat._cash = 1000.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.TOP_UP
    assert report.amount_transferred == pytest.approx(40.0)
    ex.transfer_spot_to_perp.assert_awaited_once_with(pytest.approx(40.0))
    assert strat.cash == pytest.approx(960.0)
    assert strat.perp_cash == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_strategy_a_forced_close_when_cash_insufficient(mocker):
    ex = _make_executor(mocker)
    ex.close_paired = mocker.AsyncMock(return_value=_close_ok("BTC", 10.0, 100.0))
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0
    strat._cash = 5.0  # below top_up amount of 40

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.FORCED_CLOSE
    assert report.coin == "BTC"
    ex.transfer_spot_to_perp.assert_not_called()
    ex.close_paired.assert_awaited_once()
    ex.transfer_perp_to_spot.assert_awaited_once_with(pytest.approx(300.0))
    assert "BTC" not in strat._positions


@pytest.mark.asyncio
async def test_strategy_a_top_up_failure_falls_through_to_forced_close(mocker):
    ex = _make_executor(mocker)
    ex.transfer_spot_to_perp = mocker.AsyncMock(side_effect=RuntimeError("nope"))
    ex.close_paired = mocker.AsyncMock(return_value=_close_ok("BTC", 10.0, 100.0))
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0
    strat._cash = 1000.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.FORCED_CLOSE
    ex.transfer_spot_to_perp.assert_awaited_once()
    ex.close_paired.assert_awaited_once()


@pytest.mark.asyncio
async def test_strategy_a_emergency_when_ratio_below_one(mocker):
    ex = _make_executor(mocker)
    ex.close_paired = mocker.AsyncMock(return_value=_close_ok("BTC", 10.0, 130.0))
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    # Entry=100, mark=130 → short unrealized = 10*(100-130) = -300
    # maint at mark=130: 10*130*0.05 = 65
    # perp_cash=300, ratio = (300-300)/65 = 0 < 1.0
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=130.0)
    strat._perp_cash = 300.0
    strat._cash = 1000.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.EMERGENCY
    assert report.coin == "BTC"
    ex.transfer_spot_to_perp.assert_not_called()
    ex.close_paired.assert_awaited_once()


@pytest.mark.asyncio
async def test_strategy_a_select_weakest_picks_lowest_signal(mocker):
    ex = _make_executor(mocker)
    ex.close_paired = mocker.AsyncMock(return_value=_close_ok("ETH", 10.0, 100.0))
    mgr = _make_manager(coins=("BTC", "ETH"), top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=2), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    _seed_strat_a_position(strat, "ETH", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0  # 60 / 100 = 0.6 ratio → emergency

    # Seed signals via mocker on market_state.signals
    mocker.patch.object(
        strat._market_state, "signals",
        return_value={"BTC": 0.5, "ETH": 0.1},
    )

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.EMERGENCY
    assert report.coin == "ETH"  # lower signal


@pytest.mark.asyncio
async def test_strategy_a_close_failure_collapses_to_none(mocker):
    ex = _make_executor(mocker)
    ex.close_paired = mocker.AsyncMock(return_value=_close_failed())
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_strat_a_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0
    strat._cash = 5.0  # forced-close path

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.NONE
    assert report.coin is None
    assert "BTC" in strat._positions  # still open


# ---------------------------------------------------------------------------
# TwoPhaseDynamic tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_phase_no_margin_manager_returns_none(mocker):
    ex = _make_executor(mocker)
    strat = TwoPhaseDynamic(
        TwoPhaseDynamicParams(coins=("BTC",), concurrency_cap=1), ex,
    )
    assert await strat.margin_watchdog(T0) is None


@pytest.mark.asyncio
async def test_two_phase_top_up_when_cash_sufficient(mocker):
    ex = _make_executor(mocker)
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = TwoPhaseDynamic(
        TwoPhaseDynamicParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_twophase_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=100.0)
    strat._perp_cash = 60.0
    strat._cash = 1000.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.TOP_UP
    ex.transfer_spot_to_perp.assert_awaited_once_with(pytest.approx(40.0))


@pytest.mark.asyncio
async def test_two_phase_emergency_when_ratio_below_one(mocker):
    ex = _make_executor(mocker)
    ex.close_paired = mocker.AsyncMock(return_value=_close_ok("BTC", 10.0, 130.0))
    mgr = _make_manager(top_up_trigger=1.5, healthy_ratio=2.0)
    strat = TwoPhaseDynamic(
        TwoPhaseDynamicParams(coins=("BTC",), concurrency_cap=1), ex, margin_manager=mgr,
    )
    _seed_twophase_position(strat, "BTC", qty=10.0, entry_price=100.0, mark=130.0)
    strat._perp_cash = 300.0
    strat._cash = 1000.0

    report = await strat.margin_watchdog(T0)
    assert report is not None
    assert report.action == WatchdogAction.EMERGENCY
    assert report.coin == "BTC"
