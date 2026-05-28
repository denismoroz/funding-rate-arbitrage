"""Fixtures for FarbRepo tests — in-memory SQLite, mirroring db/tests/conftest.py."""
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.session import init_db, make_session_factory, session_scope
from frab.db.models import Exchange, Position, Strategy
from frab.domain.enums import Instrument, PositionStatus, Side

_NOW_MS = 1704067200000  # 2024-01-01 00:00:00 UTC


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return make_session_factory(engine)


@pytest_asyncio.fixture
async def strategy_id(session_factory) -> int:
    """Insert a Strategy row and return its id."""
    async with session_scope(session_factory) as s:
        strat = Strategy(name="test_strategy", version="v1", params_json={"k": 3})
        s.add(strat)
        await s.flush()
        sid = strat.id
    return sid


@pytest_asyncio.fixture
async def real_position_id(session_factory) -> int:
    """Insert an Exchange + Position row. Returns the position id for FK-safe set_leg tests."""
    async with session_scope(session_factory) as s:
        exc = Exchange(name="HL", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5)
        s.add(exc)
        await s.flush()
        pos = Position(
            exchange_id=exc.id,
            coin="BTC",
            instrument=Instrument.SPOT,
            side=Side.LONG,
            qty=0.01,
            entry_price=50000.0,
            opened_at=_NOW_MS,
            closed_at=None,
            status=PositionStatus.OPEN,
            farb_position_id=None,
        )
        s.add(pos)
        await s.flush()
        pid = pos.id
    return pid
