"""Unit tests for ClosePositionAction."""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import Base, Exchange as DBExchange, Fill as DBFill, Position as DBPosition
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLActionContext
from frab.exchanges.hyperliquid.actions.close_position import (
    ClosePositionAction,
    MIN_SPOT_RESIDUE_NOTIONAL_USDC,
    CLOSE_RETRY_SLIPPAGE_MULTIPLIER,
)
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import HLFillRecord, HLOrderResponse, HLOrderStatus, HLSpotBalance, HLSpotState, HLUserFill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2026, 5, 29, 16, 0, tzinfo=UTC)
_FIXED_MS = int(_FIXED_DT.timestamp() * 1000)
_CLOCK_FN = lambda: _FIXED_DT  # noqa: E731


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(DBExchange(name="hyperliquid", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5))
        await s.commit()
    try:
        yield sf
    finally:
        await engine.dispose()


@pytest.fixture
def mock_client(mocker):
    client = mocker.AsyncMock(spec=HLClient)
    client.spot_user_state.return_value = HLSpotState(balances=[])
    return client


@pytest.fixture
def symbols(mock_client):
    sym = HLSymbols(
        client=mock_client,
        spot_token_map={"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"},
        spot_quote_token="USDC",
    )
    sym._sz_decimals_cache = {"BTC": 5, "ETH": 4, "SOL": 2}
    return sym


def make_action(session_factory, mock_client, symbols, *, address="0xabc", slippage=0.01):
    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=_CLOCK_FN,
        slippage=slippage,
    )
    return ClosePositionAction(ctx)


def _filled_response(qty: float, price: float, oid: int | None = 42, fee_usdc: float | None = None) -> HLOrderResponse:
    return HLOrderResponse(statuses=[
        HLOrderStatus(filled=HLFillRecord(qty=qty, price=price, oid=oid, fee_usdc=fee_usdc))
    ])


def _error_response(error: str) -> HLOrderResponse:
    return HLOrderResponse(statuses=[HLOrderStatus(error=error)])


def _unrecognized_response() -> HLOrderResponse:
    return HLOrderResponse(statuses=[HLOrderStatus()])


async def _seed_open_position(
    sf, *, coin: str, instrument: Instrument, side: Side,
    qty: float, entry_price: float,
    farb_position_id: int | None = None,
) -> Position:
    from frab.db.models import Exchange as DBExchange
    async with sf() as s:
        exc_id = (await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid")))
        row = DBPosition(
            exchange_id=exc_id, coin=coin, instrument=instrument.value, side=side.value,
            qty=qty, entry_price=entry_price,
            opened_at=int(_FIXED_DT.timestamp() * 1000) - 3_600_000,
            closed_at=None, status=PositionStatus.OPEN.value,
            farb_position_id=farb_position_id,
        )
        s.add(row); await s.commit(); row_id = row.id
    async with sf() as s:
        row = await s.get(DBPosition, row_id)
        return Position(
            id=row.id, exchange_name="hyperliquid", coin=row.coin,
            instrument=Instrument(row.instrument), side=Side(row.side),
            qty=row.qty, entry_price=row.entry_price,
            opened_at=_FIXED_DT, closed_at=None,
            status=PositionStatus(row.status), farb_position_id=row.farb_position_id,
        )


# ---------------------------------------------------------------------------
# COLLATERAL branch
# ---------------------------------------------------------------------------

async def test_collateral_marks_db_row_closed_no_order_call(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="USDC", instrument=Instrument.COLLATERAL,
        side=Side.NONE, qty=500.0, entry_price=1.0,
    )
    action = make_action(session_factory, mock_client, symbols)

    result = await action.execute(pos)

    mock_client.market_open.assert_not_called()
    mock_client.market_close.assert_not_called()
    assert result.status == PositionStatus.CLOSED

    # Verify closed_at is set and no DBFill written
    async with session_factory() as s:
        row = await s.get(DBPosition, pos.id)
        assert row.closed_at == _FIXED_MS
        fills = (await s.execute(select(DBFill))).scalars().all()
        assert len(fills) == 0


async def test_collateral_missing_row_raises(session_factory, mock_client, symbols):
    # Create a Position domain object with a non-existent DB id
    fake_pos = Position(
        id=99999, exchange_name="hyperliquid", coin="USDC",
        instrument=Instrument.COLLATERAL, side=Side.NONE,
        qty=100.0, entry_price=1.0,
        opened_at=_FIXED_DT, closed_at=None,
        status=PositionStatus.OPEN, farb_position_id=None,
    )
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="not found in DB"):
        await action.execute(fake_pos)


