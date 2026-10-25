"""Shared helpers used by multiple State implementations."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.session import session_scope
from frab.domain import Position
from frab.events.bus import Event, EventBus


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


async def publish_event(
    bus: EventBus | None,
    *,
    level: str,
    kind: str,
    message: str,
    payload: dict | None = None,
) -> None:
    """No-op if bus is None. Identical to TwoPhaseStrategy._publish."""
    if bus is None:
        return
    await bus.publish(Event(
        ts=datetime.now(timezone.utc),
        level=level,
        source="strategy",
        kind=kind,
        message=message,
        payload_json=payload,
    ))


async def load_position(
    session_factory: async_sessionmaker[AsyncSession],
    position_id: int,
) -> Position:
    """Load a Position domain object from DB by id.
    Verbatim port of TwoPhaseStrategy._get_position."""
    from frab.db.models import Position as PositionRow
    from frab.domain import Position as DomainPosition
    from frab.domain.enums import Instrument as Inst, PositionStatus, Side as S

    async with session_scope(session_factory) as session:
        row = await session.get(PositionRow, position_id)
        if row is None:
            raise KeyError(f"Position {position_id} not found")
        return DomainPosition(
            id=row.id,
            exchange_name=str(row.exchange_id),
            coin=row.coin,
            instrument=Inst(row.instrument),
            side=S(row.side),
            qty=row.qty,
            entry_price=row.entry_price,
            opened_at=datetime.fromtimestamp(row.opened_at / 1000, tz=timezone.utc),
            closed_at=(
                datetime.fromtimestamp(row.closed_at / 1000, tz=timezone.utc)
                if row.closed_at is not None
                else None
            ),
            status=PositionStatus(row.status),
            farb_position_id=row.farb_position_id,
        )


async def load_funding_peak(
    session_factory: async_sessionmaker[AsyncSession],
    position_id: int,
) -> float:
    """High-water mark of the position's cumulative funding, from its accrual
    rows in time order. Derived from the rows rather than carried forward, so a
    correction to the rows (the doubled HL daily summaries) corrects the peak
    too; a carried peak would keep the old high and read the correction as
    funding given back."""
    from sqlalchemy import select

    from frab.db.models import FundingAccrual as FundingAccrualRow

    async with session_scope(session_factory) as session:
        amounts = (await session.execute(
            select(FundingAccrualRow.amount)
            .where(FundingAccrualRow.position_id == position_id)
            .order_by(FundingAccrualRow.ts_ms, FundingAccrualRow.id)
        )).scalars().all()
    total = peak = 0.0
    for amount in amounts:
        total += amount
        peak = max(peak, total)
    return peak
