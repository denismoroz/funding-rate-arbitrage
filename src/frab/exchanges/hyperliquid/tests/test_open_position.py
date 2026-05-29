"""Unit tests for OpenPositionAction."""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import Base, Exchange as DBExchange, Fill as DBFill, Position as DBPosition
from frab.domain import Instrument, PositionStatus, Side
from frab.exchanges.hyperliquid.actions.open_position import OpenPositionAction, PartialFillError
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import HLFillRecord, HLOrderResponse, HLOrderStatus, HLUserFill
from frab.exchanges.protocol import OpenRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLOCK_DT = datetime(2026, 5, 29, 16, 0, tzinfo=UTC)
CLOCK_MS = int(CLOCK_DT.timestamp() * 1000)
CLOCK_FN = lambda: CLOCK_DT  # noqa: E731


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
    return mocker.AsyncMock(spec=HLClient)


@pytest.fixture
def symbols(mock_client):
    sym = HLSymbols(
        client=mock_client,
        spot_token_map={"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"},
        spot_quote_token="USDC",
    )
    sym._sz_decimals_cache = {"BTC": 5, "ETH": 4, "SOL": 2}
    return sym


def make_action(session_factory, mock_client, symbols, *, address="0xabc", slippage=0.01, tol=0.01):
    return OpenPositionAction(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        slippage=slippage,
        partial_fill_tolerance=tol,
        clock_fn=CLOCK_FN,
    )


def _filled_response(qty: float, price: float, oid: int | None = 42, fee_usdc: float | None = None) -> HLOrderResponse:
    return HLOrderResponse(statuses=[
        HLOrderStatus(filled=HLFillRecord(qty=qty, price=price, oid=oid, fee_usdc=fee_usdc))
    ])


def _error_response(error: str) -> HLOrderResponse:
    return HLOrderResponse(statuses=[HLOrderStatus(error=error)])


def _resting_response(oid: int) -> HLOrderResponse:
    return HLOrderResponse(statuses=[HLOrderStatus(resting_oid=oid)])


def _unrecognized_response() -> HLOrderResponse:
    return HLOrderResponse(statuses=[HLOrderStatus()])


# ---------------------------------------------------------------------------
# 1. Collateral
# ---------------------------------------------------------------------------

async def test_collateral_creates_db_row_no_order_call(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.COLLATERAL, coin="USDC", qty=500.0, side=Side.NONE, farb_position_id=42)

    pos = await action.execute(req)

    mock_client.market_open.assert_not_called()
    assert pos.entry_price == 1.0
    assert pos.status == PositionStatus.OPEN
    assert pos.qty == 500.0


# ---------------------------------------------------------------------------
# 2–3. SPOT symbol name
# ---------------------------------------------------------------------------

async def test_spot_long_calls_market_open_with_pair_name(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, fee_usdc=0.035)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    mock_client.market_open.assert_called_once_with("UBTC/USDC", True, 0.001, 0.01)


async def test_spot_short_calls_with_is_buy_false(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, fee_usdc=0.035)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.SHORT)

    await action.execute(req)

    _sym, is_buy, _qty, _slip = mock_client.market_open.call_args.args
    assert is_buy is False


# ---------------------------------------------------------------------------
# 4–6. PERP
# ---------------------------------------------------------------------------

async def test_perp_calls_market_open_with_raw_coin(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.5, 50000.0, fee_usdc=8.75)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG)

    await action.execute(req)

    mock_client.market_open.assert_called_once_with("BTC", True, 0.5, 0.01)
    mock_client.update_leverage.assert_not_called()


async def test_perp_with_leverage_sets_leverage_first(session_factory, mock_client, symbols):
    call_order: list[str] = []
    mock_client.update_leverage.side_effect = lambda *a, **kw: call_order.append("leverage") or None
    mock_client.market_open.side_effect = lambda *a, **kw: call_order.append("open") or _filled_response(0.5, 50000.0, fee_usdc=8.75)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG, leverage=5)

    await action.execute(req)

    assert call_order == ["leverage", "open"]
    mock_client.update_leverage.assert_called_once_with("BTC", 5)


async def test_perp_leverage_zero_raises_value_error(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG, leverage=0)

    with pytest.raises(ValueError, match="leverage must be > 0"):
        await action.execute(req)


# ---------------------------------------------------------------------------
# 7–8. Qty rounding
# ---------------------------------------------------------------------------

async def test_rounds_qty_floors_to_sz_decimals(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.00123, 50000.0, fee_usdc=0.04)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.0012345, side=Side.LONG)

    await action.execute(req)

    _sym, _buy, wire_qty, _slip = mock_client.market_open.call_args.args
    assert wire_qty == pytest.approx(0.00123)


async def test_qty_rounds_to_zero_raises_runtime_error(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.0000001, side=Side.LONG)

    with pytest.raises(RuntimeError, match="rounds to 0"):
        await action.execute(req)


# ---------------------------------------------------------------------------
# 9–11. Order status error branches
# ---------------------------------------------------------------------------

async def test_market_open_error_status_raises_runtime_error(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _error_response("insufficient margin")
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG)

    with pytest.raises(RuntimeError, match="HL order error"):
        await action.execute(req)


async def test_market_open_resting_raises_runtime_error(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _resting_response(oid=777)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG)

    with pytest.raises(RuntimeError, match="unexpectedly resting"):
        await action.execute(req)


