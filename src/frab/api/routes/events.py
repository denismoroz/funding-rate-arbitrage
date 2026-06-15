"""Events routes — updated for new schema (ts_ms)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import Event

router = APIRouter()


@router.get("")
async def list_events(
    level: str | None = None,
    source: str | None = None,
    kind_prefix: str | None = None,
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(Event).order_by(Event.ts_ms.desc())
    if level is not None:
        stmt = stmt.where(Event.level == level)
    if source is not None:
        stmt = stmt.where(Event.source == source)
    if kind_prefix is not None:
        stmt = stmt.where(Event.kind.like(f"{kind_prefix}%"))
    stmt = stmt.offset(offset).limit(limit)

    result = await session.execute(stmt)
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "ts_ms": e.ts_ms,
            "level": e.level,
            "source": e.source,
            "kind": e.kind,
            "message": e.message,
            "payload_json": e.payload_json,
        }
        for e in events
    ]
