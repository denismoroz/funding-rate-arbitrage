"""Fixtures for TwoPhaseStrategy tests — in-memory SQLite."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.session import init_db, make_session_factory, session_scope
from frab.db.models import Exchange as ExchangeRow, Strategy, Position as PositionRow
from frab.domain.enums import Instrument, PositionStatus, Side
from frab.repo.farb_repo import FarbRepo

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC


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
    async with session_scope(session_factory) as s:
        strat = Strategy(name="two_phase_test", version="v2", params_json={"k": 3})
        s.add(strat)
        await s.flush()
        sid = strat.id
    return sid


@pytest_asyncio.fixture
async def exchange_id(session_factory) -> int:
    async with session_scope(session_factory) as s:
        exc = ExchangeRow(
            name="HL", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5
        )
        s.add(exc)
        await s.flush()
        eid = exc.id
    return eid


@pytest_asyncio.fixture
async def farb_repo(session_factory) -> FarbRepo:
    return FarbRepo(session_factory)


async def make_position(session_factory, *, exchange_id: int, coin: str,
                        instrument: Instrument, side: Side, qty: float = 0.01,
                        entry_price: float = 50000.0) -> int:
    """Insert a Position row and return its id."""
    async with session_scope(session_factory) as s:
        pos = PositionRow(
            exchange_id=exchange_id,
            coin=coin,
            instrument=instrument,
            side=side,
            qty=qty,
            entry_price=entry_price,
            opened_at=_NOW_MS,
            closed_at=None,
            status=PositionStatus.OPEN,
            farb_position_id=None,
        )
        s.add(pos)
        await s.flush()
        pid = pos.id
    return pid
