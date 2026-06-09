"""Unit tests for LoadAccruedFundingAction."""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.db.models import Base, Exchange as DBExchange, FundingAccrual as DBFundingAccrual, Position as DBPosition
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLActionContext
from frab.exchanges.hyperliquid.actions.load_funding import LoadAccruedFundingAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import HLFundingDelta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_OPENED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_OPENED_AT_MS = int(_OPENED_AT.timestamp() * 1000)


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


def _make_symbols(mock_client):
    return HLSymbols(client=mock_client, spot_token_map={}, spot_quote_token="USDC")


def make_action(session_factory, mock_client, *, address="0xabc"):
    ctx = HLActionContext(
        client=mock_client,
        symbols=_make_symbols(mock_client),
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=lambda: _OPENED_AT,
    )
    return LoadAccruedFundingAction(ctx)


async def _seed_db_position(sf, *, coin: str = "BTC") -> int:
    """Seed a DBPosition, return its id."""
    async with sf() as s:
        exc_id = await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid"))
        row = DBPosition(
            exchange_id=exc_id,
            coin=coin,
            instrument=Instrument.PERP.value,
            side=Side.LONG.value,
            qty=1.0,
            entry_price=50000.0,
            opened_at=_OPENED_AT_MS,
            closed_at=None,
            status=PositionStatus.OPEN.value,
            farb_position_id=None,
        )
        s.add(row)
        await s.commit()
        return row.id


def _make_position(pos_id: int | None, coin: str = "BTC") -> Position:
    return Position(
        id=pos_id,
        exchange_name="hyperliquid",
        coin=coin,
        instrument=Instrument.PERP,
        side=Side.LONG,
        qty=1.0,
        entry_price=50000.0,
        opened_at=_OPENED_AT,
        closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_no_address_raises_runtime_error(session_factory, mock_client):
    ctx = HLActionContext(
        client=mock_client,
        symbols=_make_symbols(mock_client),
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=None,
        clock_fn=lambda: _OPENED_AT,
    )
    action = LoadAccruedFundingAction(ctx)
    pos = _make_position(1)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.execute(pos)


async def test_position_without_id_raises_value_error(session_factory, mock_client):
    action = make_action(session_factory, mock_client)
    pos = _make_position(None)
    with pytest.raises(ValueError, match="DB id"):
        await action.execute(pos)


async def test_calls_user_funding_with_since_ms_from_opened_at(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)
    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    await action.execute(pos)

    expected_since_ms = int(_OPENED_AT.timestamp() * 1000)
    mock_client.user_funding.assert_called_once_with("0xabc", expected_since_ms)


async def test_filters_deltas_by_coin(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory, coin="BTC")
    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=1000, amount_usdc=0.1),
        HLFundingDelta(coin="ETH", ts_ms=2000, amount_usdc=0.2),
        HLFundingDelta(coin="ETH", ts_ms=3000, amount_usdc=0.3),
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id, coin="BTC")
    total = await action.execute(pos)

    # Only BTC delta should be inserted and summed
    assert total == pytest.approx(0.1)

    async with session_factory() as s:
        rows = (await s.execute(select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].ts_ms == 1000


async def test_inserts_new_accruals_to_db(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)
    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=1000, amount_usdc=0.10),
        HLFundingDelta(coin="BTC", ts_ms=2000, amount_usdc=0.20),
        HLFundingDelta(coin="BTC", ts_ms=3000, amount_usdc=0.30),
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    await action.execute(pos)

    async with session_factory() as s:
        rows = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()

    assert len(rows) == 3
    ts_values = {r.ts_ms for r in rows}
    assert ts_values == {1000, 2000, 3000}


async def test_idempotent_skips_existing_ts_ms(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)

    # Seed an existing accrual with ts_ms=1000
    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=1000, amount=0.5))
        await s.commit()

    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=1000, amount_usdc=0.5),  # duplicate
        HLFundingDelta(coin="BTC", ts_ms=2000, amount_usdc=0.25),  # new
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    await action.execute(pos)

    async with session_factory() as s:
        rows = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()

    assert len(rows) == 2
    ts_values = {r.ts_ms for r in rows}
    assert ts_values == {1000, 2000}


