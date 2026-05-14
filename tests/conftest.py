import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.session import init_db, make_session_factory, session_scope


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    # Enable FK constraints for SQLite
    from sqlalchemy import event

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
async def session(session_factory):
    async with session_scope(session_factory) as s:
        yield s