# ---------------------------------------------------------------------------
# PERP branch
# ---------------------------------------------------------------------------

async def test_perp_calls_market_close(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.SHORT, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _filled_response(0.5, 50000.0, fee_usdc=8.75)
    action = make_action(session_factory, mock_client, symbols, slippage=0.01)

    await action.execute(pos)

    mock_client.market_close.assert_called_once_with("BTC", 0.01)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.side == Side.LONG.value  # opposite of SHORT
    assert fill.qty == pytest.approx(0.5)
    assert fill.price == pytest.approx(50000.0)


async def test_perp_uses_fill_fee_when_present(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _filled_response(0.5, 50000.0, fee_usdc=0.05)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    mock_client.user_fills_by_time.assert_not_called()
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.05)


async def test_perp_falls_back_to_real_fee_lookup(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _filled_response(0.5, 50000.0, oid=42, fee_usdc=None)
    hl_fill = HLUserFill(oid=42, side="A", sz=0.5, px=50000.0, ts_ms=_FIXED_MS,
                         fee_raw=0.07, fee_token="USDC", coin="BTC")
    mock_client.user_fills_by_time.return_value = [hl_fill]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    mock_client.user_fills_by_time.assert_called_once()
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.07)


async def test_perp_fee_lookup_returns_none_uses_perp_taker_estimate(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _filled_response(0.5, 50000.0, oid=None, fee_usdc=None)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    expected_fee = 0.5 * 50000.0 * PERP_TAKER
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(expected_fee)


async def test_perp_error_status_raises(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _error_response("liquidation_in_progress")
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="HL close error"):
        await action.execute(pos)


async def test_perp_unrecognized_status_raises(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _unrecognized_response()
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="unrecognized"):
        await action.execute(pos)


async def test_perp_marks_db_row_closed(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.PERP,
        side=Side.LONG, qty=0.5, entry_price=50000.0,
    )
    mock_client.market_close.return_value = _filled_response(0.5, 50000.0, fee_usdc=8.75)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        row = await s.get(DBPosition, pos.id)
    assert row.status == PositionStatus.CLOSED.value
    assert row.closed_at == _FIXED_MS


# ---------------------------------------------------------------------------
# SPOT branch
# ---------------------------------------------------------------------------

def _spot_state(coin: str, total: float) -> HLSpotState:
    """Helper: build an HLSpotState with a single balance entry."""
    return HLSpotState(balances=[HLSpotBalance(coin=coin, total=total, hold=0.0)])


async def test_spot_single_fill_drains_no_retry(session_factory, mock_client, symbols):
    # 1st balance=0.01 BTC at $60k → $600 ≥ $11 → order placed, fills all 0.01.
    # 2nd balance=0 → loop breaks → exactly one market_open call.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=4.2)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    mock_client.market_open.assert_called_once()
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.01)
    assert fill.price == pytest.approx(60000.0)


async def test_spot_partial_then_full_two_attempts(session_factory, mock_client, symbols):
    # 1st balance=0.01, fills 0.005; 2nd balance=0.005, fills 0.005; 3rd balance=0 → break.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600 ≥ $11
        _spot_state("UBTC", 0.005),  # 2nd: $300 ≥ $11
        _spot_state("UBTC", 0.0),    # 3rd: $0 → break
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.005, 60000.0, fee_usdc=1.5),
        _filled_response(0.005, 60050.0, fee_usdc=1.5),
    ]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    assert mock_client.market_open.call_count == 2
    expected_vwap = (0.005 * 60000.0 + 0.005 * 60050.0) / 0.01
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.01)
    assert fill.price == pytest.approx(expected_vwap)
    assert fill.fee == pytest.approx(3.0)


