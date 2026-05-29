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
