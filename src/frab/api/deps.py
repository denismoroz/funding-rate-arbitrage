from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.db.models import Strategy
from frab.db.session import session_scope


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_scope(session_factory) as session:
        yield session


async def get_strategy_or_404(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> Strategy:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy
