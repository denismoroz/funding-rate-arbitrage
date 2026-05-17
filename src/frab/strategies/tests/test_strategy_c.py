"""Tests for strategies/strategy_c.py — StrategyC two-phase exit + dynamic min_hold."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.exchanges.base import Executor, FillReport, FundingTick, Leg, OrderRequest, Quote, Side
from frab.strategies.base import EquitySnapshot, TickReport
from frab.strategies.strategy_a import AccumulatorsSnapshot
from frab.strategies.strategy_c import (
    OpenPositionSnapshot,
    StrategyC,
    StrategyCParams,
)

HOUR = timedelta(hours=1)
T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

# Annualized rate for a funding rate of 0.0001/hr = 0.0001 * 8760 = 0.876
# entry_threshold default = 0.10, so 0.876 > 0.10 → opens


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
    coin: str,
    leg: Leg,
    side: Side,
    qty: float = 10.0,
    price: float = 100.0,
    fee: float = 0.1,
    ts: datetime = T0,
    client_ref: str | None = None,
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def executor(mocker):
    return mocker.AsyncMock(spec=Executor)


def _default_params(**kwargs) -> StrategyCParams:
    defaults = dict(
        coins=("BTC",),
        entry_threshold=0.10,
        signal_window_hours=1,
        base_min_hold_hours=24,
        safety_mult=5.0,
        cap_min_hold_hours=720,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        concurrency_cap=3,
        position_size_usdc=1000.0,
        fee_round_trip_annual=18.396,
    )
    defaults.update(kwargs)
    return StrategyCParams(**defaults)


# ---------------------------------------------------------------------------
# Params validation
# ---------------------------------------------------------------------------

def test_params_zero_concurrency_raises():
    with pytest.raises(ValueError, match="concurrency_cap must be positive"):
        StrategyCParams(coins=("BTC",), concurrency_cap=0)


def test_params_negative_position_size_raises():
    with pytest.raises(ValueError, match="position_size_usdc must be positive"):
        StrategyCParams(coins=("BTC",), position_size_usdc=-1)


def test_params_zero_window_raises():
    with pytest.raises(ValueError, match="signal_window_hours must be positive"):
        StrategyCParams(coins=("BTC",), signal_window_hours=0)


def test_params_empty_coins_raises():
    with pytest.raises(ValueError, match="coins must be non-empty"):
        StrategyCParams(coins=())


def test_params_zero_safety_mult_raises():
    with pytest.raises(ValueError, match="safety_mult must be positive"):
        StrategyCParams(coins=("BTC",), safety_mult=0.0)


def test_params_zero_cap_min_hold_raises():
    with pytest.raises(ValueError, match="cap_min_hold_hours must be positive"):
        StrategyCParams(coins=("BTC",), cap_min_hold_hours=0)


# ---------------------------------------------------------------------------
# Test group 1: Entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sufficient_signal_opens_position(executor):
    """signal above entry_threshold → OPEN, position_min_hold computed."""
    strat = StrategyC(_default_params(coins=("BTC",), concurrency_cap=1), executor)

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate=0.0001 → annual=0.876 > 0.10 → should open
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert report.opened == ("BTC",)
    assert report.closed == ()
    assert "BTC" in strat.open_positions()
    # opened_min_holds should be populated
    assert len(report.opened_min_holds) == 1
    coin, min_hold = report.opened_min_holds[0]
    assert coin == "BTC"
    assert min_hold > 0
    # min_hold = min(720, max(24, 5.0 * (18.396 / 0.876))) ≈ min(720, max(24, 105.0)) = 105
    assert min_hold == strat._positions["BTC"].position_min_hold_hours


@pytest.mark.asyncio
async def test_signal_below_threshold_does_not_open(executor):
    """signal below entry_threshold → no OPEN."""
    strat = StrategyC(_default_params(coins=("BTC",), entry_threshold=0.10), executor)

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate=0.000001 → annual=0.00876 < 0.10 → NONE
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.000001)})

    assert report.opened == ()
    executor.submit.assert_not_called()


@pytest.mark.asyncio
async def test_concurrency_cap_picks_top_k(executor):
    """When more candidates than slots, top-K by signal_value are chosen."""
    coins = ("BTC", "ETH", "SOL", "AAVE")
    strat = StrategyC(_default_params(coins=coins, concurrency_cap=2, entry_threshold=0.10), executor)

    quotes = {c: _quote(c, mark=100.0) for c in coins}
    await strat.on_minute_tick(T0, quotes)

    # SOL=2.0 ann, ETH=1.0 ann, BTC=0.5 ann, AAVE=0.2 ann — all > 0.10
    # rate / 8760 mapping:
    # SOL: 2.0/8760 ≈ 0.000228, ETH: 1.0/8760 ≈ 0.000114, BTC: 0.5/8760 ≈ 0.000057, AAVE: 0.2/8760 ≈ 0.0000228
    funding = {
        "SOL": _funding("SOL", T0, 2.0 / 8760),
        "ETH": _funding("ETH", T0, 1.0 / 8760),
        "BTC": _funding("BTC", T0, 0.5 / 8760),
        "AAVE": _funding("AAVE", T0, 0.2 / 8760),
    }

    async def _fill_gen(req: OrderRequest) -> FillReport:
        return _fill(req.coin, req.leg, req.side, qty=10.0, price=100.0, fee=0.0)

    executor.submit.side_effect = _fill_gen

    report = await strat.on_hour_tick(T0, funding)

    # Top-2 should be SOL and ETH
    assert set(report.opened) == {"SOL", "ETH"}
    assert "BTC" not in strat.open_positions()
    assert "AAVE" not in strat.open_positions()


@pytest.mark.asyncio
async def test_no_open_when_quote_missing(executor):
    """Signal good but no quote for coin → position not opened."""
    strat = StrategyC(_default_params(coins=("BTC",), concurrency_cap=1), executor)

    # Don't call on_minute_tick → no quote cached
    # rate=0.001 → annual=8.76 >> threshold
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.001)})

    assert report.opened == ()
    executor.submit.assert_not_called()


# ---------------------------------------------------------------------------
# Test group 2: Dynamic min_hold lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_hold_blocks_close_before_expiry(executor):
    """Position is locked for position_min_hold_hours hours even with catastrophic rate."""
    # Use low entry rate to get a predictable min_hold
    # entry signal = 0.876 ann, fee_annual = 18.396
    # min_hold = min(720, max(24, 5.0 * (18.396 / 0.876))) ≈ 105
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            phase1_negative_patience=72,
            phase2_exit_threshold=-0.10,
        ),
        executor,
    )

    # Open at T0
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    open_report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert open_report.opened == ("BTC",)
    _, min_hold = open_report.opened_min_holds[0]
    assert min_hold > 1  # min_hold > 1 hour — lock is active

    # One hour later: catastrophic negative rate — but min_hold not met
    t1 = T0 + HOUR
    executor.submit.side_effect = []  # must not be called
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -1.0 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


@pytest.mark.asyncio
async def test_exit_logic_active_after_min_hold(executor):
    """After position_min_hold_hours hours, exit logic can fire."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=2,
            cap_min_hold_hours=2,   # forces min_hold=2
            safety_mult=1.0,
            phase1_negative_patience=0,  # immediate exit on negative
            phase2_exit_threshold=-0.10,
        ),
        executor,
    )

    # Open at T0
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    assert strat._positions["BTC"].position_min_hold_hours == 2

    # Hour 1: still locked (1 < 2)
    t1 = T0 + HOUR
    executor.submit.side_effect = []
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    r1 = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})
    assert r1.closed == ()

    # Hour 2: min_hold met (2 >= 2), consec_negative=2 > patience=0 → CLOSE_PHASE1_NEG
    t2 = T0 + 2 * HOUR
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(t2, {"BTC": _quote("BTC", mark=100.0)})
    r2 = await strat.on_hour_tick(t2, {"BTC": _funding("BTC", t2, -0.5 / 8760)})
    assert r2.closed == ("BTC",)
    assert "BTC" not in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 3: Phase 1 exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase1_consec_negative_exceeds_patience_closes(executor):
    """consec_negative_hours > patience → CLOSE_PHASE1_NEG."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,    # min_hold = 1
            safety_mult=1.0,
            phase1_negative_patience=2,  # patience = 2 hours of negative
        ),
        executor,
    )

    # Open
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    assert strat._positions["BTC"].position_min_hold_hours == 1

    # Hour 1..3: all negative — accumulate consec_negative
    for i in range(1, 4):
        ti = T0 + i * HOUR
        executor.submit.side_effect = (
            [
                _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0),
                _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0),
            ]
            if i == 3
            else []
        )
        await strat.on_minute_tick(ti, {"BTC": _quote("BTC", mark=100.0)})
        report = await strat.on_hour_tick(ti, {"BTC": _funding("BTC", ti, -0.5 / 8760)})
        if i < 3:
            assert report.closed == (), f"should not close at hour {i}"
        else:
            # At hour 3: consec_negative = 3 > patience=2 → close
            assert report.closed == ("BTC",), f"should close at hour {i}"


@pytest.mark.asyncio
async def test_phase1_breakeven_cap_exceeded_closes(executor):
    """current rate so small that hours_to_breakeven > cap → CLOSE_PHASE1_CAP."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,    # min_hold = 1 hour
            safety_mult=1.0,
            phase1_negative_patience=1000,  # effectively no patience exit
            phase1_breakeven_cap_hours=10,  # only 10 hours to break even
        ),
        executor,
    )

    # Open with fees
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=5.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=5.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    # fees_paid = 10, funding_collected = 0 initially → not in profit
    # min_hold = 1

    # Hour 1: tiny positive rate → hours_to_breakeven = fees / hourly_income >> 10
    # tiny rate: 0.000001 annual → hourly income = 1000 * 0.000001 / 8760 ≈ 0.0
    # Even at tiny positive: 10 fees / (almost 0) → huge hours_to_breakeven > 10 → CLOSE_PHASE1_CAP
    t1 = T0 + HOUR
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.000001 / 8760)})

    assert report.closed == ("BTC",)


