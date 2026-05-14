from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import FillOut, PositionOut
from frab.db.models import Fill, Market, Position, PositionStatus

router = APIRouter()


@router.get("", response_model=list[PositionOut])
async def list_positions(
    strategy_id: int | None = None,
    status: PositionStatus | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
) -> list[PositionOut]:
    stmt = (
        select(Position, Market.coin)
        .join(Market, Position.market_id == Market.id)
        .order_by(Position.opened_at.desc())
        .limit(limit)
    )
    if strategy_id is not None:
        stmt = stmt.where(Position.strategy_id == strategy_id)
    if status is not None:
        stmt = stmt.where(Position.status == status)

    result = await session.execute(stmt)
    rows = result.all()

    if not rows:
        return []

    position_ids = [pos.id for pos, _ in rows]
    fills_result = await session.execute(
        select(Fill).where(Fill.position_id.in_(position_ids))
    )
    all_fills = fills_result.scalars().all()

    fills_by_position: dict[int, list[Fill]] = {}
    for fill in all_fills:
        fills_by_position.setdefault(fill.position_id, []).append(fill)

    out = []
    for position, coin in rows:
        fill_list = [FillOut.model_validate(f) for f in fills_by_position.get(position.id, [])]
        pos_dict = {
            "id": position.id,
            "strategy_id": position.strategy_id,
            "market_id": position.market_id,
            "coin": coin,
            "mode": str(position.mode),
            "status": str(position.status),
            "opened_at": position.opened_at,
            "closed_at": position.closed_at,
            "spot_units": position.spot_units,
            "perp_units": position.perp_units,
            "entry_spot_price": position.entry_spot_price,
            "entry_perp_price": position.entry_perp_price,
            "exit_spot_price": position.exit_spot_price,
            "exit_perp_price": position.exit_perp_price,
            "realized_pnl": position.realized_pnl,
            "funding_collected": position.funding_collected,
            "fees_paid": position.fees_paid,
            "fills": fill_list,
        }
        out.append(PositionOut.model_validate(pos_dict))
    return out
