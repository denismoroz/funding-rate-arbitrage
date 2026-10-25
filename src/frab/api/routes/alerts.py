"""Alerts route.

Today the only alerts are FRAB positions that need to exit but cannot be closed within
HL's limits (the pre-close check, strategy/two_phase/close_gate.py): they are derived
from the live `state_data` of non-terminal farb_positions, so an alert disappears as soon
as the position can close (or no longer needs to).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import FarbPosition as FarbPositionRow
from frab.domain.enums import FarbState

router = APIRouter()

_NON_TERMINAL_STATES = [
    s.value for s in FarbState if s not in (FarbState.CLOSED, FarbState.FAILED)
]


@router.get("")
async def list_alerts(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    rows = (await session.execute(
        select(FarbPositionRow)
        .where(
            FarbPositionRow.strategy_id == strategy_id,
            FarbPositionRow.state.in_(_NON_TERMINAL_STATES),
        )
        .order_by(FarbPositionRow.id)
    )).scalars().all()

    alerts: list[dict] = []
    for fp in rows:
        sd = fp.state_data or {}
        if not sd.get("close_blocked"):
            continue
        since_ms = sd.get("close_blocked_since_ms")
        ts = datetime.fromtimestamp(
            (since_ms if since_ms is not None else fp.opened_at) / 1000, tz=timezone.utc,
        )
        alerts.append({
            "type": "close_blocked",
            "severity": "ERROR",
            "ts": ts.isoformat(),
            "coin": fp.coin,
            "message": (
                f"can't close - needs to: {sd.get('close_blocked_wanted')}; "
                f"{sd.get('close_blocked_reason')}"
            ),
            "position_id": fp.id,
            "payload": {
                "since_ms": since_ms,
                "wanted": sd.get("close_blocked_wanted"),
                "reason": sd.get("close_blocked_reason"),
                "check": sd.get("close_blocked_check"),
            },
        })
    return alerts