@pytest.mark.asyncio
async def test_phase1_mildly_positive_within_patience_no_close(executor):
    """Phase 1: rate slightly positive, consec_neg within patience → NO close."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=72,
            phase1_breakeven_cap_hours=10000,  # very large → never triggers cap
        ),
        executor,
    )

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=1.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=1.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Hour 1: slightly positive rate, not yet in profit (fees=2, collected≈0)
    t1 = T0 + HOUR
    executor.submit.side_effect = []  # no close expected
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.2 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 4: Phase 2 exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_rate_below_threshold_closes(executor):
    """In phase 2 (funding > fees), rate < phase2_exit_threshold → CLOSE_PHASE2."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase2_exit_threshold=-0.10,  # annual
        ),
        executor,
    )

    # Open with tiny fees
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.01),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.01),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    total_fees = strat._positions["BTC"].fees_paid  # 0.02

    # Manually push funding_collected above fees to enter phase 2
    strat._positions["BTC"].funding_collected = total_fees + 1.0  # clearly in profit

    # Hour 1: rate strongly negative below phase2_exit_threshold
    t1 = T0 + HOUR
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    # rate = -0.5 annual → -0.5 < -0.10 → CLOSE_PHASE2
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})

    assert report.closed == ("BTC",)
    assert "BTC" not in strat.open_positions()


