from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import EventOut
from frab.db.models import Event

router = APIRouter()


@router.get("", response_model=list[EventOut])
async def list_events(
    level: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    stmt = select(Event).order_by(Event.ts.desc()).limit(limit)
    if level is not None:
        stmt = stmt.where(Event.level == level)
    if source is not None:
        stmt = stmt.where(Event.source == source)
    if since is not None:
        stmt = stmt.where(Event.ts >= since)

    result = await session.execute(stmt)
    events = result.scalars().all()
    return [EventOut.model_validate(e) for e in events]
