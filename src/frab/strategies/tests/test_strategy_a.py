"""Tests for strategies/strategy_a.py — StrategyA behavior."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.exchanges.atomic import AtomicExecutor, PairedCloseResult, PairedOpenResult
from frab.exchanges.base import FillReport, FundingTick, Leg, OrderRequest, Quote, Side
from frab.strategies.base import EquitySnapshot, FailedOpen, TickReport
from frab.strategies.strategy_a import (
    AccumulatorsSnapshot,
    OpenPositionSnapshot,
    StrategyA,
    StrategyAParams,
)

HOUR = timedelta(hours=1)
T0 = datetime(2023, 11, 14, 22, 0, 0, tzinfo=UTC)  # base datetime


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _quote(
    coin: str,
    mark: float = 100.0,
    bid: float | None = None,
    ask: float | None = None,
    spot: float | None = None,
) -> Quote:
    return Quote(
        coin=coin,
        ts=T0,
        bid=bid if bid is not None else mark,
        ask=ask if ask is not None else mark,
        mark=mark,
        spot=spot,
    )


def _funding(coin: str, ts: datetime, rate: float) -> FundingTick:
    return FundingTick(
        coin=coin,
        ts=ts,
        rate=rate,
        premium=None,
        annualized_pct=rate * 8760 * 100,
    )


def _fill(
    coin,
    leg,
    side,
    qty=10.0,
    price=100.0,
    fee=0.1,
    ts: datetime = T0,
    client_ref=None,
) -> FillReport:
    return FillReport(
        coin=coin,
        leg=leg,
        side=side,
        ts=ts,
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=2.0,
        is_paper=True,
        client_ref=client_ref,
    )


# ---------------------------------------------------------------------------
# Executor mock helpers
# ---------------------------------------------------------------------------

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


def make_paired_open_failed(perp_fill=None, spot_fill=None, errors=("some error",)):
    return PairedOpenResult(
        status="failed", perp_fill=perp_fill, spot_fill=spot_fill,
        perp_attempts=1, spot_attempts=0, errors=errors,
    )


def make_paired_close_failed(perp_fill=None, spot_fill=None, errors=("some error",)):
    return PairedCloseResult(
        status="failed", perp_fill=perp_fill, spot_fill=spot_fill,
        perp_attempts=1, spot_attempts=0, errors=errors,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def executor(mocker):
    return make_executor(mocker)


# ---------------------------------------------------------------------------
# Params validation tests
# ---------------------------------------------------------------------------

def test_params_zero_concurrency_raises():
    with pytest.raises(ValueError, match="concurrency_cap must be positive"):
        StrategyAParams(coins=("BTC",), concurrency_cap=0)


def test_params_negative_position_size_raises():
    with pytest.raises(ValueError, match="position_size_usdc must be positive"):
        StrategyAParams(coins=("BTC",), position_size_usdc=-1)


def test_params_zero_window_raises():
    with pytest.raises(ValueError, match="signal_window_hours must be positive"):
        StrategyAParams(coins=("BTC",), signal_window_hours=0)


def test_params_empty_coins_raises():
    with pytest.raises(ValueError, match="coins must be non-empty"):
        StrategyAParams(coins=())


# ---------------------------------------------------------------------------
# Init tests
# ---------------------------------------------------------------------------

def test_initial_cash(executor):
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=3, position_size_usdc=1000.0),
        executor,
    )
    assert strat.cash == pytest.approx(6000.0)  # 3 * 1000 * 2


def test_initial_open_positions_empty(executor):
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=3, position_size_usdc=1000.0),
        executor,
    )
    assert strat.open_positions() == []
    assert strat.realized_pnl_cum == 0.0
    assert strat.funding_cum == 0.0
    assert strat.fees_cum == 0.0


# ---------------------------------------------------------------------------
# Tick behaviour tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_minute_tick_caches_quotes(executor):
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=3, signal_window_hours=1),
        executor,
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", 100.0), "ETH": _quote("ETH", 200.0)})
    assert strat._last_quotes["BTC"].mark == pytest.approx(100.0)
    assert strat._last_quotes["ETH"].mark == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_hour_tick_emits_signals_for_all_coins(mocker):
    # Use a high entry_threshold so no OPEN is triggered, just signals emitted
    executor = make_executor(mocker)
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=3, signal_window_hours=1, entry_threshold=10.0),
        executor,
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC"), "ETH": _quote("ETH")})

    funding_ticks = {
        "BTC": _funding("BTC", T0, 0.0001),
        "ETH": _funding("ETH", T0, 0.00005),
    }
    report = await strat.on_hour_tick(T0, funding_ticks)

    assert len(report.signals) == 2
    coins_in_signals = {s.coin for s in report.signals}
    assert "BTC" in coins_in_signals
    assert "ETH" in coins_in_signals
    for sig in report.signals:
        assert sig.signal_value is not None
    assert report.closed == ()


@pytest.mark.asyncio
async def test_hour_tick_opens_when_signal_above_threshold(mocker):
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(
        mocker,
        open_results=[make_paired_open_ok(perp_fill, spot_fill)],
    )

    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=3, signal_window_hours=1),
        executor,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate = 0.0001 → annual = 0.876 > 0.30 → OPEN
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert report.opened == ("BTC",)
    assert report.closed == ()
    assert len(report.fills) == 2

    # Verify open_paired was called with perp first
    assert executor.open_paired.call_count == 1
    call_perp_req, call_spot_req = executor.open_paired.call_args.args
    assert call_perp_req.leg == Leg.PERP
    assert call_perp_req.side == Side.SELL
    assert call_perp_req.qty == pytest.approx(10.0)  # 1000 / 100
    assert call_perp_req.client_ref is not None
    assert call_perp_req.client_ref.startswith("open-perp-BTC-")

    assert call_spot_req.leg == Leg.SPOT
    assert call_spot_req.side == Side.BUY
    assert call_spot_req.qty == pytest.approx(10.0)
    assert call_spot_req.client_ref is not None
    assert call_spot_req.client_ref.startswith("open-spot-BTC-")

    assert "BTC" in strat.open_positions()
    # initial cash = 3 * 1000 * 2 = 6000
    # cash = 6000 - (10*100 + 0.07) - 0.035 = 6000 - 1000.07 - 0.035 = 4999.895
    assert strat.cash == pytest.approx(4999.895, abs=1e-6)
    assert strat.fees_cum == pytest.approx(0.105, abs=1e-6)


@pytest.mark.asyncio
async def test_hour_tick_closes_when_signal_below_exit_and_min_hold_met(mocker):
    # Open first at T0 with window=1
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            min_hold_hours=120,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # Open fills
    perp_open = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_open = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_open, spot_open)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    # cash after open = 2000 - (10*100 + 0.07) - 0.035 = 999.895
    assert strat.cash == pytest.approx(999.895, abs=1e-6)
    assert "BTC" in strat.open_positions()

    # Now close at T0 + 121 hours with mark=110 and negative funding
    close_ts = T0 + 121 * HOUR

    # Update quote to mark=110
    await strat.on_minute_tick(close_ts, {"BTC": _quote("BTC", mark=110.0)})

    # Close fills (perp first, then spot)
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=110.0, fee=0.0385)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=110.0, fee=0.077)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )

    # Rate = -0.0001 → annual = -0.876 < -0.15 → CLOSE (if min_hold met)
    report = await strat.on_hour_tick(close_ts, {"BTC": _funding("BTC", close_ts, -0.0001)})

    assert report.closed == ("BTC",)
    assert "BTC" not in strat.open_positions()

    # Cash computation:
    # After open: 999.895
    # Funding accrual at close tick: 10 * 110 * (-0.0001) = -0.11 → cash = 999.895 - 0.11 = 999.785
    # After close:
    #   spot: 10*110 - 0.077 = 1099.923
    #   perp: 10*(100 - 110) - 0.0385 = -100 - 0.0385 = -100.0385
    #   cash += 1099.923 + (-100.0385) = 999.8845
    # Total: 999.785 + 999.8845 = 1999.6695
    assert strat.cash == pytest.approx(1999.6695, abs=1e-6)
    assert strat.realized_pnl_cum == pytest.approx(-100.0, abs=1e-6)
    assert strat.funding_cum == pytest.approx(-0.11, abs=1e-6)
    assert strat.fees_cum == pytest.approx(0.07 + 0.035 + 0.077 + 0.0385, abs=1e-6)


@pytest.mark.asyncio
async def test_min_hold_blocks_close(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            min_hold_hours=120,
        ),
        make_executor(mocker),
    )

    # Open
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert "BTC" in strat.open_positions()

    # Only 1 hour later — min_hold not met
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.0001)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()
    strat._executor.close_paired.assert_not_called()


@pytest.mark.asyncio
async def test_concurrency_cap_picks_top_k_by_signal(mocker):
    coins = ("BTC", "ETH", "SOL", "AAVE", "LINK")
    strat = StrategyA(
        StrategyAParams(
            coins=coins,
            concurrency_cap=2,
            signal_window_hours=1,
            position_size_usdc=1000.0,
            entry_threshold=0.30,
        ),
        make_executor(mocker),
    )

    # All quotes at mark=100
    quotes = {c: _quote(c, mark=100.0) for c in coins}
    await strat.on_minute_tick(T0, quotes)

    # Funding rates: BTC=0.00005 (0.438), ETH=0.0001 (0.876), SOL=0.0002 (1.752),
    #               AAVE=0.00004 (0.3504), LINK=0.00003 (0.2628 < 0.30)
    funding = {
        "BTC": _funding("BTC", T0, 0.00005),
        "ETH": _funding("ETH", T0, 0.0001),
        "SOL": _funding("SOL", T0, 0.0002),
        "AAVE": _funding("AAVE", T0, 0.00004),
        "LINK": _funding("LINK", T0, 0.00003),
    }

    async def _open_gen(perp_req, spot_req):
        coin = perp_req.coin
        pf = _fill(coin, Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.05)
        sf = _fill(coin, Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.05)
        return make_paired_open_ok(pf, sf)

    strat._executor.open_paired = mocker.AsyncMock(side_effect=_open_gen)

    report = await strat.on_hour_tick(T0, funding)

    # Top-2 by signal: SOL (1.752) and ETH (0.876)
    assert set(report.opened) == {"SOL", "ETH"}
    assert set(strat.open_positions()) == {"SOL", "ETH"}
    # BTC, AAVE, LINK not opened
    assert "BTC" not in strat.open_positions()
    assert "AAVE" not in strat.open_positions()
    assert "LINK" not in strat.open_positions()


@pytest.mark.asyncio
async def test_funding_accrual_for_open_position(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            min_hold_hours=120,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # Open BTC at T0 with zero fees for simplicity
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    cash_after_open = strat.cash
    assert "BTC" in strat.open_positions()

    # Next hour tick: still positive funding, position still open (min_hold not met)
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    # Funding accrual = 10 * 100 * 0.0001 = 0.1
    assert strat.funding_cum == pytest.approx(0.1, abs=1e-9)
    assert strat.cash == pytest.approx(cash_after_open + 0.1, abs=1e-9)
    assert strat._positions["BTC"].funding_collected == pytest.approx(0.1, abs=1e-9)

    # TickReport carries the per-coin funding delta so recorder can persist it
    assert report.funding_accrued == (("BTC", pytest.approx(0.1, abs=1e-9)),)


@pytest.mark.asyncio
async def test_rehydrate_restores_positions_and_accrues_next_tick(mocker):
    """After rehydrate, an existing OPEN position receives funding on next hour-tick."""
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        make_executor(mocker),
    )

    strat.rehydrate(
        positions=[
            OpenPositionSnapshot(
                coin="BTC",
                opened_at=T0,
                spot_qty=10.0,
                perp_qty=10.0,
                entry_spot_price=100.0,
                entry_perp_price=100.0,
                funding_collected=0.5,
                fees_paid=0.2,
            )
        ],
        accumulators=AccumulatorsSnapshot(
            cash=1234.0,
            realized_pnl_cum=0.0,
            funding_cum=0.5,
            fees_cum=0.2,
        ),
    )

    assert "BTC" in strat.open_positions()
    assert strat.cash == 1234.0
    assert strat.funding_cum == 0.5

    # Next hour-tick: funding should accrue on the rehydrated position
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    # Δfunding = 10 * 100 * 0.0001 = 0.1
    assert report.funding_accrued == (("BTC", pytest.approx(0.1, abs=1e-9)),)
    assert strat.funding_cum == pytest.approx(0.6, abs=1e-9)
    assert strat.cash == pytest.approx(1234.1, abs=1e-9)


def test_rehydrate_without_accumulators_keeps_defaults(mocker):
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=3, position_size_usdc=1000.0),
        make_executor(mocker),
    )
    initial_cash = strat.cash

    strat.rehydrate(positions=[], accumulators=None)

    assert strat.open_positions() == []
    assert strat.cash == initial_cash  # untouched


@pytest.mark.asyncio
async def test_funding_accrued_empty_when_no_open_positions(mocker):
    """No open positions → funding_accrued is empty even with funding ticks."""
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            entry_threshold=10.0,  # too high — won't open
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert report.funding_accrued == ()


@pytest.mark.asyncio
async def test_close_skipped_when_no_position(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # Very negative rate — would trigger CLOSE if in position
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, -0.001)})

    assert report.closed == ()
    assert report.opened == ()
    strat._executor.open_paired.assert_not_called()
    strat._executor.close_paired.assert_not_called()


@pytest.mark.asyncio
async def test_signal_below_threshold_does_not_open(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            entry_threshold=0.30,
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate = 0.00001 → annual = 0.0876 < 0.30
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.00001)})

    assert report.opened == ()
    strat._executor.open_paired.assert_not_called()


@pytest.mark.asyncio
async def test_signal_window_not_filled_no_open(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=3,
            entry_threshold=0.30,
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # Only 1 sample, window=3 — smoothed=None → NONE
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert report.opened == ()
    assert len(report.signals) == 1
    assert report.signals[0].signal_value is None
    assert report.signals[0].action == "NONE"
    strat._executor.open_paired.assert_not_called()


@pytest.mark.asyncio
async def test_signal_window_fills_after_three_ticks(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=3,
            entry_threshold=0.30,
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})

    # Tick 1: 1 of 3 samples
    report1 = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.00005)})
    assert report1.opened == ()
    assert report1.signals[0].signal_value is None

    # Tick 2: 2 of 3 samples
    t1 = T0 + HOUR
    report2 = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})
    assert report2.opened == ()
    assert report2.signals[0].signal_value is None

    # Tick 3: 3 of 3 — window filled
    # mean rate = (0.00005 + 0.0001 + 0.0001) / 3 ≈ 0.0000833 → annualized ≈ 0.73 > 0.30 → OPEN
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    t2 = T0 + 2 * HOUR
    report3 = await strat.on_hour_tick(
        t2,
        {"BTC": _funding("BTC", t2, 0.0001)},
    )
    assert report3.opened == ("BTC",)


@pytest.mark.asyncio
async def test_compute_equity_no_positions(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=3,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    snap = strat.compute_equity(T0)
    # cash = 3 * 1000 * 2 = 6000
    assert snap.total_equity == pytest.approx(6000.0)
    assert snap.cash == pytest.approx(6000.0)
    assert snap.spot_value == pytest.approx(0.0)
    assert snap.perp_unrealized == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_compute_equity_with_open_position(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # Open with zero fees for clean math
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    # cash after open: 2000 - 10*100 - 0 - 0 = 1000
    assert strat.cash == pytest.approx(1000.0, abs=1e-9)

    # Price moves to 110
    T1 = T0 + HOUR
    await strat.on_minute_tick(T1, {"BTC": _quote("BTC", mark=110.0)})

    snap = strat.compute_equity(T1)
    assert snap.spot_value == pytest.approx(1100.0)           # 10 * 110
    assert snap.perp_unrealized == pytest.approx(-100.0)       # 10 * (100 - 110)
    assert snap.total_equity == pytest.approx(2000.0)          # 1000 + 1100 - 100


@pytest.mark.asyncio
async def test_tick_report_signals_action_strings(mocker):
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC", "ETH"),
            concurrency_cap=1,
            signal_window_hours=1,
        ),
        make_executor(mocker),
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC"), "ETH": _quote("ETH")})
    # Low rate — NONE expected
    report = await strat.on_hour_tick(
        T0,
        {
            "BTC": _funding("BTC", T0, 0.000001),
            "ETH": _funding("ETH", T0, 0.000001),
        },
    )

    for sig in report.signals:
        assert isinstance(sig.action, str)
        assert sig.action in {"NONE", "OPEN", "CLOSE"}


# ---------------------------------------------------------------------------
# warmup_from_history
# ---------------------------------------------------------------------------

def test_warmup_from_history_fills_market_state(mocker):
    strat = StrategyA(
        StrategyAParams(coins=("BTC", "ETH"), concurrency_cap=1, signal_window_hours=3),
        make_executor(mocker),
    )
    btc_ticks = [_funding("BTC", T0 - 3 * HOUR + i * HOUR, 0.0001) for i in range(3)]
    eth_ticks = [_funding("ETH", T0 - 2 * HOUR + i * HOUR, 0.00005) for i in range(2)]

    applied = strat.warmup_from_history({"BTC": btc_ticks, "ETH": eth_ticks})

    assert applied == 5
    assert strat._market_state.get("BTC").is_ready  # 3 samples, window=3
    assert not strat._market_state.get("ETH").is_ready  # only 2 samples
    assert strat._market_state.get("BTC").smoothed_signal() == pytest.approx(0.0001 * 8760)


def test_warmup_from_history_skips_unknown_coin(mocker):
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        make_executor(mocker),
    )
    applied = strat.warmup_from_history({
        "BTC": [_funding("BTC", T0, 0.0001)],
        "DOGE": [_funding("DOGE", T0, 0.0001)],  # not in universe
    })
    assert applied == 1


def test_warmup_from_history_skips_duplicates_silently(mocker):
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        make_executor(mocker),
    )
    tick = _funding("BTC", T0, 0.0001)
    strat.warmup_from_history({"BTC": [tick]})
    # Re-applying the same tick should be a no-op, not raise
    applied = strat.warmup_from_history({"BTC": [tick]})
    assert applied == 0
    assert strat._market_state.get("BTC").samples == 1


def test_warmup_from_history_empty_input(mocker):
    strat = StrategyA(
        StrategyAParams(coins=("BTC",), concurrency_cap=1, signal_window_hours=1),
        make_executor(mocker),
    )
    assert strat.warmup_from_history({}) == 0
    assert strat.warmup_from_history({"BTC": []}) == 0


# ---------------------------------------------------------------------------
# New tests: failure semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_paired_failure_records_failed_open_no_position_change(mocker):
    """open_paired returns status=failed → FailedOpen in report, position unchanged."""
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )
    cash_before = strat.cash

    # Perp partially filled, spot failed
    perp_partial = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    failed_result = make_paired_open_failed(
        perp_fill=perp_partial,
        spot_fill=None,
        errors=("ConnectionError: timed out",),
    )
    strat._executor.open_paired = mocker.AsyncMock(return_value=failed_result)

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    # failed_opens should have one entry
    assert len(report.failed_opens) == 1
    fo = report.failed_opens[0]
    assert fo.coin == "BTC"
    assert fo.ts == T0
    assert fo.perp_fill == perp_partial
    assert fo.spot_fill is None
    assert "ConnectionError" in fo.error

    # BTC must NOT be in opened
    assert "BTC" not in report.opened

    # In-memory state must be unchanged
    assert "BTC" not in strat._positions
    assert strat.cash == pytest.approx(cash_before)
    assert strat.fees_cum == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_close_paired_failure_keeps_position_open(mocker):
    """close_paired returns status=failed → position remains, accumulators unchanged."""
    strat = StrategyA(
        StrategyAParams(
            coins=("BTC",),
            concurrency_cap=1,
            signal_window_hours=1,
            min_hold_hours=120,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # First open the position successfully
    perp_open = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_open = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_open, spot_open)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert "BTC" in strat._positions
    cash_before_close = strat.cash
    fees_before_close = strat.fees_cum
    realized_before_close = strat.realized_pnl_cum
    pos_before = strat._positions["BTC"]

    # Now at T0 + 121h, try to close but it fails
    close_ts = T0 + 121 * HOUR
    await strat.on_minute_tick(close_ts, {"BTC": _quote("BTC", mark=110.0)})

    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_failed(errors=("TimeoutError",))
    )

    # Rate = -0.0001 → CLOSE decision
    report = await strat.on_hour_tick(close_ts, {"BTC": _funding("BTC", close_ts, -0.0001)})

    # BTC must NOT be in closed
    assert "BTC" not in report.closed

    # Position must still exist
    assert "BTC" in strat._positions

    # Cash and accumulators must not have changed (excluding funding accrual which is expected)
    # Note: funding accrual at close tick = 10 * 110 * (-0.0001) = -0.11
    assert strat.fees_cum == pytest.approx(fees_before_close)
    assert strat.realized_pnl_cum == pytest.approx(realized_before_close)

    # Position state must be intact
    assert strat._positions["BTC"].spot_qty == pos_before.spot_qty
    assert strat._positions["BTC"].perp_qty == pos_before.perp_qty
    assert strat._positions["BTC"].entry_spot_price == pos_before.entry_spot_price
    assert strat._positions["BTC"].entry_perp_price == pos_before.entry_perp_price