@pytest.mark.asyncio
async def test_phase2_rate_above_threshold_no_close(executor):
    """In phase 2, rate above phase2_exit_threshold → NO close."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase2_exit_threshold=-0.10,
        ),
        executor,
    )

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.01),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.01),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Push to phase 2
    strat._positions["BTC"].funding_collected = strat._positions["BTC"].fees_paid + 1.0

    # Hour 1: rate = 0.05 annual > -0.10 → no close
    t1 = T0 + HOUR
    executor.submit.side_effect = []
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.05 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 5: TickReport contents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opened_min_holds_populated_on_open(executor):
    """opened_min_holds contains entry for each newly opened position."""
    strat = StrategyC(_default_params(coins=("BTC", "ETH"), concurrency_cap=2), executor)

    async def _fill_gen(req: OrderRequest) -> FillReport:
        return _fill(req.coin, req.leg, req.side, qty=10.0, price=100.0, fee=0.0)

    executor.submit.side_effect = _fill_gen

    quotes = {"BTC": _quote("BTC", mark=100.0), "ETH": _quote("ETH", mark=100.0)}
    await strat.on_minute_tick(T0, quotes)
    funding = {
        "BTC": _funding("BTC", T0, 0.5 / 8760),
        "ETH": _funding("ETH", T0, 0.5 / 8760),
    }
    report = await strat.on_hour_tick(T0, funding)

    assert len(report.opened_min_holds) == 2
    opened_coins = {coin for coin, _ in report.opened_min_holds}
    assert opened_coins == {"BTC", "ETH"}
    for coin, mh in report.opened_min_holds:
        assert mh > 0


@pytest.mark.asyncio
async def test_consec_negative_updates_for_open_positions(executor):
    """consec_negative_updates contains entry for each in-position coin."""
    strat = StrategyC(_default_params(coins=("BTC",), concurrency_cap=1), executor)

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Next tick: BTC in position → should appear in consec_negative_updates
    t1 = T0 + HOUR
    executor.submit.side_effect = []
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.5 / 8760)})

    assert len(report.consec_negative_updates) >= 1
    update_coins = {coin for coin, _ in report.consec_negative_updates}
    assert "BTC" in update_coins


@pytest.mark.asyncio
async def test_consec_negative_updates_excludes_closed_coins(executor):
    """Coins closed this tick must not appear in consec_negative_updates."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=0,  # close immediately when negative
        ),
        executor,
    )

    # Open
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    # Hour 1: negative → close (patience=0, consec_neg=1 > 0)
    t1 = T0 + HOUR
    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})

    assert report.closed == ("BTC",)
    # BTC was closed → must NOT appear in consec_negative_updates
    closed_in_updates = any(coin == "BTC" for coin, _ in report.consec_negative_updates)
    assert not closed_in_updates


