"""Tests for strategies/two_phase_dynamic.py — TwoPhaseDynamic two-phase exit + dynamic min_hold."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.exchanges.atomic import AtomicExecutor, PairedCloseResult, PairedOpenResult
from frab.exchanges.base import FillReport, FundingTick, Leg, OrderRequest, Quote, Side
from frab.strategies.base import EquitySnapshot, FailedOpen, TickReport
from frab.strategies.strategy_a import AccumulatorsSnapshot
from frab.strategies.two_phase_dynamic import (
    OpenPositionSnapshot,
    TwoPhaseDynamic,
    TwoPhaseDynamicParams,
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


def _default_params(**kwargs) -> TwoPhaseDynamicParams:
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
    return TwoPhaseDynamicParams(**defaults)


# ---------------------------------------------------------------------------
# Params validation
# ---------------------------------------------------------------------------

def test_params_zero_concurrency_raises():
    with pytest.raises(ValueError, match="concurrency_cap must be positive"):
        TwoPhaseDynamicParams(coins=("BTC",), concurrency_cap=0)


def test_params_negative_position_size_raises():
    with pytest.raises(ValueError, match="position_size_usdc must be positive"):
        TwoPhaseDynamicParams(coins=("BTC",), position_size_usdc=-1)


def test_params_zero_window_raises():
    with pytest.raises(ValueError, match="signal_window_hours must be positive"):
        TwoPhaseDynamicParams(coins=("BTC",), signal_window_hours=0)


def test_params_empty_coins_raises():
    with pytest.raises(ValueError, match="coins must be non-empty"):
        TwoPhaseDynamicParams(coins=())


def test_params_zero_safety_mult_raises():
    with pytest.raises(ValueError, match="safety_mult must be positive"):
        TwoPhaseDynamicParams(coins=("BTC",), safety_mult=0.0)


def test_params_zero_cap_min_hold_raises():
    with pytest.raises(ValueError, match="cap_min_hold_hours must be positive"):
        TwoPhaseDynamicParams(coins=("BTC",), cap_min_hold_hours=0)


# ---------------------------------------------------------------------------
# Test group 1: Entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sufficient_signal_opens_position(mocker):
    """signal above entry_threshold → OPEN, position_min_hold computed."""
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), concurrency_cap=1), make_executor(mocker))

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )

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
async def test_signal_below_threshold_does_not_open(mocker):
    """signal below entry_threshold → no OPEN."""
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), entry_threshold=0.10), make_executor(mocker))

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate=0.000001 → annual=0.00876 < 0.10 → NONE
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.000001)})

    assert report.opened == ()
    strat._executor.open_paired.assert_not_called()


@pytest.mark.asyncio
async def test_concurrency_cap_picks_top_k(mocker):
    """When more candidates than slots, top-K by signal_value are chosen."""
    coins = ("BTC", "ETH", "SOL", "AAVE")
    strat = TwoPhaseDynamic(_default_params(coins=coins, concurrency_cap=2, entry_threshold=0.10), make_executor(mocker))

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

    async def _open_gen(perp_req, spot_req):
        coin = perp_req.coin
        pf = _fill(coin, Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
        sf = _fill(coin, Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
        return make_paired_open_ok(pf, sf)

    strat._executor.open_paired = mocker.AsyncMock(side_effect=_open_gen)

    report = await strat.on_hour_tick(T0, funding)

    # Top-2 should be SOL and ETH
    assert set(report.opened) == {"SOL", "ETH"}
    assert "BTC" not in strat.open_positions()
    assert "AAVE" not in strat.open_positions()


@pytest.mark.asyncio
async def test_no_open_when_quote_missing(mocker):
    """Signal good but no quote for coin → position not opened."""
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), concurrency_cap=1), make_executor(mocker))

    # Don't call on_minute_tick → no quote cached
    # rate=0.001 → annual=8.76 >> threshold
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.001)})

    assert report.opened == ()
    strat._executor.open_paired.assert_not_called()


# ---------------------------------------------------------------------------
# Test group 2: Dynamic min_hold lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_hold_blocks_close_before_expiry(mocker):
    """Position is locked for position_min_hold_hours hours even with catastrophic rate."""
    # Use low entry rate to get a predictable min_hold
    # entry signal = 0.876 ann, fee_annual = 18.396
    # min_hold = min(720, max(24, 5.0 * (18.396 / 0.876))) ≈ 105
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            phase1_negative_patience=72,
            phase2_exit_threshold=-0.10,
        ),
        make_executor(mocker),
    )

    # Open at T0
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    open_report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert open_report.opened == ("BTC",)
    _, min_hold = open_report.opened_min_holds[0]
    assert min_hold > 1  # min_hold > 1 hour — lock is active

    # One hour later: catastrophic negative rate — but min_hold not met
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -1.0 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()
    strat._executor.close_paired.assert_not_called()


@pytest.mark.asyncio
async def test_exit_logic_active_after_min_hold(mocker):
    """After position_min_hold_hours hours, exit logic can fire."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=2,
            cap_min_hold_hours=2,   # forces min_hold=2
            safety_mult=1.0,
            phase1_negative_patience=0,  # immediate exit on negative
            phase2_exit_threshold=-0.10,
        ),
        make_executor(mocker),
    )

    # Open at T0
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    assert strat._positions["BTC"].position_min_hold_hours == 2

    # Hour 1: still locked (1 < 2)
    t1 = T0 + HOUR
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    r1 = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})
    assert r1.closed == ()

    # Hour 2: min_hold met (2 >= 2), consec_negative=2 > patience=0 → CLOSE_PHASE1_NEG
    t2 = T0 + 2 * HOUR
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )
    await strat.on_minute_tick(t2, {"BTC": _quote("BTC", mark=100.0)})
    r2 = await strat.on_hour_tick(t2, {"BTC": _funding("BTC", t2, -0.5 / 8760)})
    assert r2.closed == ("BTC",)
    assert "BTC" not in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 3: Phase 1 exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase1_consec_negative_exceeds_patience_closes(mocker):
    """consec_negative_hours > patience → CLOSE_PHASE1_NEG."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,    # min_hold = 1
            safety_mult=1.0,
            phase1_negative_patience=2,  # patience = 2 hours of negative
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
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    assert strat._positions["BTC"].position_min_hold_hours == 1

    # Hour 1..3: all negative — accumulate consec_negative
    for i in range(1, 4):
        ti = T0 + i * HOUR
        if i == 3:
            perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
            spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
            strat._executor.close_paired = mocker.AsyncMock(
                return_value=make_paired_close_ok(perp_close, spot_close)
            )
        else:
            strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
            strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
        await strat.on_minute_tick(ti, {"BTC": _quote("BTC", mark=100.0)})
        report = await strat.on_hour_tick(ti, {"BTC": _funding("BTC", ti, -0.5 / 8760)})
        if i < 3:
            assert report.closed == (), f"should not close at hour {i}"
        else:
            # At hour 3: consec_negative = 3 > patience=2 → close
            assert report.closed == ("BTC",), f"should close at hour {i}"


@pytest.mark.asyncio
async def test_phase1_breakeven_cap_exceeded_closes(mocker):
    """current rate so small that hours_to_breakeven > cap → CLOSE_PHASE1_CAP."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,    # min_hold = 1 hour
            safety_mult=1.0,
            phase1_negative_patience=1000,  # effectively no patience exit
            phase1_breakeven_cap_hours=10,  # only 10 hours to break even
        ),
        make_executor(mocker),
    )

    # Open with fees
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=5.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=5.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    # fees_paid = 10, funding_collected = 0 initially → not in profit
    # min_hold = 1

    # Hour 1: tiny positive rate → hours_to_breakeven = fees / hourly_income >> 10
    # tiny rate: 0.000001 annual → hourly income = 1000 * 0.000001 / 8760 ≈ 0.0
    # Even at tiny positive: 10 fees / (almost 0) → huge hours_to_breakeven > 10 → CLOSE_PHASE1_CAP
    t1 = T0 + HOUR
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.000001 / 8760)})

    assert report.closed == ("BTC",)


