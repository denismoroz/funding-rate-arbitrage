"""Funding rate routes — updated for new schema (exchange_id + coin + ts_ms)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import Exchange, FundingRate

router = APIRouter()

_DEFAULT_EXCHANGE_NAME = "hyperliquid"


async def _resolve_exchange_id(session: AsyncSession, exchange_id: int | None) -> int | None:
    """Return exchange_id if provided, otherwise look up the 'hyperliquid' exchange row.

    Returns None if neither an explicit id is given nor the default exchange row exists.
    """
    if exchange_id is not None:
        return exchange_id
    result = await session.execute(
        select(Exchange).where(Exchange.name == _DEFAULT_EXCHANGE_NAME)
    )
    row = result.scalar_one_or_none()
    return row.id if row is not None else None


@router.get("/{coin}")
async def list_funding_rates(
    coin: str,
    exchange_id: int | None = None,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    resolved_id = await _resolve_exchange_id(session, exchange_id)

    if resolved_id is None:
        # No explicit exchange given and no default exchange row — return empty
        return []

    stmt = (
        select(FundingRate)
        .where(FundingRate.coin == coin, FundingRate.exchange_id == resolved_id)
        .order_by(FundingRate.ts_ms.desc())
        .limit(limit)
    )

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
