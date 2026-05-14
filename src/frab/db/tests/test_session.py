"""Unit tests for session helpers: session_scope, init_db, create_engine."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from frab.db.models import Exchange
from frab.db.session import (
    create_engine,
    init_db,
    make_session_factory,
    session_scope,
)


def make_exchange(**kwargs) -> Exchange:
    defaults = dict(name="TestHL", funding_interval_h=8, spot_taker_bps=7.0, perp_taker_bps=2.5)
    defaults.update(kwargs)
    return Exchange(**defaults)


# ---------------------------------------------------------------------------
# test_session_scope_commits_on_success
# ---------------------------------------------------------------------------

async def test_session_scope_commits_on_success(engine, session_factory, mocker):
    spy = mocker.spy(AsyncSession, "commit")

    async with session_scope(session_factory) as s:
        s.add(make_exchange(name="CommitTest"))

    # commit was called exactly once
    assert spy.call_count == 1

    # object persisted — verify via new session
    async with session_scope(session_factory) as s2:
        result = await s2.execute(select(Exchange).where(Exchange.name == "CommitTest"))
        row = result.scalar_one_or_none()
    assert row is not None
    assert row.name == "CommitTest"


# ---------------------------------------------------------------------------
# test_session_scope_rolls_back_on_exception
# ---------------------------------------------------------------------------

async def test_session_scope_rolls_back_on_exception(engine, session_factory, mocker):
    spy = mocker.spy(AsyncSession, "rollback")

    with pytest.raises(ValueError, match="boom"):
        async with session_scope(session_factory) as s:
            s.add(make_exchange(name="RollbackTest"))
            raise ValueError("boom")

    assert spy.call_count == 1

    # object must NOT be persisted
    async with session_scope(session_factory) as s2:
        result = await s2.execute(select(Exchange).where(Exchange.name == "RollbackTest"))
        row = result.scalar_one_or_none()
    assert row is None


# ---------------------------------------------------------------------------
# test_session_scope_closes_session
# ---------------------------------------------------------------------------

async def test_session_scope_closes_session(engine, session_factory, mocker):
    spy = mocker.spy(AsyncSession, "close")

    async with session_scope(session_factory) as s:
        s.add(make_exchange(name="CloseTest"))

    assert spy.call_count >= 1


# ---------------------------------------------------------------------------
# test_init_db_creates_tables
# ---------------------------------------------------------------------------

async def test_init_db_creates_tables():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    try:
        await init_db(eng)
        factory = make_session_factory(eng)
        async with session_scope(factory) as s:
            s.add(make_exchange(name="InitTest"))
        # If no exception — table was created successfully
        async with session_scope(factory) as s:
            result = await s.execute(select(Exchange).where(Exchange.name == "InitTest"))
            assert result.scalar_one_or_none() is not None
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# test_create_engine_uses_aiosqlite
# ---------------------------------------------------------------------------

async def test_create_engine_uses_aiosqlite():
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert eng.url.get_backend_name() == "sqlite"
        assert eng.url.get_driver_name() == "aiosqlite"
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# test_create_engine_enables_sqlite_fks (pragma enforcement)
# ---------------------------------------------------------------------------

async def test_create_engine_enables_sqlite_fks():
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with eng.connect() as conn:
            result = await conn.execute(text("PRAGMA foreign_keys"))
            value = result.scalar()
        assert value == 1
    finally:
        await eng.dispose()
