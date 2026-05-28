"""Position routes — reads from positions + farb_positions tables."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import FarbPosition, Position

router = APIRouter()


@router.get("")
async def list_positions(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(Position).order_by(Position.id.desc()).limit(500)
    if status is not None:
        stmt = stmt.where(Position.status == status)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "id": p.id,
            "exchange_id": p.exchange_id,
            "coin": p.coin,
            "instrument": p.instrument,
            "side": p.side,
            "qty": p.qty,
            "entry_price": p.entry_price,
            "opened_at": p.opened_at,
            "closed_at": p.closed_at,
            "status": p.status,
            "farb_position_id": p.farb_position_id,
        }
        for p in rows
    ]


@router.get("/{position_id}/funding-history")
async def get_position_funding_history(
    position_id: int,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from frab.db.models import FundingAccrual
    stmt = (
        select(FundingAccrual)
        .where(FundingAccrual.position_id == position_id)
        .order_by(FundingAccrual.ts_ms.asc())
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        {"id": r.id, "position_id": r.position_id, "ts_ms": r.ts_ms, "amount": r.amount}
        for r in rows
    ]
