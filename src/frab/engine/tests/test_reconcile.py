"""Tests for src/frab/engine/reconcile.py."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Base, Exchange, Market, Position, PositionStatus, Strategy
from frab.db.session import make_session_factory, session_scope
from frab.engine.reconcile import ReconcileReport, scan
from frab.events.bus import EventBus

_T0 = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Per-test DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    sa_event.listen(eng.sync_engine, "connect", _enable_fks)

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return make_session_factory(engine)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_base(session_factory, *, strategy_id_val: int = 1, coin: str = "BTC") -> tuple[int, int]:
    """Insert Exchange, Market, Strategy; return (market_id, strategy_id)."""
    async with session_scope(session_factory) as s:
        exc = Exchange(name=f"HL_{coin}_{strategy_id_val}", funding_interval_h=8,
                       spot_taker_bps=7.0, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()

        mkt = Market(exchange_id=exc.id, coin=coin)
        s.add(mkt)
        await s.flush()

        strat = Strategy(id=strategy_id_val, name=f"strat_{strategy_id_val}",
                         version="v1", params_json={"k": 3})
        s.add(strat)
        await s.flush()

        return mkt.id, strat.id


def _make_pos(strategy_id: int, market_id: int, status: PositionStatus) -> Position:
    return Position(
        strategy_id=strategy_id,
        market_id=market_id,
        mode="paper",
        status=status,
        opened_at=_T0,
        spot_units=0.1,
        perp_units=0.1,
        entry_spot_price=30000.0,
        entry_perp_price=30010.0,
    )


# ---------------------------------------------------------------------------
# Test 1: empty DB publishes nothing
# ---------------------------------------------------------------------------


async def test_empty_db_publishes_nothing(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    # No rows at all — don't even seed base rows, query should just return empty
    async with session_scope(session_factory) as s:
        strat = Strategy(name="empty_strat", version="v1", params_json={})
        s.add(strat)
        await s.flush()

    report = await scan(session_factory, 999, bus)

    assert report == ReconcileReport(0, 0)
    bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: only OPEN and CLOSED positions publish nothing
# ---------------------------------------------------------------------------


async def test_only_open_and_closed_publishes_nothing(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    mkt_id, strat_id = await _seed_base(session_factory, strategy_id_val=1)

    async with session_scope(session_factory) as s:
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPEN))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPEN))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.CLOSED))

    report = await scan(session_factory, strat_id, bus)

    assert report == ReconcileReport(0, 0)
    bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: FAILED position emits WARNING
# ---------------------------------------------------------------------------


async def test_failed_position_emits_warning(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    mkt_id, strat_id = await _seed_base(session_factory, strategy_id_val=1, coin="BTC")

    async with session_scope(session_factory) as s:
        pos = _make_pos(strat_id, mkt_id, PositionStatus.FAILED)
        s.add(pos)
        await s.flush()
        pos_id = pos.id

    report = await scan(session_factory, strat_id, bus)

    assert report == ReconcileReport(1, 0)
    bus.publish.assert_called_once()

    event = bus.publish.call_args.args[0]
    assert event.level == "WARNING"
    assert event.kind == "failed_position_found"
    assert event.source == "reconcile"
    assert event.payload_json["position_id"] == pos_id
    assert event.payload_json["coin"] == "BTC"


# ---------------------------------------------------------------------------
# Test 4: OPENING and CLOSING positions emit ERRORs
# ---------------------------------------------------------------------------


async def test_opening_and_closing_emit_errors(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    mkt_id, strat_id = await _seed_base(session_factory, strategy_id_val=1, coin="ETH")

    async with session_scope(session_factory) as s:
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPENING))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.CLOSING))

    report = await scan(session_factory, strat_id, bus)

    assert report == ReconcileReport(0, 2)
    assert bus.publish.call_count == 2

    events = [call.args[0] for call in bus.publish.call_args_list]
    for ev in events:
        assert ev.level == "ERROR"
        assert ev.kind == "stuck_position_state"

    statuses = {ev.payload_json["status"] for ev in events}
    assert statuses == {"opening", "closing"}


# ---------------------------------------------------------------------------
# Test 5: other strategy_id is ignored
# ---------------------------------------------------------------------------


async def test_other_strategy_id_ignored(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    # Seed two strategies with different IDs
    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL_multi", funding_interval_h=8, spot_taker_bps=7.0, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()

        mkt = Market(exchange_id=exc.id, coin="SOL")
        s.add(mkt)
        await s.flush()
        mkt_id = mkt.id

        strat1 = Strategy(name="strat_1", version="v1", params_json={})
        strat999 = Strategy(name="strat_999", version="v1", params_json={})
        s.add(strat1)
        s.add(strat999)
        await s.flush()
        strat1_id = strat1.id
        strat999_id = strat999.id

        # FAILED for strat999 (should be ignored)
        s.add(_make_pos(strat999_id, mkt_id, PositionStatus.FAILED))
        # FAILED for strat1 (should be reported)
        pos1 = _make_pos(strat1_id, mkt_id, PositionStatus.FAILED)
        s.add(pos1)
        await s.flush()
        pos1_id = pos1.id

    report = await scan(session_factory, strat1_id, bus)

    assert report == ReconcileReport(1, 0)
    bus.publish.assert_called_once()

    event = bus.publish.call_args.args[0]
    assert event.payload_json["position_id"] == pos1_id


# ---------------------------------------------------------------------------
# Test 6: mixed state full scan
# ---------------------------------------------------------------------------


async def test_mixed_state_full_scan(session_factory, mocker):
    bus = EventBus()
    mocker.patch.object(bus, "publish", new_callable=mocker.AsyncMock)

    mkt_id, strat_id = await _seed_base(session_factory, strategy_id_val=1, coin="AVAX")

    async with session_scope(session_factory) as s:
        # 2 FAILED, 1 OPENING, 1 CLOSING, 3 OPEN, 1 CLOSED
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.FAILED))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.FAILED))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPENING))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.CLOSING))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPEN))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPEN))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.OPEN))
        s.add(_make_pos(strat_id, mkt_id, PositionStatus.CLOSED))

    report = await scan(session_factory, strat_id, bus)

    assert report == ReconcileReport(2, 2)
    assert bus.publish.call_count == 4

    events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = [ev.kind for ev in events]

    # First two must be failed_position_found (FAILED query first)
    assert kinds[0] == "failed_position_found"
    assert kinds[1] == "failed_position_found"
    # Last two must be stuck_position_state (OPENING/CLOSING query second)
    assert kinds[2] == "stuck_position_state"
    assert kinds[3] == "stuck_position_state"
