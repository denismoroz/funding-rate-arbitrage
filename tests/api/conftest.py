import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from frab.api.app import create_app
from frab.db.session import init_db, make_session_factory


@pytest_asyncio.fixture
async def engine():
    from sqlalchemy import event

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
async def api_client(session_factory):
    """API client with no executor (paper-mode without engine running)."""
    app = create_app(session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        yield client


@pytest_asyncio.fixture
async def api_client_with_executor(session_factory):
    """Return a factory: api_client_with_executor(executor, strategy, strategy_id)."""
    created: list[AsyncClient] = []

    async def _make(executor, strategy=None, strategy_id=None):
        app = create_app(session_factory, executor=executor)
        app.state.strategy = strategy
        app.state.strategy_id = strategy_id
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
        )
        await client.__aenter__()
        created.append(client)
        return client

    yield _make

    for c in created:
        await c.__aexit__(None, None, None)
