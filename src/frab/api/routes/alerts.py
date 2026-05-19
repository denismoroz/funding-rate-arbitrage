"""GET /api/alerts — combined feed of FAILED positions + recent failure Events.

Returns a single list, newest first by `ts`, suitable for the dashboard's
AlertBanner. FAILED positions are always included regardless of `since`
(they represent unresolved state). Failure Events are filtered by `since`.

Failure-Event kinds surfaced:
  - paired_open_failed     (atomic_executor)
  - paired_close_failed    (atomic_executor)
  - retry_exhausted        (atomic_executor)
  - failed_position_found  (reconcile)
  - stuck_position_state   (reconcile)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import AlertOut
from frab.db.models import Event, Market, Position, PositionStatus

router = APIRouter()

FAILURE_KINDS = (
    "paired_open_failed",
    "paired_close_failed",
    "retry_exhausted",
    "failed_position_found",
    "stuck_position_state",
)


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    strategy_id: int,
    since: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[AlertOut]:
    if since is None:
        since = datetime.now(UTC) - timedelta(hours=24)

    # Query A — FAILED positions for this strategy
    pos_stmt = (
        select(Position, Market.coin)
        .join(Market, Position.market_id == Market.id)
        .where(
            Position.strategy_id == strategy_id,
            Position.status == PositionStatus.FAILED,
        )
        .order_by(Position.id.asc())
    )
    pos_result = await session.execute(pos_stmt)
    position_alerts: list[AlertOut] = []
    for pos, coin in pos_result.all():
        position_alerts.append(
            AlertOut(
                type="failed_position",
                severity="WARNING",
                ts=pos.opened_at,
                coin=coin,
                message=f"Position {pos.id} ({coin}) is in FAILED state",
                position_id=pos.id,
                payload={"realized_pnl": pos.realized_pnl, "fees_paid": pos.fees_paid},
            )
        )

    # Query B — failure Events since `since`
    # TODO: filter by strategy_id once Event gets that column
    evt_stmt = (
        select(Event)
        .where(Event.ts >= since, Event.kind.in_(FAILURE_KINDS))
        .order_by(Event.ts.desc())
    )
    evt_result = await session.execute(evt_stmt)
    event_alerts: list[AlertOut] = []
    for event in evt_result.scalars().all():
        event_alerts.append(
            AlertOut(
                type="event",
                severity=event.level,
                ts=event.ts,
                coin=event.payload_json.get("coin") if event.payload_json else None,
                message=event.message,
                position_id=event.payload_json.get("position_id") if event.payload_json else None,
                payload=event.payload_json,
            )
        )

    merged = position_alerts + event_alerts
    merged.sort(key=lambda a: a.ts, reverse=True)
    return merged[:200]