async def test_spot_residue_below_min_stops_retry(session_factory, mock_client, symbols):
    # 1st balance=0.001 BTC at entry_price=$60k → $60 ≥ $11 → order placed, fills 0.001.
    # 2nd balance=0.00001 at last_price=$60k → $0.6 < $11 → break without 2nd order.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.001, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.001),    # 1st iteration: $60 ≥ $11
        _spot_state("UBTC", 0.00001),  # 2nd iteration: $0.6 < $11 → stop
    ]
    mock_client.market_open.return_value = _filled_response(0.001, 60000.0, fee_usdc=0.42)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    mock_client.market_open.assert_called_once()
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.001)


async def test_spot_residue_remains_after_max_retries_logs_warning(session_factory, mock_client, symbols, caplog):
    # ETH, 3 iterations each with sufficient balance but each only fills 0.001.
    # After 3 fills the loop exits at MAX_CLOSE_RETRIES; final_balance still $≥11 → warning.
    pos = await _seed_open_position(
        session_factory, coin="ETH", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=2000.0,
    )
    # Each iteration re-queries balance; simulate balance decreasing by 0.001 each fill.
    mock_client.spot_user_state.side_effect = [
        _spot_state("UETH", 0.01),   # 1st: $20 ≥ $11
        _spot_state("UETH", 0.009),  # 2nd: $18 ≥ $11
        _spot_state("UETH", 0.008),  # 3rd: $16 ≥ $11
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.001, 2000.0, fee_usdc=0.014),
        _filled_response(0.001, 2000.0, fee_usdc=0.014),
        _filled_response(0.001, 2000.0, fee_usdc=0.014),
    ]
    action = make_action(session_factory, mock_client, symbols)

    import logging
    with caplog.at_level(logging.WARNING):
        await action.execute(pos)

    assert mock_client.market_open.call_count == 3
    # Check warning was logged with attempts=3
    warning_records = [r for r in caplog.records if "close_position left" in r.message]
    assert len(warning_records) == 1
    assert warning_records[0].args[2] == 3  # %d attempts arg

    # DBPosition still marked CLOSED
    async with session_factory() as s:
        row = await s.get(DBPosition, pos.id)
    assert row.status == PositionStatus.CLOSED.value

    # Single DBFill with sum of 3 fills
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.003)


async def test_spot_zero_filled_raises_runtime_error(session_factory, mock_client, symbols):
    # Balance is sufficient each iteration but all fills return qty=0 → total=0 → raises.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.return_value = _spot_state("UBTC", 0.01)  # $600 ≥ $11 every call
    mock_client.market_open.side_effect = [
        _filled_response(0.0, 60000.0, fee_usdc=0.0),
        _filled_response(0.0, 60000.0, fee_usdc=0.0),
        _filled_response(0.0, 60000.0, fee_usdc=0.0),
    ]
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="drained 0"):
        await action.execute(pos)


async def test_spot_doubles_slippage_each_retry(session_factory, mock_client, symbols):
    # 3 iterations; slippage starts at 0.01 and is doubled after each fill.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600
        _spot_state("UBTC", 0.007),  # 2nd: $420
        _spot_state("UBTC", 0.004),  # 3rd: $240
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.003, 60000.0, fee_usdc=1.0),
        _filled_response(0.003, 60000.0, fee_usdc=1.0),
        _filled_response(0.004, 60000.0, fee_usdc=1.0),
    ]
    action = make_action(session_factory, mock_client, symbols, slippage=0.01)

    await action.execute(pos)

    calls = mock_client.market_open.call_args_list
    # 1st call: initial slippage=0.01
    assert calls[0].args[3] == pytest.approx(0.01)
    # 2nd call: doubled once → 0.02
    assert calls[1].args[3] == pytest.approx(0.02)
    # 3rd call: doubled twice → 0.04
    assert calls[2].args[3] == pytest.approx(0.04)


async def test_spot_long_close_writes_short_fill(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=4.2)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.side == Side.SHORT.value


async def test_spot_short_close_writes_long_fill(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.SHORT, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=4.2)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.side == Side.LONG.value


async def test_spot_uses_make_spot_name_for_pair_symbol(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=4.2)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    first_arg = mock_client.market_open.call_args.args[0]
    assert first_arg == "UBTC/USDC"


