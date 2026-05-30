"""Unit tests for LoadOpenPositionsAction."""
from __future__ import annotations

import logging
from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.db.models import Base, Exchange as DBExchange, Position as DBPosition
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLActionContext
from frab.exchanges.hyperliquid.actions.load_positions import LoadOpenPositionsAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import (
    HLPerpAssetPosition,
    HLPerpState,
    HLSpotBalance,
    HLSpotState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2026, 5, 29, 16, 0, tzinfo=UTC)
_FIXED_MS = int(_FIXED_DT.timestamp() * 1000)


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


def make_action(session_factory, mock_client, symbols, *, address="0xabc"):
    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=lambda: _FIXED_DT,
    )
    return LoadOpenPositionsAction(ctx)


def _empty_perp_state() -> HLPerpState:
    return HLPerpState(account_value=0.0, asset_positions=[])


def _empty_spot_state() -> HLSpotState:
    return HLSpotState(balances=[])


def _perp_state_with(*coins_szis: tuple[str, float]) -> HLPerpState:
    return HLPerpState(
        account_value=0.0,
        asset_positions=[
            HLPerpAssetPosition(coin=c, szi=szi, unrealized_pnl=0.0, cum_funding_since_open=0.0)
            for c, szi in coins_szis
        ],
    )


def _spot_state_with(*coins_totals: tuple[str, float]) -> HLSpotState:
    return HLSpotState(
        balances=[HLSpotBalance(coin=c, total=total, hold=0.0) for c, total in coins_totals]
    )


async def _seed_position(
    sf,
    *,
    coin: str,
    instrument: Instrument,
    side: Side,
    qty: float = 1.0,
    entry_price: float = 100.0,
    status: PositionStatus = PositionStatus.OPEN,
) -> int:
    """Seed a DBPosition row; return its id."""
    async with sf() as s:
        exc_id = await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid"))
        row = DBPosition(
            exchange_id=exc_id,
            coin=coin,
            instrument=instrument.value,
            side=side.value,
            qty=qty,
            entry_price=entry_price,
            opened_at=_FIXED_MS - 3_600_000,
            closed_at=None,
            status=status.value,
            farb_position_id=None,
        )
        s.add(row)
        await s.commit()
        return row.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_no_address_raises_runtime_error(session_factory, mock_client, symbols):
    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=None,
        clock_fn=lambda: _FIXED_DT,
    )
    action = LoadOpenPositionsAction(ctx)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.execute()


async def test_empty_db_returns_empty_list(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute()

    assert result == []
    mock_client.user_state.assert_called_once_with("0xabc")
    mock_client.spot_user_state.assert_called_once_with("0xabc")


async def test_returns_open_perp_position_matching_hl(session_factory, mock_client, symbols, caplog):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG)
    mock_client.user_state.return_value = _perp_state_with(("BTC", 0.5))
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    pos = result[0]
    assert pos.coin == "BTC"
    assert pos.instrument == Instrument.PERP
    assert pos.side == Side.LONG
    assert pos.status == PositionStatus.OPEN
    assert pos.exchange_name == "hyperliquid"
    assert "DB has OPEN PERP" not in caplog.text


async def test_warns_when_db_perp_not_on_hl(session_factory, mock_client, symbols, caplog):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG)
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert "DB has OPEN PERP BTC" in caplog.text


async def test_warns_when_db_spot_not_on_hl(session_factory, mock_client, symbols, caplog):
    # spot_token_map has BTC→UBTC, so HL coin would be UBTC
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG)
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()  # no UBTC

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert "DB has OPEN SPOT BTC" in caplog.text


async def test_spot_token_map_used_for_reconcile_match(session_factory, mock_client, symbols, caplog):
    # BTC maps to UBTC; HL spot has UBTC — no warning expected
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG)
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _spot_state_with(("UBTC", 0.01))

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert "DB has OPEN SPOT" not in caplog.text


async def test_excludes_closed_positions(session_factory, mock_client, symbols):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG, status=PositionStatus.OPEN)
    await _seed_position(session_factory, coin="ETH", instrument=Instrument.PERP, side=Side.LONG, status=PositionStatus.CLOSED)
    mock_client.user_state.return_value = _perp_state_with(("BTC", 0.5), ("ETH", 1.0))
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute()

    assert len(result) == 1
    assert result[0].coin == "BTC"


async def test_zero_szi_perp_filtered_out_of_hl_set(session_factory, mock_client, symbols, caplog):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG)
    # HL reports BTC with szi=0.0 — treated as not open
    mock_client.user_state.return_value = _perp_state_with(("BTC", 0.0))
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert "DB has OPEN PERP BTC" in caplog.text


async def test_zero_total_spot_filtered_out_of_hl_set(session_factory, mock_client, symbols, caplog):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG)
    # HL reports UBTC with total=0.0 — treated as not open
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _spot_state_with(("UBTC", 0.0))

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert "DB has OPEN SPOT BTC" in caplog.text


async def test_missing_exchange_row_raises(mock_client, symbols):
    # Create a session_factory with no Exchange row
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=sf,
        exchange_name="hyperliquid",
        address="0xabc",
        clock_fn=lambda: _FIXED_DT,
    )
    action = LoadOpenPositionsAction(ctx)
    with pytest.raises(RuntimeError, match="not found"):
        await action.execute()

    await engine.dispose()


async def test_collateral_position_returned_no_reconcile(session_factory, mock_client, symbols, caplog):
    await _seed_position(
        session_factory, coin="USDC", instrument=Instrument.COLLATERAL, side=Side.NONE,
        qty=500.0, entry_price=1.0,
    )
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 1
    assert result[0].instrument == Instrument.COLLATERAL
    # No warning emitted for collateral
    assert "DB has OPEN" not in caplog.text


async def test_calls_user_state_and_spot_user_state_in_parallel(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(session_factory, mock_client, symbols)
    await action.execute()

    mock_client.user_state.assert_called_once_with("0xabc")
    mock_client.spot_user_state.assert_called_once_with("0xabc")


async def test_includes_multiple_positions(session_factory, mock_client, symbols, caplog):
    await _seed_position(session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG)
    await _seed_position(session_factory, coin="ETH", instrument=Instrument.SPOT, side=Side.LONG)
    await _seed_position(session_factory, coin="USDC", instrument=Instrument.COLLATERAL, side=Side.NONE, entry_price=1.0)

    mock_client.user_state.return_value = _perp_state_with(("BTC", 0.5))
    mock_client.spot_user_state.return_value = _spot_state_with(("UETH", 1.0))

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.execute()

    assert len(result) == 3
    coins = {p.coin for p in result}
    assert coins == {"BTC", "ETH", "USDC"}
    assert "DB has OPEN" not in caplog.text