@pytest.mark.asyncio
async def test_phase1_mildly_positive_within_patience_no_close(mocker):
    """Phase 1: rate slightly positive, consec_neg within patience → NO close."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=72,
            phase1_breakeven_cap_hours=10000,  # very large → never triggers cap
        ),
        make_executor(mocker),
    )

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=1.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=1.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Hour 1: slightly positive rate, not yet in profit (fees=2, collected≈0)
    t1 = T0 + HOUR
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.2 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 4: Phase 2 exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_rate_below_threshold_closes(mocker):
    """In phase 2 (funding > fees), rate < phase2_exit_threshold → CLOSE_PHASE2."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase2_exit_threshold=-0.10,  # annual
        ),
        make_executor(mocker),
    )

    # Open with tiny fees
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.01)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.01)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()
    total_fees = strat._positions["BTC"].fees_paid  # 0.02

    # Manually push funding_collected above fees to enter phase 2
    strat._positions["BTC"].funding_collected = total_fees + 1.0  # clearly in profit

    # Hour 1: rate strongly negative below phase2_exit_threshold
    t1 = T0 + HOUR
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    # rate = -0.5 annual → -0.5 < -0.10 → CLOSE_PHASE2
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})

    assert report.closed == ("BTC",)
    assert "BTC" not in strat.open_positions()