# ---------------------------------------------------------------------------
# Test group 6: Rehydrate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rehydrate_restores_two_phase_state(executor):
    """After rehydrate, min_hold and consec_negative are preserved."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=500,
        ),
        executor,
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
                position_min_hold_hours=500,
                consec_negative_hours=10,
            )
        ],
        accumulators=AccumulatorsSnapshot(
            cash=5000.0,
            realized_pnl_cum=0.0,
            funding_cum=0.5,
            fees_cum=0.2,
        ),
    )

    assert "BTC" in strat.open_positions()
    assert strat._positions["BTC"].position_min_hold_hours == 500
    assert strat._positions["BTC"].consec_negative_hours == 10
    assert strat.cash == 5000.0


@pytest.mark.asyncio
async def test_rehydrate_uses_restored_state_in_next_tick(executor):
    """After rehydrate with min_hold=500, position is locked on next tick."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            phase1_negative_patience=0,  # would close immediately without lock
        ),
        executor,
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
                funding_collected=0.0,
                fees_paid=0.2,
                position_min_hold_hours=500,  # locked for 500 hours
                consec_negative_hours=5,
            )
        ],
        accumulators=AccumulatorsSnapshot(
            cash=5000.0,
            realized_pnl_cum=0.0,
            funding_cum=0.0,
            fees_cum=0.2,
        ),
    )

    # Tick at T0 + 1hr: only 1 hour in position, min_hold=500 → locked, no close
    t1 = T0 + HOUR
    executor.submit.side_effect = []  # no orders expected
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    # Strongly negative rate that would trigger phase1_neg exit if not locked
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -1.0 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


