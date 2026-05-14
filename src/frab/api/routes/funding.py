from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import FundingRateOut
from frab.db.models import FundingRate, Market

router = APIRouter()


@router.get("/{coin}", response_model=list[FundingRateOut])
async def list_funding_rates(
    coin: str,
    exchange_id: int | None = None,
    since: datetime | None = None,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
) -> list[FundingRateOut]:
    stmt = (
        select(FundingRate, Market.coin)
        .join(Market, FundingRate.market_id == Market.id)
        .where(Market.coin == coin)
        .order_by(FundingRate.ts.desc())
        .limit(limit)
    )
    if exchange_id is not None:
        stmt = stmt.where(Market.exchange_id == exchange_id)
    if since is not None:
        stmt = stmt.where(FundingRate.ts >= since)

    result = await session.execute(stmt)
    rows = result.all()

    out = []
    for fr, coin_val in rows:
        fr_dict = {
            "id": fr.id,
            "market_id": fr.market_id,
            "coin": coin_val,
            "ts": fr.ts,
            "rate": fr.rate,
            "premium": fr.premium,
            "annualized_pct": fr.annualized_pct,
        }
        out.append(FundingRateOut.model_validate(fr_dict))
    return out