@pytest.mark.asyncio
async def test_phase2_rate_above_threshold_no_close(mocker):
    """In phase 2, rate above phase2_exit_threshold → NO close."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase2_exit_threshold=-0.10,
        ),
        make_executor(mocker),
    )

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.01)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.01)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Push to phase 2
    strat._positions["BTC"].funding_collected = strat._positions["BTC"].fees_paid + 1.0

    # Hour 1: rate = 0.05 annual > -0.10 → no close
    t1 = T0 + HOUR
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.05 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()


# ---------------------------------------------------------------------------
# Test group 5: TickReport contents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opened_min_holds_populated_on_open(mocker):
    """opened_min_holds contains entry for each newly opened position."""
    strat = TwoPhaseDynamic(_default_params(coins=("BTC", "ETH"), concurrency_cap=2), make_executor(mocker))

    async def _open_gen(perp_req, spot_req):
        coin = perp_req.coin
        pf = _fill(coin, Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
        sf = _fill(coin, Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
        return make_paired_open_ok(pf, sf)

    strat._executor.open_paired = mocker.AsyncMock(side_effect=_open_gen)

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
async def test_consec_negative_updates_for_open_positions(mocker):
    """consec_negative_updates contains entry for each in-position coin."""
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), concurrency_cap=1), make_executor(mocker))

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})
    assert "BTC" in strat.open_positions()

    # Next tick: BTC in position → should appear in consec_negative_updates
    t1 = T0 + HOUR
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.5 / 8760)})

    assert len(report.consec_negative_updates) >= 1
    update_coins = {coin for coin, _ in report.consec_negative_updates}
    assert "BTC" in update_coins


@pytest.mark.asyncio
async def test_consec_negative_updates_excludes_closed_coins(mocker):
    """Coins closed this tick must not appear in consec_negative_updates."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=0,  # close immediately when negative
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
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    # Hour 1: negative → close (patience=0, consec_neg=1 > 0)
    t1 = T0 + HOUR
    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )
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
async def test_rehydrate_restores_two_phase_state(mocker):
    """After rehydrate, min_hold and consec_negative are preserved."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=500,
        ),
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
async def test_rehydrate_uses_restored_state_in_next_tick(mocker):
    """After rehydrate with min_hold=500, position is locked on next tick."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            phase1_negative_patience=0,  # would close immediately without lock
        ),
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
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    # Strongly negative rate that would trigger phase1_neg exit if not locked
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -1.0 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat.open_positions()
    strat._executor.close_paired.assert_not_called()


def test_rehydrate_without_accumulators_keeps_default_cash(mocker):
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), concurrency_cap=3, position_size_usdc=1000.0), make_executor(mocker))
    initial_cash = strat.cash

    strat.rehydrate(positions=[], accumulators=None)

    assert strat.open_positions() == []
    assert strat.cash == initial_cash


# ---------------------------------------------------------------------------
# Test group 7: Equity computation
# ---------------------------------------------------------------------------