def test_rehydrate_without_accumulators_keeps_default_cash(executor):
    strat = StrategyC(_default_params(coins=("BTC",), concurrency_cap=3, position_size_usdc=1000.0), executor)
    initial_cash = strat.cash

    strat.rehydrate(positions=[], accumulators=None)

    assert strat.open_positions() == []
    assert strat.cash == initial_cash


# ---------------------------------------------------------------------------
# Test group 7: Equity computation
# ---------------------------------------------------------------------------

def test_compute_equity_no_positions(executor):
    strat = StrategyC(
        _default_params(coins=("BTC",), concurrency_cap=3, position_size_usdc=1000.0),
        executor,
    )

    snap = strat.compute_equity(T0)
    # cash = 3 * 1000 * 2 = 6000
    assert snap.total_equity == pytest.approx(6000.0)
    assert snap.cash == pytest.approx(6000.0)
    assert snap.spot_value == pytest.approx(0.0)
    assert snap.perp_unrealized == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_compute_equity_with_open_position(executor):
    """cash + spot_value + perp_unrealized = total_equity (sanity check)."""
    strat = StrategyC(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            position_size_usdc=1000.0,
        ),
        executor,
    )

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    # cash after open = 2*1000 - 10*100 - 0 = 1000
    assert strat.cash == pytest.approx(1000.0, abs=1e-9)

    # Price moves to 120
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=120.0)})

    snap = strat.compute_equity(t1)
    assert snap.spot_value == pytest.approx(1200.0)           # 10 * 120
    assert snap.perp_unrealized == pytest.approx(-200.0)       # 10 * (100 - 120)
    assert snap.total_equity == pytest.approx(2000.0)          # 1000 + 1200 - 200
    assert snap.total_equity == pytest.approx(snap.cash + snap.spot_value + snap.perp_unrealized)


# ---------------------------------------------------------------------------
# Additional: warmup_from_history (mirrors StrategyA behaviour)
# ---------------------------------------------------------------------------

def test_warmup_from_history_fills_market_state(executor):
    strat = StrategyC(
        _default_params(coins=("BTC", "ETH"), signal_window_hours=3),
        executor,
    )
    btc_ticks = [_funding("BTC", T0 - 3 * HOUR + i * HOUR, 0.0001) for i in range(3)]
    applied = strat.warmup_from_history({"BTC": btc_ticks})

    assert applied == 3
    assert strat._market_state.get("BTC").is_ready


def test_warmup_from_history_skips_unknown_coin(executor):
    strat = StrategyC(_default_params(coins=("BTC",)), executor)
    applied = strat.warmup_from_history({
        "BTC": [_funding("BTC", T0, 0.0001)],
        "DOGE": [_funding("DOGE", T0, 0.0001)],
    })
    assert applied == 1


# ---------------------------------------------------------------------------
# Additional: funding accrual
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funding_accrual_updates_position(executor):
    """Funding collected is correctly tracked in the position record."""
    strat = StrategyC(
        _default_params(coins=("BTC",), concurrency_cap=1, position_size_usdc=1000.0),
        executor,
    )

    executor.submit.side_effect = [
        _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0),
        _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0),
    ]
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    cash_after_open = strat.cash

    # Next tick: positive funding accrues
    t1 = T0 + HOUR
    executor.submit.side_effect = []
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    # funding = 10 * 100 * 0.0001 = 0.1
    assert strat.funding_cum == pytest.approx(0.1, abs=1e-9)
    assert strat.cash == pytest.approx(cash_after_open + 0.1, abs=1e-9)
    assert report.funding_accrued == (("BTC", pytest.approx(0.1, abs=1e-9)),)