async def test_spot_re_queries_balance_each_iteration_uses_actual_hl_remainder(session_factory, mock_client, symbols):
    # sz_dec=5 for BTC. 1st iter: balance=0.01, fills 0.00543.
    # 2nd iter: re-query returns 0.00457 (HL settled). round_qty floors to 0.00457 → 2nd order.
    # 3rd iter: balance=0 → break.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),     # 1st: $600 ≥ $11
        _spot_state("UBTC", 0.00457),  # 2nd: $274 ≥ $11
        _spot_state("UBTC", 0.0),      # 3rd: $0 → break
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.00543, 60000.0, fee_usdc=1.0),
        _filled_response(0.00457, 60000.0, fee_usdc=1.0),
    ]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    calls = mock_client.market_open.call_args_list
    # Second call qty should be round_qty of HL balance 0.00457 with sz_dec=5 = 0.00457
    assert calls[1].args[2] == pytest.approx(0.00457)


async def test_spot_balance_rounds_to_zero_breaks_loop(session_factory, mock_client, symbols):
    # sz_dec=4 for BTC. 1st iter: balance=0.001 at $60k → $60 ≥ $11, rounded=0.001 → order.
    # 2nd iter: balance=0.00001 → floor(0.00001, 4 dec)=0 → break before 2nd order.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.001, entry_price=60000.0,
    )
    symbols._sz_decimals_cache["BTC"] = 4
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.001),    # 1st: $60 ≥ $11
        _spot_state("UBTC", 0.00001),  # 2nd: rounds to 0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.001, 60000.0, fee_usdc=0.42)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    # Only one market_open call (second iteration breaks at rounded==0)
    mock_client.market_open.assert_called_once()


async def test_spot_error_status_raises_no_retry(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.return_value = _spot_state("UBTC", 0.01)
    mock_client.market_open.return_value = _error_response("insufficient balance")
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="HL close error"):
        await action.execute(pos)

    mock_client.market_open.assert_called_once()


async def test_spot_fill_with_explicit_fee_uses_it(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=0.02)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.02)
    mock_client.user_fills_by_time.assert_not_called()


async def test_spot_fee_fallback_to_real_lookup_per_attempt(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600 ≥ $11
        _spot_state("UBTC", 0.005),  # 2nd: $300 ≥ $11
        _spot_state("UBTC", 0.0),    # 3rd: $0 → break
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.005, 60000.0, oid=10, fee_usdc=None),
        _filled_response(0.005, 60000.0, oid=11, fee_usdc=None),
    ]
    hl_fill_1 = HLUserFill(oid=10, side="A", sz=0.005, px=60000.0, ts_ms=_FIXED_MS,
                            fee_raw=0.021, fee_token="USDC", coin="UBTC/USDC")
    hl_fill_2 = HLUserFill(oid=11, side="A", sz=0.005, px=60000.0, ts_ms=_FIXED_MS,
                            fee_raw=0.021, fee_token="USDC", coin="UBTC/USDC")
    mock_client.user_fills_by_time.side_effect = [[hl_fill_1], [hl_fill_2]]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    assert mock_client.user_fills_by_time.call_count == 2
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.042)


async def test_spot_marks_db_row_closed(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),  # 1st: $600 → order
        _spot_state("UBTC", 0.0),   # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.01, 60000.0, fee_usdc=4.2)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        row = await s.get(DBPosition, pos.id)
    assert row.status == PositionStatus.CLOSED.value
    assert row.closed_at == _FIXED_MS


async def test_spot_single_dbfill_row_with_vwap_and_summed_fee(session_factory, mock_client, symbols):
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600 ≥ $11
        _spot_state("UBTC", 0.004),  # 2nd: $240 ≥ $11
        _spot_state("UBTC", 0.0),    # 3rd: $0 → break
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.006, 60000.0, fee_usdc=1.5),
        _filled_response(0.004, 60100.0, fee_usdc=1.0),
    ]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    async with session_factory() as s:
        fills = (await s.execute(select(DBFill))).scalars().all()
    assert len(fills) == 1
    fill = fills[0]
    expected_vwap = (0.006 * 60000.0 + 0.004 * 60100.0) / 0.01
    assert fill.qty == pytest.approx(0.01)
    assert fill.price == pytest.approx(expected_vwap)
    assert fill.fee == pytest.approx(2.5)


