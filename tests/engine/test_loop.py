"""Tests for EngineLoop._resolve_exchange_id — hasattr→init refactor (TASK D)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Exchange as ExchangeRow
from frab.db.session import init_db, make_session_factory, session_scope
from frab.engine.loop import EngineLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loop(session_factory, exchange_name: str = "test_exchange") -> EngineLoop:
    """Build a minimal EngineLoop with mocked strategy/exchange/ledger."""
    mock_exchange = type("MockExchange", (), {"name": exchange_name})()
    mock_strategy = type("MockStrategy", (), {"strategy_id": 1})()
    mock_ledger = object()
    return EngineLoop(
        strategy=mock_strategy,  # type: ignore[arg-type]
        exchange=mock_exchange,  # type: ignore[arg-type]
        ledger=mock_ledger,  # type: ignore[arg-type]
        session_factory=session_factory,
        coins=["BTC"],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_engine():
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
async def session_factory(db_engine):
    return make_session_factory(db_engine)


@pytest_asyncio.fixture
async def seeded_session_factory(session_factory):
    """Session factory with an 'test_exchange' row pre-inserted."""
    async with session_scope(session_factory) as s:
        row = ExchangeRow(
            name="test_exchange",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=3.5,
        )
        s.add(row)
    return session_factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExchangeIdCacheInit:
    """_exchange_id_cache is declared in __init__, not via hasattr."""

    def test_cache_starts_none(self, seeded_session_factory):
        loop = _make_loop(seeded_session_factory)
        assert loop._exchange_id_cache is None

    def test_no_hasattr_in_resolve(self):
        import inspect
        import frab.engine.loop as loop_mod

        src = inspect.getsource(loop_mod.EngineLoop._resolve_exchange_id)
        assert "hasattr" not in src, "_resolve_exchange_id must not use hasattr"


class TestResolveExchangeId:
    """_resolve_exchange_id returns the correct DB id and caches it."""

    async def test_returns_exchange_id(self, seeded_session_factory):
        loop = _make_loop(seeded_session_factory)
        eid = await loop._resolve_exchange_id()
        assert isinstance(eid, int)
        assert eid >= 1

    async def test_caches_after_first_call(self, seeded_session_factory, mocker):
        loop = _make_loop(seeded_session_factory)
        assert loop._exchange_id_cache is None

        eid = await loop._resolve_exchange_id()
        assert loop._exchange_id_cache == eid

        # Second call must NOT hit the DB — patch session_scope to assert it is not called
        mock_scope = mocker.patch("frab.engine.loop.session_scope")
        eid2 = await loop._resolve_exchange_id()
        mock_scope.assert_not_called()
        assert eid2 == eid

    async def test_raises_when_exchange_not_in_db(self, session_factory):
        """No matching Exchange row → RuntimeError mentioning 'frab seed'."""
        loop = _make_loop(session_factory, exchange_name="nonexistent")
        with pytest.raises(RuntimeError, match="frab seed"):
            await loop._resolve_exchange_id()

    async def test_cache_survives_multiple_calls(self, seeded_session_factory):
        loop = _make_loop(seeded_session_factory)
        results = [await loop._resolve_exchange_id() for _ in range(5)]
        assert len(set(results)) == 1, "All calls must return the same id"
        assert loop._exchange_id_cache == results[0]
