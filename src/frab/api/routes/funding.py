"""Funding rate routes — updated for new schema (exchange_id + coin + ts_ms)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import FundingRate

router = APIRouter()


@router.get("/{coin}")
async def list_funding_rates(
    coin: str,
    exchange_id: int | None = None,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(FundingRate)
        .where(FundingRate.coin == coin)
        .order_by(FundingRate.ts_ms.desc())
        .limit(limit)
    )
    if exchange_id is not None:
        stmt = stmt.where(FundingRate.exchange_id == exchange_id)

    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [
        {
            "id": fr.id,
            "exchange_id": fr.exchange_id,
            "coin": fr.coin,
            "ts_ms": fr.ts_ms,
            "rate": fr.rate,
            "premium": fr.premium,
            "annualized_pct": fr.annualized_pct,
        }
        for fr in rows
    ]