async def test_spot_writes_slippage_bps_from_final_slippage(session_factory, mock_client, symbols):
    # 3 orders: slippage doubles after each fill.
    # Writes current_slippage = 0.01 * 2^3 = 0.08 (multiplied after every fill incl. last).
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600
        _spot_state("UBTC", 0.007),  # 2nd: $420
        _spot_state("UBTC", 0.004),  # 3rd: $240
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.003, 60000.0, fee_usdc=1.0),
        _filled_response(0.003, 60000.0, fee_usdc=1.0),
        _filled_response(0.004, 60000.0, fee_usdc=1.0),
    ]
    action = make_action(session_factory, mock_client, symbols, slippage=0.01)

    await action.execute(pos)

    # current_slippage is multiplied after every order including the last, so 0.01 * 2^3 = 0.08
    final_slippage = 0.01 * CLOSE_RETRY_SLIPPAGE_MULTIPLIER ** 3
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.slippage_bps == pytest.approx(final_slippage * 1e4)


# ---------------------------------------------------------------------------
# Dust-sweep / balance-anchoring tests
# ---------------------------------------------------------------------------

async def test_close_spot_sells_actual_wallet_balance_when_larger_than_pos_qty(
    session_factory, mock_client, symbols
):
    # balance=0.00025 BTC at $60k → $15 ≥ $11. pos.qty=0.0002 (dust extra from prior close).
    # New design: first iteration orders the actual wallet balance (0.00025), not pos.qty.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.0002, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.00025),  # 1st: $15 ≥ $11 → order
        _spot_state("UBTC", 0.0),      # 2nd: $0 → break
    ]
    mock_client.market_open.return_value = _filled_response(0.00025, 60000.0, fee_usdc=0.01)
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    # market_open uses the actual rounded balance, not pos.qty
    assert mock_client.market_open.call_args.args[2] == pytest.approx(0.00025)
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.00025)


async def test_close_spot_balance_below_min_notional_raises(session_factory, mock_client, symbols):
    # balance=0.0001 BTC at entry_price=$60k → $6 < $11. Loop breaks immediately → nothing sold → raises.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.0001, entry_price=60000.0,
    )
    mock_client.spot_user_state.return_value = _spot_state("UBTC", 0.0001)
    action = make_action(session_factory, mock_client, symbols)

    with pytest.raises(RuntimeError, match="drained 0"):
        await action.execute(pos)

    mock_client.market_open.assert_not_called()


async def test_close_spot_re_queries_balance_each_iteration(session_factory, mock_client, symbols):
    # Verify spot_user_state is called once per iteration (not just once up-front).
    # 2 fills → loop tries 3rd iteration → call_count == 3.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.spot_user_state.side_effect = [
        _spot_state("UBTC", 0.01),   # 1st: $600 → order
        _spot_state("UBTC", 0.005),  # 2nd: $300 → order
        _spot_state("UBTC", 0.0),    # 3rd: $0 → break
    ]
    mock_client.market_open.side_effect = [
        _filled_response(0.005, 60000.0, fee_usdc=1.5),
        _filled_response(0.005, 60000.0, fee_usdc=1.5),
    ]
    action = make_action(session_factory, mock_client, symbols)

    await action.execute(pos)

    assert mock_client.spot_user_state.call_count == 3


async def test_close_spot_falls_back_when_address_none(session_factory, mock_client, symbols):
    # address=None → skip balance lookup entirely, one-shot close at pos.qty.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.00016, entry_price=60000.0,
    )
    mock_client.market_open.return_value = _filled_response(0.00016, 60000.0, fee_usdc=0.01)
    action = make_action(session_factory, mock_client, symbols, address=None)

    await action.execute(pos)

    mock_client.spot_user_state.assert_not_called()
    assert mock_client.market_open.call_args.args[2] == pytest.approx(0.00016)


async def test_close_spot_address_none_zero_fill_raises(session_factory, mock_client, symbols):
    # address=None fallback: fill returns qty=0 → raises.
    pos = await _seed_open_position(
        session_factory, coin="BTC", instrument=Instrument.SPOT,
        side=Side.LONG, qty=0.01, entry_price=60000.0,
    )
    mock_client.market_open.return_value = _filled_response(0.0, 60000.0, fee_usdc=0.0)
    action = make_action(session_factory, mock_client, symbols, address=None)

    with pytest.raises(RuntimeError, match="drained 0"):
        await action.execute(pos)