def test_compute_equity_no_positions(mocker):
    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=3, position_size_usdc=1000.0),
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
    """cash + spot_value + perp_unrealized = total_equity (sanity check)."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
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

def test_warmup_from_history_fills_market_state(mocker):
    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC", "ETH"), signal_window_hours=3),
        make_executor(mocker),
    )
    btc_ticks = [_funding("BTC", T0 - 3 * HOUR + i * HOUR, 0.0001) for i in range(3)]
    applied = strat.warmup_from_history({"BTC": btc_ticks})

    assert applied == 3
    assert strat._market_state.get("BTC").is_ready


def test_warmup_from_history_skips_unknown_coin(mocker):
    strat = TwoPhaseDynamic(_default_params(coins=("BTC",)), make_executor(mocker))
    applied = strat.warmup_from_history({
        "BTC": [_funding("BTC", T0, 0.0001)],
        "DOGE": [_funding("DOGE", T0, 0.0001)],
    })
    assert applied == 1


# ---------------------------------------------------------------------------
# Additional: funding accrual
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funding_accrual_updates_position(mocker):
    """Funding collected is correctly tracked in the position record."""
    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=1, position_size_usdc=1000.0),
        make_executor(mocker),
    )

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.0)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.0)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_fill, spot_fill)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    cash_after_open = strat.cash

    # Next tick: positive funding accrues
    t1 = T0 + HOUR
    strat._executor.open_paired = mocker.AsyncMock(side_effect=[])
    strat._executor.close_paired = mocker.AsyncMock(side_effect=[])
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    # funding = 10 * 100 * 0.0001 = 0.1
    assert strat.funding_cum == pytest.approx(0.1, abs=1e-9)
    assert strat.cash == pytest.approx(cash_after_open + 0.1, abs=1e-9)
    assert report.funding_accrued == (("BTC", pytest.approx(0.1, abs=1e-9)),)


# ---------------------------------------------------------------------------
# New tests: failure semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_skips_open_decisions(mocker):
    """dry_run=True: OPEN signal fires but no executor call and no position created."""
    ex = mocker.MagicMock(spec=AtomicExecutor)
    ex.open_paired = mocker.AsyncMock()
    ex.close_paired = mocker.AsyncMock()

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=1, entry_threshold=0.10),
        ex,
        dry_run=True,
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    # rate 0.0001 → annual 0.876 > 0.10 → OPEN decision
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert report.opened == ()
    assert report.fills == ()
    assert report.opened_min_holds == ()
    assert len(strat._positions) == 0
    ex.open_paired.assert_not_called()
    ex.close_paired.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_skips_close_decisions(mocker):
    """dry_run=True: CLOSE signal fires but no executor call and position stays open."""
    ex = mocker.MagicMock(spec=AtomicExecutor)
    ex.open_paired = mocker.AsyncMock()
    ex.close_paired = mocker.AsyncMock()

    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=0,  # would trigger CLOSE_PHASE1_NEG immediately
        ),
        ex,
        dry_run=True,
    )
    # Pre-populate a position via rehydrate (min_hold=1 so close logic is active after 1h)
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
                fees_paid=0.0,
                position_min_hold_hours=1,
                consec_negative_hours=0,
            )
        ]
    )
    assert "BTC" in strat._positions

    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    # Strongly negative rate → CLOSE_PHASE1_NEG decision
    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -1.0 / 8760)})

    assert report.closed == ()
    assert "BTC" in strat._positions  # position must remain
    ex.close_paired.assert_not_called()
    ex.open_paired.assert_not_called()


@pytest.mark.asyncio
async def test_open_paired_failure_records_failed_open_no_position_change(mocker):
    """open_paired returns status=failed → FailedOpen in report, no position, state unchanged."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
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

    # opened_min_holds must NOT have an entry for BTC
    assert not any(coin == "BTC" for coin, _ in report.opened_min_holds)

    # In-memory state must be unchanged
    assert "BTC" not in strat._positions
    assert strat.cash == pytest.approx(cash_before)
    assert strat.fees_cum == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_close_paired_failure_keeps_position_open(mocker):
    """close_paired returns status=failed → position remains, accumulators unchanged."""
    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=0,  # would normally close immediately
            position_size_usdc=1000.0,
        ),
        make_executor(mocker),
    )

    # Open successfully
    perp_open = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_open = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    strat._executor.open_paired = mocker.AsyncMock(
        return_value=make_paired_open_ok(perp_open, spot_open)
    )
    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.5 / 8760)})

    assert "BTC" in strat._positions
    cash_before_close = strat.cash
    fees_before_close = strat.fees_cum
    realized_before_close = strat.realized_pnl_cum
    pos_before = strat._positions["BTC"]

    # Next tick: negative rate → CLOSE decision, but close_paired fails
    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_failed(errors=("TimeoutError",))
    )

    report = await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, -0.5 / 8760)})

    # BTC must NOT be in closed
    assert "BTC" not in report.closed

    # Position must still exist
    assert "BTC" in strat._positions

    # Fees and realized PnL must not have changed
    assert strat.fees_cum == pytest.approx(fees_before_close)
    assert strat.realized_pnl_cum == pytest.approx(realized_before_close)

    # Position state must be intact
    assert strat._positions["BTC"].spot_qty == pos_before.spot_qty
    assert strat._positions["BTC"].perp_qty == pos_before.perp_qty
    assert strat._positions["BTC"].entry_spot_price == pos_before.entry_spot_price
    assert strat._positions["BTC"].entry_perp_price == pos_before.entry_perp_price