async def test_market_open_unrecognized_status_raises_runtime_error(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _unrecognized_response()
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG)

    with pytest.raises(RuntimeError, match="unrecognized"):
        await action.execute(req)


# ---------------------------------------------------------------------------
# 12–15. Fee resolution
# ---------------------------------------------------------------------------

async def test_fill_with_fee_uses_fill_fee_directly(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, fee_usdc=0.05)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    mock_client.user_fills_by_time.assert_not_called()
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.05)


async def test_fill_without_fee_falls_back_to_real_fee_lookup(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, oid=42, fee_usdc=None)
    hl_fill = HLUserFill(oid=42, side="B", sz=0.001, px=50000.0, ts_ms=CLOCK_MS,
                         fee_raw=0.03, fee_token="USDC", coin="UBTC/USDC")
    mock_client.user_fills_by_time.return_value = [hl_fill]
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(0.03)


async def test_fee_lookup_returns_none_falls_back_to_taker_estimate_spot(session_factory, mock_client, symbols):
    # oid=None → fetch skipped entirely
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, oid=None, fee_usdc=None)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    mock_client.user_fills_by_time.assert_not_called()
    expected_fee = 0.001 * 50000.0 * SPOT_TAKER
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(expected_fee)


async def test_fee_lookup_returns_none_falls_back_to_taker_estimate_perp(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.5, 50000.0, oid=None, fee_usdc=None)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=0.5, side=Side.LONG)

    await action.execute(req)

    expected_fee = 0.5 * 50000.0 * PERP_TAKER
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(expected_fee)


# ---------------------------------------------------------------------------
# 16–17. Partial fill
# ---------------------------------------------------------------------------

async def test_partial_fill_below_tolerance_raises(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.5, 50000.0, fee_usdc=8.75)
    action = make_action(session_factory, mock_client, symbols, tol=0.01)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=1.0, side=Side.LONG)

    with pytest.raises(PartialFillError) as exc_info:
        await action.execute(req)

    err = exc_info.value
    assert err.requested_qty == pytest.approx(1.0)
    assert err.filled_qty == pytest.approx(0.5)
    assert err.fill_price == pytest.approx(50000.0)


async def test_partial_fill_within_tolerance_succeeds(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.995, 50000.0, fee_usdc=17.41)
    action = make_action(session_factory, mock_client, symbols, tol=0.01)
    req = OpenRequest(instrument=Instrument.PERP, coin="BTC", qty=1.0, side=Side.LONG)

    pos = await action.execute(req)

    assert pos.qty == pytest.approx(0.995)


# ---------------------------------------------------------------------------
# 18–21. DB writes
# ---------------------------------------------------------------------------

async def test_writes_db_position_with_filled_qty_and_price(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 48000.0, fee_usdc=0.034)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    async with session_factory() as s:
        row = (await s.execute(select(DBPosition))).scalar_one()
    assert row.qty == pytest.approx(0.001)
    assert row.entry_price == pytest.approx(48000.0)
    assert row.status == PositionStatus.OPEN.value


async def test_writes_db_fill_row(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 48000.0, fee_usdc=0.034)
    action = make_action(session_factory, mock_client, symbols, slippage=0.01)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.qty == pytest.approx(0.001)
    assert fill.price == pytest.approx(48000.0)
    assert fill.fee == pytest.approx(0.034)
    assert fill.slippage_bps == pytest.approx(0.01 * 1e4)
    assert fill.is_paper is False
    assert fill.ts_ms == CLOCK_MS


async def test_associates_with_farb_position_id(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 48000.0, fee_usdc=0.034)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG, farb_position_id=99)

    await action.execute(req)

    async with session_factory() as s:
        row = (await s.execute(select(DBPosition))).scalar_one()
    assert row.farb_position_id == 99


async def test_returns_domain_position_with_correct_fields(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 48000.0, fee_usdc=0.034)
    action = make_action(session_factory, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    pos = await action.execute(req)

    assert pos.coin == "BTC"
    assert pos.instrument == Instrument.SPOT
    assert pos.side == Side.LONG
    assert pos.qty == pytest.approx(0.001)
    assert pos.entry_price == pytest.approx(48000.0)
    assert pos.opened_at == CLOCK_DT
    assert pos.status == PositionStatus.OPEN
    assert pos.exchange_name == "hyperliquid"


# ---------------------------------------------------------------------------
# 22–23. Edge cases
# ---------------------------------------------------------------------------

async def test_no_address_skips_real_fee_lookup(session_factory, mock_client, symbols):
    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, oid=42, fee_usdc=None)
    action = make_action(session_factory, mock_client, symbols, address=None)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    await action.execute(req)

    mock_client.user_fills_by_time.assert_not_called()
    expected_fee = 0.001 * 50000.0 * SPOT_TAKER
    async with session_factory() as s:
        fill = (await s.execute(select(DBFill))).scalar_one()
    assert fill.fee == pytest.approx(expected_fee)


async def test_no_exchange_in_db_raises(mock_client, symbols):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    # Intentionally NOT adding any Exchange row

    mock_client.market_open.return_value = _filled_response(0.001, 50000.0, fee_usdc=0.035)
    action = make_action(sf, mock_client, symbols)
    req = OpenRequest(instrument=Instrument.SPOT, coin="BTC", qty=0.001, side=Side.LONG)

    with pytest.raises(RuntimeError, match="not found in DB"):
        await action.execute(req)

    await engine.dispose()
