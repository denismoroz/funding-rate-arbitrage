"""Startup reconcile scan: surface broken/stuck Positions as Events.

Called once at server lifespan startup after strategy rehydration.
Reads the positions table and emits one Event per anomaly:

  - status=FAILED      → Event(level=WARNING, kind="failed_position_found")
  - status=OPENING|CLOSING → Event(level=ERROR, kind="stuck_position_state")

Does NOT mutate DB state — only observes and reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import select

from frab.db.models import Market, Position, PositionStatus
from frab.db.session import session_scope
from frab.events.bus import Event, EventBus


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    failed_count: int
    stuck_count: int


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def scan(
    session_factory,
    strategy_id: int,
    bus: EventBus,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReconcileReport:
    async with session_scope(session_factory) as session:
        # Query 1: FAILED positions
        stmt_failed = (
            select(Position, Market.coin)
            .join(Market, Position.market_id == Market.id)
            .where(
                Position.strategy_id == strategy_id,
                Position.status == PositionStatus.FAILED,
            )
            .order_by(Position.id.asc())
        )
        result_failed = await session.execute(stmt_failed)
        failed_rows = result_failed.all()

        for pos, coin in failed_rows:
            await bus.publish(Event(
                ts=clock(),
                level="WARNING",
                source="reconcile",
                kind="failed_position_found",
                message=f"Position {pos.id} ({coin}) is in FAILED state",
                payload_json={
                    "position_id": pos.id,
                    "coin": coin,
                    "opened_at": _iso(pos.opened_at),
                    "closed_at": _iso(pos.closed_at),
                },
            ))

        # Query 2: OPENING / CLOSING (stuck) positions
        stmt_stuck = (
            select(Position, Market.coin)
            .join(Market, Position.market_id == Market.id)
            .where(
                Position.strategy_id == strategy_id,
                Position.status.in_([PositionStatus.OPENING, PositionStatus.CLOSING]),
            )
            .order_by(Position.id.asc())
        )
        result_stuck = await session.execute(stmt_stuck)
        stuck_rows = result_stuck.all()

        for pos, coin in stuck_rows:
            await bus.publish(Event(
                ts=clock(),
                level="ERROR",
                source="reconcile",
                kind="stuck_position_state",
                message=f"Position {pos.id} ({coin}) is stuck in {pos.status.value} state",
                payload_json={
                    "position_id": pos.id,
                    "coin": coin,
                    "status": pos.status.value,
                    "opened_at": _iso(pos.opened_at),
                    "closed_at": _iso(pos.closed_at),
                },
            ))

    return ReconcileReport(
        failed_count=len(failed_rows),
        stuck_count=len(stuck_rows),
    )
