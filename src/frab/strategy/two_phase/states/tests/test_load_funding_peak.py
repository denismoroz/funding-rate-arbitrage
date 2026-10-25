"""load_funding_peak: the high-water mark of cumulative funding from accrual rows."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from frab.db.models import Base, Exchange as DBExchange, FundingAccrual as DBFundingAccrual, Position as DBPosition
from frab.strategy.two_phase.states._helpers import load_funding_peak


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DBExchange(id=1, name="hyperliquid", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5))
        s.add(DBPosition(id=7, exchange_id=1, coin="PURR", instrument="PERP", side="SHORT", qty=77.0,
                         entry_price=0.14, opened_at=0, closed_at=None, status="OPEN"))
        await s.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _add(sf, rows):
    async with sf() as s:
        for ts, amount in rows:
            s.add(DBFundingAccrual(position_id=7, ts_ms=ts, amount=amount))
        await s.commit()


async def test_peak_is_running_max_in_time_order(sf):
    # inserted out of order: 0.3 at ts 1, -0.2 at ts 2, 0.1 at ts 3 -> sums 0.3, 0.1, 0.2
    await _add(sf, [(3, 0.1), (1, 0.3), (2, -0.2)])
    assert await load_funding_peak(sf, 7) == pytest.approx(0.3)


async def test_peak_of_only_negative_funding_is_zero(sf):
    await _add(sf, [(1, -0.1), (2, -0.05)])
    assert await load_funding_peak(sf, 7) == 0.0


async def test_no_rows(sf):
    assert await load_funding_peak(sf, 7) == 0.0