# ---------------------------------------------------------------------------
# F1.3c: dual-track to PortfolioService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_position_mirrors_fees_to_portfolio_service(mocker):
    """After _open_position succeeds, set_fees_cum is awaited with strategy's _fees_cum."""
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(
        mocker,
        open_results=[make_paired_open_ok(perp_fill, spot_fill)],
    )

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=1),
        executor,
        portfolio_service=ps,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    expected_fees = strat._fees_cum
    ps.set_fees_cum.assert_awaited_with(pytest.approx(expected_fees))


@pytest.mark.asyncio
async def test_hour_tick_funding_accrual_mirrors_to_portfolio_service(mocker):
    """After funding accrual loop, set_funding_cum is awaited once with post-loop value."""
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=1),
        make_executor(mocker),
        portfolio_service=ps,
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
                fees_paid=0.0,
                position_min_hold_hours=24,
                consec_negative_hours=0,
            )
        ],
        accumulators=AccumulatorsSnapshot(
            cash=1000.0,
            realized_pnl_cum=0.0,
            funding_cum=0.0,
            fees_cum=0.0,
        ),
    )

    t1 = T0 + HOUR
    await strat.on_minute_tick(t1, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(t1, {"BTC": _funding("BTC", t1, 0.0001)})

    # Funding delta = 10 * 100 * 0.0001 = 0.1
    expected_funding = strat._funding_cum
    assert expected_funding == pytest.approx(0.1, abs=1e-9)
    ps.set_funding_cum.assert_awaited_with(pytest.approx(expected_funding))
    assert ps.set_funding_cum.await_count == 1


@pytest.mark.asyncio
async def test_two_phase_without_portfolio_service_works_as_before(mocker):
    """Constructing without portfolio_service runs a full tick without errors."""
    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(
        mocker,
        open_results=[make_paired_open_ok(perp_fill, spot_fill)],
    )

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=1),
        executor,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    report = await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})
    assert report.opened == ("BTC",)


@pytest.mark.asyncio
async def test_set_portfolio_service_attaches_late(mocker):
    """Late binding via set_portfolio_service activates dual-track."""
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(mocker, open_results=[make_paired_open_ok(perp_fill, spot_fill)])

    strat = TwoPhaseDynamic(_default_params(coins=("BTC",), concurrency_cap=1), executor)
    assert strat._portfolio_service is None
    strat.set_portfolio_service(ps)
    assert strat._portfolio_service is ps

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    ps.set_fees_cum.assert_awaited()


