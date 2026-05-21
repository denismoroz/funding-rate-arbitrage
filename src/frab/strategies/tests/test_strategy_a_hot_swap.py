"""Tests for StrategyA.update_hot_params (hot-swap)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.exchanges.atomic import AtomicExecutor, PairedCloseResult, PairedOpenResult
from frab.exchanges.base import FillReport, FundingTick, Leg, OrderRequest, Quote, Side
from frab.strategies.strategy_a import StrategyA, StrategyAParams

HOUR = timedelta(hours=1)
T0 = datetime(2023, 11, 14, 22, 0, 0, tzinfo=UTC)


def _quote(coin: str, mark: float = 100.0) -> Quote:
    return Quote(coin=coin, ts=T0, bid=mark, ask=mark, mark=mark, spot=None)


def _funding(coin: str, ts: datetime, rate: float) -> FundingTick:
    return FundingTick(
        coin=coin,
        ts=ts,
        rate=rate,
        premium=None,
        annualized_pct=rate * 8760 * 100,
    )


def _fill(coin, leg, side, qty=10.0, price=100.0, fee=0.0) -> FillReport:
    return FillReport(
        coin=coin,
        leg=leg,
        side=side,
        ts=T0,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=2.0,
        client_ref=None,
    )


def make_executor(mocker, *, open_results=None, close_results=None):
    ex = mocker.MagicMock(spec=AtomicExecutor)
    ex.open_paired = mocker.AsyncMock(side_effect=open_results or [])
    ex.close_paired = mocker.AsyncMock(side_effect=close_results or [])
    return ex


def make_paired_open_ok(perp_fill, spot_fill):
    return PairedOpenResult(
        status="ok", perp_fill=perp_fill, spot_fill=spot_fill,
        perp_attempts=1, spot_attempts=1, errors=(),
    )


def make_paired_close_ok(perp_fill, spot_fill):
    return PairedCloseResult(
        status="ok", perp_fill=perp_fill, spot_fill=spot_fill,
        perp_attempts=1, spot_attempts=1, errors=(),
    )


@pytest.fixture
def executor(mocker):
    return make_executor(mocker)


# ---------------------------------------------------------------------------
# test_update_hot_params_replaces_thresholds
# ---------------------------------------------------------------------------


def test_update_hot_params_replaces_thresholds(executor):
    """Hot-swap replaces all 5 hot fields and preserves cold fields."""
    coins = ("BTC", "ETH", "SOL")
    strat = StrategyA(
        StrategyAParams(
            coins=coins,
            entry_threshold=0.30,
            exit_threshold=-0.15,
            min_hold_hours=120,
            signal_window_hours=12,
            concurrency_cap=3,
            position_size_usdc=1000.0,
        ),
        executor,
    )

    strat.update_hot_params(
        entry_threshold=0.05,
        exit_threshold=-0.30,
        min_hold_hours=24,
        concurrency_cap=5,
        position_size_usdc=500.0,
    )

    # Hot fields updated
    assert strat._params.entry_threshold == pytest.approx(0.05)
    assert strat._params.exit_threshold == pytest.approx(-0.30)
    assert strat._params.min_hold_hours == 24
    assert strat._params.concurrency_cap == 5
    assert strat._params.position_size_usdc == pytest.approx(500.0)

    # Cold fields preserved
    assert strat._params.coins == coins
    assert strat._params.signal_window_hours == 12


# ---------------------------------------------------------------------------
# test_update_hot_params_triggers_close_on_open_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_hot_params_triggers_close_on_open_position(mocker):
    """After hot-swap raises exit_threshold, next tick closes the open position."""
    # Use a single coin, window=1, entry=0.20 to ensure OPEN fires with rate ~0.876
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            entry_threshold=0.20,
            exit_threshold=-0.05,
            min_hold_hours=120,
            signal_window_hours=1,
            concurrency_cap=3,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # Step 1: open a position with positive funding (annual ~0.876 > 0.20)
    perp_open = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_open = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_open, spot_open)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    report_open = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})
    assert "BTC" in report_open.opened
    assert "BTC" in strat.open_positions()

    # Step 2: hot-swap: raise exit_threshold to 0.30 and drop min_hold to 0
    # Now current_annual_rate ~0.876 is NOT < 0.30 yet for exit check
    # We need current_annual_rate < exit_threshold, so set exit_threshold > 0.876 to guarantee close
    strat.update_hot_params(
        entry_threshold=0.50,
        exit_threshold=0.30,     # current rate 0.876 < 0.30? No. Let's pick a very high exit_threshold
        min_hold_hours=0,
        concurrency_cap=3,
        position_size_usdc=1000.0,
    )
    # Actually: exit check is `current_annual_rate < exit_threshold`.
    # current_annual_rate from rate=0.0001 → 0.0001 * 8760 = 0.876
    # We need exit_threshold > 0.876. So set exit_threshold = 1.0
    strat.update_hot_params(
        entry_threshold=2.0,
        exit_threshold=1.0,   # 0.876 < 1.0 → CLOSE
        min_hold_hours=0,     # no hold requirement
        concurrency_cap=3,
        position_size_usdc=1000.0,
    )

    # Step 3: next tick with same rate — should trigger CLOSE
    t1 = T0 + HOUR
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report_close = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    assert "BTC" in report_close.closed
    assert "BTC" not in strat.open_positions()