async def test_returns_cumulative_sum_from_db(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)

    # Pre-seed existing accruals summing to 1.0
    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=500, amount=0.6))
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=600, amount=0.4))
        await s.commit()

    # HL returns a new delta adding 0.25
    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=1000, amount_usdc=0.25),
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    total = await action.execute(pos)

    # Sum from DB (source of truth): 0.6 + 0.4 + 0.25 = 1.25
    assert total == pytest.approx(1.25)


async def test_returns_zero_when_no_accruals(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)
    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    total = await action.execute(pos)

    assert total == 0.0


async def test_existing_only_no_new_returns_existing_sum(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)

    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=100, amount=0.3))
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=200, amount=0.2))
        await s.commit()

    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    total = await action.execute(pos)

    assert total == pytest.approx(0.5)


async def test_negative_amount_handled(session_factory, mock_client):
    pos_id = await _seed_db_position(session_factory)
    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=1000, amount_usdc=-0.1),
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    total = await action.execute(pos)

    assert total == pytest.approx(-0.1)

    async with session_factory() as s:
        rows = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount == pytest.approx(-0.1)


async def test_uses_correct_position_id_in_query(session_factory, mock_client):
    pos_id_1 = await _seed_db_position(session_factory, coin="BTC")
    pos_id_2 = await _seed_db_position(session_factory, coin="ETH")

    # Insert accrual only for position 1
    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id_1, ts_ms=1000, amount=0.5))
        await s.commit()

    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    # Execute for position 2 (ETH) — should return 0, not contaminated by pos 1
    pos2 = _make_position(pos_id_2, coin="ETH")
    total = await action.execute(pos2)

    assert total == 0.0


# ---------------------------------------------------------------------------
# Incremental / full-sweep behaviour
# ---------------------------------------------------------------------------

async def test_incremental_uses_last_ts_when_existing_accruals(session_factory, mock_client):
    """full=False with existing accruals → since_ms is the max existing ts_ms."""
    pos_id = await _seed_db_position(session_factory)

    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=1000, amount=0.1))
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=3000, amount=0.2))
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=2000, amount=0.3))
        await s.commit()

    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    await action.execute(pos, full=False)

    # last max ts is 3000; incremental should start there
    mock_client.user_funding.assert_called_once_with("0xabc", 3000)


async def test_full_sweep_uses_opened_at_when_existing_accruals(session_factory, mock_client):
    """full=True always uses opened_at regardless of existing accruals."""
    pos_id = await _seed_db_position(session_factory)

    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=1000, amount=0.1))
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=3000, amount=0.2))
        await s.commit()

    mock_client.user_funding.return_value = []

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    await action.execute(pos, full=True)

    mock_client.user_funding.assert_called_once_with("0xabc", _OPENED_AT_MS)


async def test_no_existing_accruals_uses_opened_at_regardless_of_full(session_factory, mock_client):
    """When there are no existing accruals, both full=True and full=False use opened_at."""
    for full_flag in (False, True):
        pos_id = await _seed_db_position(session_factory)
        mock_client.user_funding.return_value = []

        action = make_action(session_factory, mock_client)
        pos = _make_position(pos_id)
        await action.execute(pos, full=full_flag)

        mock_client.user_funding.assert_called_with("0xabc", _OPENED_AT_MS)
    assert mock_client.user_funding.call_count == 2


async def test_incremental_boundary_row_deduped(session_factory, mock_client):
    """The boundary ts_ms row returned by incremental fetch is dropped by dedup."""
    pos_id = await _seed_db_position(session_factory)
    boundary_ts = 5000

    async with session_factory() as s:
        s.add(DBFundingAccrual(position_id=pos_id, ts_ms=boundary_ts, amount=0.5))
        await s.commit()

    # HL returns the boundary row again plus one new row
    mock_client.user_funding.return_value = [
        HLFundingDelta(coin="BTC", ts_ms=boundary_ts, amount_usdc=0.5),  # duplicate boundary
        HLFundingDelta(coin="BTC", ts_ms=6000, amount_usdc=0.25),        # new
    ]

    action = make_action(session_factory, mock_client)
    pos = _make_position(pos_id)
    total = await action.execute(pos, full=False)

    # since_ms should be the boundary ts
    mock_client.user_funding.assert_called_once_with("0xabc", boundary_ts)

    async with session_factory() as s:
        rows = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()

    # Only 2 rows: original boundary + new; boundary not double-inserted
    assert len(rows) == 2
    assert {r.ts_ms for r in rows} == {boundary_ts, 6000}
    assert total == pytest.approx(0.75)
