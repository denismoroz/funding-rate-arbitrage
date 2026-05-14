import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app


@pytest_asyncio.fixture
async def api_client(session_factory):
    app = create_app(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as client:
        yield client
