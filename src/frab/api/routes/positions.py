from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import FillOut, PositionFundingAccrualOut, PositionOut
from frab.db.models import Fill, Market, Position, PositionFundingAccrual, PositionStatus, Price

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

    # Batch-load latest mark prices for all coins in the result set
    coins_in_use = {coin for _, coin in rows}
    if coins_in_use:
        market_q = await session.execute(
            select(Market.id, Market.coin).where(Market.coin.in_(coins_in_use))
        )
        coin_to_market_id = {coin: mid for mid, coin in market_q.all()}
        latest_marks: dict[str, float] = {}
        for coin, market_id in coin_to_market_id.items():
            price_q = await session.execute(
                select(Price.mark)
                .where(Price.market_id == market_id)
                .order_by(Price.ts.desc())
                .limit(1)
            )
            mark = price_q.scalar_one_or_none()
            if mark is not None:
                latest_marks[coin] = mark
    else:
        latest_marks = {}

    out = []
    for position, coin in rows:
        fill_list = [FillOut.model_validate(f) for f in fills_by_position.get(position.id, [])]

        mark = latest_marks.get(coin)
        notional_at_entry = position.spot_units * position.entry_spot_price
        if mark is not None:
            spot_value_now = position.spot_units * mark
            perp_unrealized = abs(position.perp_units) * (position.entry_perp_price - mark)
            net_mtm = (
                spot_value_now - notional_at_entry
                + perp_unrealized
                + position.funding_collected
                - position.fees_paid
            )
            current_mark_v = mark
        else:
            spot_value_now = None
            perp_unrealized = None
            net_mtm = None
            current_mark_v = None

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
            "current_mark": current_mark_v,
            "spot_value_now": spot_value_now,
            "perp_unrealized": perp_unrealized,
            "notional_at_entry": notional_at_entry,
            "net_mtm": net_mtm,
        }
        out.append(PositionOut.model_validate(pos_dict))
    return out


@router.get("/{position_id}/funding-history", response_model=list[PositionFundingAccrualOut])
async def get_position_funding_history(
    position_id: int,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
) -> list[PositionFundingAccrualOut]:
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    limit = min(limit, 5000)
    result = await session.execute(
        select(PositionFundingAccrual)
        .where(PositionFundingAccrual.position_id == position_id)
        .order_by(PositionFundingAccrual.ts.asc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return [PositionFundingAccrualOut.model_validate(r) for r in rows]
