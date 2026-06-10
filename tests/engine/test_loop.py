"""Tests for EngineLoop._resolve_exchange_id — hasattr→init refactor (TASK D)
and EngineLoop.force_hour_tick (TASK E)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Exchange as ExchangeRow, Strategy
from frab.db.session import init_db, make_session_factory, session_scope
from frab.engine.loop import EngineLoop, StrategyIdMismatch


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


# ---------------------------------------------------------------------------
# TASK E — force_hour_tick
# ---------------------------------------------------------------------------


async def _seed_strategy(session_factory, strategy_id_hint: int = 1, params: dict | None = None) -> int:
    """Insert a Strategy row and return its id."""
    if params is None:
        params = {"k": 3, "leverage": 3.0}
    async with session_scope(session_factory) as s:
        row = Strategy(name="test_strat", version="v1", params_json=params, status="active")
        s.add(row)
        await s.flush()
        return row.id


class TestForceHourTick:
    """EngineLoop.force_hour_tick — happy path and mismatch."""

    async def test_mismatch_raises(self, seeded_session_factory):
        """strategy_id doesn't match running strategy → StrategyIdMismatch."""
        loop = _make_loop(seeded_session_factory)  # mock_strategy.strategy_id == 1
        with pytest.raises(StrategyIdMismatch) as exc_info:
            await loop.force_hour_tick(strategy_id=99, now_ms=0)
        assert exc_info.value.expected == 1
        assert exc_info.value.got == 99

    async def test_mismatch_error_message(self, seeded_session_factory):
        loop = _make_loop(seeded_session_factory)
        with pytest.raises(StrategyIdMismatch, match="strategy_id mismatch"):
            await loop.force_hour_tick(strategy_id=99, now_ms=0)

    async def test_happy_path_reloads_params_and_calls_hour_tick(
        self, seeded_session_factory, mocker
    ):
        """Happy path: reloads params from DB, calls _hour_tick, resets bypass flag."""
        # Seed a strategy row
        strategy_id = await _seed_strategy(seeded_session_factory, params={"k": 5, "leverage": 2.0})

        # Build a mock strategy with matching strategy_id
        mock_strategy = mocker.MagicMock()
        mock_strategy.strategy_id = strategy_id
        mock_strategy.params = None
        mock_strategy.force_entry_cooldown_bypass = False

        # Simulate reload_params actually updating mock_strategy.params
        def _reload_side_effect(new_params):
            mock_strategy.params = new_params

        mock_strategy.reload_params.side_effect = _reload_side_effect

        mock_exchange = type("MockExchange", (), {"name": "test_exchange"})()
        mock_ledger = object()

        loop = EngineLoop(
            strategy=mock_strategy,  # type: ignore[arg-type]
            exchange=mock_exchange,  # type: ignore[arg-type]
            ledger=mock_ledger,  # type: ignore[arg-type]
            session_factory=seeded_session_factory,
            coins=["BTC"],
        )

        # Patch _hour_tick to avoid actual DB/exchange calls
        mock_hour_tick = mocker.AsyncMock()
        mocker.patch.object(loop, "_hour_tick", mock_hour_tick)

        now_ms = 1_700_000_000_000
        await loop.force_hour_tick(strategy_id=strategy_id, now_ms=now_ms)

        # _hour_tick called with correct timestamp
        mock_hour_tick.assert_called_once_with(now_ms)

        # params was reloaded via reload_params(new_params)
        assert mock_strategy.params is not None

        # bypass flag was reset to False after the call
        assert mock_strategy.force_entry_cooldown_bypass is False

    async def test_bypass_flag_reset_even_on_hour_tick_error(
        self, seeded_session_factory, mocker
    ):
        """force_entry_cooldown_bypass is reset to False even if _hour_tick raises."""
        strategy_id = await _seed_strategy(seeded_session_factory)

        mock_strategy = mocker.MagicMock()
        mock_strategy.strategy_id = strategy_id
        mock_strategy.params = None
        mock_strategy.force_entry_cooldown_bypass = False

        mock_exchange = type("MockExchange", (), {"name": "test_exchange"})()

        loop = EngineLoop(
            strategy=mock_strategy,  # type: ignore[arg-type]
            exchange=mock_exchange,  # type: ignore[arg-type]
            ledger=object(),  # type: ignore[arg-type]
            session_factory=seeded_session_factory,
            coins=["BTC"],
        )

        mocker.patch.object(loop, "_hour_tick", mocker.AsyncMock(side_effect=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            await loop.force_hour_tick(strategy_id=strategy_id, now_ms=0)

        assert mock_strategy.force_entry_cooldown_bypass is False
