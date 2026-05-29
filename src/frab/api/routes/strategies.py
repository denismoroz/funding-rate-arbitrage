"""Strategy routes — updated for new schema."""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import Strategy
from frab.db.session import session_scope
from frab.events.bus import Event
from frab.strategy.two_phase import TwoPhaseParams

router = APIRouter()

_VALID_PARAM_KEYS: frozenset[str] = frozenset(TwoPhaseParams.__dataclass_fields__.keys())


class PatchParamsBody(BaseModel):
    params: dict


@router.get("")
async def list_strategies(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(select(Strategy).order_by(Strategy.id))
    strategies = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "version": s.version,
            "params_json": s.params_json,
            "status": s.status,
            "started_at_ms": s.started_at_ms,
            "stopped_at_ms": s.stopped_at_ms,
        }
        for s in strategies
    ]


@router.get("/{strategy_id}")
async def get_strategy(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {
        "id": strategy.id,
        "name": strategy.name,
        "version": strategy.version,
        "params_json": strategy.params_json,
        "status": strategy.status,
        "started_at_ms": strategy.started_at_ms,
        "stopped_at_ms": strategy.stopped_at_ms,
    }


@router.get("/{strategy_id}/params")
async def get_strategy_params(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {
        "strategy_name": strategy.name,
        "version": strategy.version,
        "params": dict(strategy.params_json),
        "hot_schema": {},  # engine not configured
    }


@router.patch("/{strategy_id}/params")
async def patch_strategy_params(
    strategy_id: int,
    body: PatchParamsBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Merge partial params dict onto the strategy's params_json.

    Keys must be valid TwoPhaseParams field names. Unknown keys → 422.
    Returns the updated params_json plus restart_required=true.
    """
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    unknown_keys = sorted(set(body.params.keys()) - _VALID_PARAM_KEYS)
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown param keys: {unknown_keys}. Valid keys: {sorted(_VALID_PARAM_KEYS)}",
        )

    current = dict(strategy.params_json) if strategy.params_json else {}
    new_params = {**current, **body.params}
    strategy.params_json = new_params
    session.add(strategy)

    return {
        "id": strategy.id,
        "params_json": new_params,
        "restart_required": True,
        "note": "params_json updated; engine must be restarted to pick up changes",
    }


@router.post("/{strategy_id}/pause")
async def pause_strategy(strategy_id: int, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Set strategy status to 'paused'. Idempotent."""
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    strategy.status = "paused"
    session.add(strategy)
    ts_ms = int(time.time() * 1000)

    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        await event_bus.publish(Event(
            ts=datetime.now(timezone.utc),
            level="INFO",
            source="api",
            kind="strategy.paused",
            message=f"strategy {strategy_id} paused",
        ))

    return {"id": strategy_id, "status": "paused", "ts_ms": ts_ms}


@router.post("/{strategy_id}/resume")
async def resume_strategy(strategy_id: int, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Set strategy status to 'active'. Idempotent."""
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    strategy.status = "active"
    session.add(strategy)
    ts_ms = int(time.time() * 1000)

    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        await event_bus.publish(Event(
            ts=datetime.now(timezone.utc),
            level="INFO",
            source="api",
            kind="strategy.resumed",
            message=f"strategy {strategy_id} resumed",
        ))

    return {"id": strategy_id, "status": "active", "ts_ms": ts_ms}


@router.post("/{strategy_id}/force-tick")
async def force_hour_tick(strategy_id: int, request: Request) -> dict:
    """Force an immediate hour-tick on the running engine: fetch funding,
    refresh wallet snapshots, run strategy.on_hour_tick. Useful for manual
    testing without waiting for the next hour boundary."""
    import time

    engine_loop = getattr(request.app.state, "engine_loop", None)
    if engine_loop is None:
        raise HTTPException(status_code=503, detail="Engine not configured")

    running_strategy_id = engine_loop._strategy.strategy_id
    if running_strategy_id != strategy_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"strategy_id mismatch: engine is running id={running_strategy_id}, "
                f"got id={strategy_id}"
            ),
        )

    # Reload params from DB so PATCH /params takes effect without restart.
    session_factory = request.app.state.session_factory
    async with session_scope(session_factory) as s:
        row = (await s.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        engine_loop._strategy.params = TwoPhaseParams.from_dict(dict(row.params_json))

    now_ms = int(time.time() * 1000)
    engine_loop._strategy.force_entry_cooldown_bypass = True
    try:
        await engine_loop._hour_tick(now_ms)
    finally:
        engine_loop._strategy.force_entry_cooldown_bypass = False
    return {"status": "ok", "ts_ms": now_ms, "message": "hour tick forced (params reloaded)"}
