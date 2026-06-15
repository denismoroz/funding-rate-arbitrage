from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine as _create_async_engine,
)

from frab.db.models import Base


def _configure_sqlite(dbapi_connection, _connection_record):
    """Per-connection SQLite pragmas.

    WAL + busy_timeout are essential here: the process runs TWO EngineLoops
    (FRAB + XSMOM) writing every minute PLUS the API serving the web poller.
    In the default rollback journal with busy_timeout=0 any read/write overlap
    raises "database is locked" immediately → 500s. WAL lets readers run
    concurrently with a single writer; busy_timeout makes a contending writer
    wait instead of erroring. synchronous=NORMAL is the recommended, safe
    companion for WAL.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_engine(db_url: str) -> AsyncEngine:
    engine = _create_async_engine(db_url, future=True, echo=False)
    if engine.url.get_backend_name() == "sqlite":
        event.listen(engine.sync_engine, "connect", _configure_sqlite)
    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session: AsyncSession = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