# ---------------------------------------------------------------------------
# F1.4b: apply_open / apply_close wired to PortfolioService
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_calls_portfolio_service_apply_open(mocker):
    """After _open_position succeeds, apply_open is awaited with correct notional and margin."""
    from frab.engine.margin_manager import MarginManager, PerCoinSpec

    mgr = MarginManager(
        per_coin_params={
            "BTC": PerCoinSpec(position_size_usd=1000.0, leverage=10, maint_ratio=0.01),
        },
        margin_buffer_x=3.0,
        top_up_trigger=2.0,
        healthy_ratio=3.0,
        budget_cap_usd=10000.0,
    )
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(mocker, open_results=[make_paired_open_ok(perp_fill, spot_fill)])

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=3),
        executor,
        portfolio_service=ps,
        margin_manager=mgr,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    ps.apply_open.assert_awaited_once()
    call_arg = ps.apply_open.call_args.args[0]
    assert call_arg.coin == "BTC"
    assert call_arg.notional_usd == pytest.approx(10.0 * 100.0)
    expected_margin = mgr.compute_required_margin_for_open("BTC")
    assert call_arg.margin_reserve_usd == pytest.approx(expected_margin)
    assert call_arg.fees_paid == pytest.approx(0.035 + 0.07)


@pytest.mark.asyncio
async def test_open_position_without_margin_manager_zero_margin(mocker):
    """Without a margin_manager, apply_open is called with margin_reserve_usd=0."""
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    perp_fill = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_fill = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(mocker, open_results=[make_paired_open_ok(perp_fill, spot_fill)])

    strat = TwoPhaseDynamic(
        _default_params(coins=("BTC",), concurrency_cap=3),
        executor,
        portfolio_service=ps,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    ps.apply_open.assert_awaited_once()
    call_arg = ps.apply_open.call_args.args[0]
    assert call_arg.margin_reserve_usd == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_close_position_calls_portfolio_service_apply_close(mocker):
    """After _close_position succeeds, apply_close is awaited with released_notional = qty * exit_price."""
    ps = mocker.MagicMock()
    ps.set_fees_cum = mocker.AsyncMock()
    ps.set_funding_cum = mocker.AsyncMock()
    ps.apply_open = mocker.AsyncMock()
    ps.apply_close = mocker.AsyncMock()

    perp_open = _fill("BTC", Leg.PERP, Side.SELL, qty=10.0, price=100.0, fee=0.035)
    spot_open = _fill("BTC", Leg.SPOT, Side.BUY, qty=10.0, price=100.0, fee=0.07)
    executor = make_executor(mocker, open_results=[make_paired_open_ok(perp_open, spot_open)])

    strat = TwoPhaseDynamic(
        _default_params(
            coins=("BTC",),
            concurrency_cap=1,
            base_min_hold_hours=1,
            cap_min_hold_hours=1,
            safety_mult=1.0,
            phase1_negative_patience=0,
        ),
        executor,
        portfolio_service=ps,
    )

    await strat.on_minute_tick(T0, {"BTC": _quote("BTC", mark=100.0)})
    await strat.on_hour_tick(T0, {"BTC": _funding("BTC", T0, 0.0001)})

    assert "BTC" in strat.open_positions()

    close_ts = T0 + HOUR
    await strat.on_minute_tick(close_ts, {"BTC": _quote("BTC", mark=110.0)})

    perp_close = _fill("BTC", Leg.PERP, Side.BUY, qty=10.0, price=110.0, fee=0.0385)
    spot_close = _fill("BTC", Leg.SPOT, Side.SELL, qty=10.0, price=110.0, fee=0.077)
    strat._executor.close_paired = mocker.AsyncMock(
        return_value=make_paired_close_ok(perp_close, spot_close)
    )

    report = await strat.on_hour_tick(close_ts, {"BTC": _funding("BTC", close_ts, -0.5 / 8760)})

    assert report.closed == ("BTC",)
    ps.apply_close.assert_awaited_once()
    call_arg = ps.apply_close.call_args.args[0]
    assert call_arg.coin == "BTC"
    # released_notional = spot_qty * exit_price = 10 * 110 = 1100
    assert call_arg.released_notional_usd == pytest.approx(10.0 * 110.0)
    assert call_arg.released_margin_usd == pytest.approx(0.0)
