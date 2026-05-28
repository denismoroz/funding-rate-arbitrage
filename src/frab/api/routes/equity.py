"""Equity routes — updated for new schema (ts_ms)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import EquitySnapshot

router = APIRouter()


@router.get("")
async def list_equity(
    strategy_id: int,
    limit: int = 1000,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.strategy_id == strategy_id)
        .order_by(EquitySnapshot.ts_ms.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    snapshots = result.scalars().all()
    return [
        {
            "id": s.id,
            "strategy_id": s.strategy_id,
            "ts_ms": s.ts_ms,
            "total_equity": s.total_equity,
            "cash": s.cash,
            "spot_value": s.spot_value,
            "perp_unrealized": s.perp_unrealized,
            "perp_realized_cum": s.perp_realized_cum,
            "funding_cum": s.funding_cum,
            "fees_cum": s.fees_cum,
        }
        for s in snapshots
    ]


@router.get("/wallet-history")
async def list_wallet_history() -> list:
    return []
