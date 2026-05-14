from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import SignalOut
from frab.db.models import Market, Signal

router = APIRouter()


@router.get("", response_model=list[SignalOut])
async def list_signals(
    strategy_id: int | None = None,
    coin: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
) -> list[SignalOut]:
    stmt = (
        select(Signal, Market.coin)
        .join(Market, Signal.market_id == Market.id)
        .order_by(Signal.ts.desc())
        .limit(limit)
    )
    if strategy_id is not None:
        stmt = stmt.where(Signal.strategy_id == strategy_id)
    if coin is not None:
        stmt = stmt.where(Market.coin == coin)
    if since is not None:
        stmt = stmt.where(Signal.ts >= since)

    result = await session.execute(stmt)
    rows = result.all()

    out = []
    for signal, coin_val in rows:
        sig_dict = {
            "id": signal.id,
            "strategy_id": signal.strategy_id,
            "market_id": signal.market_id,
            "coin": coin_val,
            "ts": signal.ts,
            "signal_value": signal.signal_value,
            "regime_pass": signal.regime_pass,
            "action": str(signal.action),
        }
        out.append(SignalOut.model_validate(sig_dict))
    return out
