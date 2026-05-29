import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.repo.farb_repo import FarbRepo


@pytest_asyncio.fixture
async def farb_repo(session_factory):
    return FarbRepo(session_factory)


@pytest_asyncio.fixture
async def api_client(session_factory, farb_repo):
    app = create_app(session_factory, farb_repo=farb_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as client:
        yield client
