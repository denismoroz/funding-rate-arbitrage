from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from frab.db.session import session_scope


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_scope(session_factory) as session:
        yield session
