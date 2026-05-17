from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import StrategyOut
from frab.db.models import Strategy
from frab.events.bus import Event
from frab.strategies.registry import get_strategy_spec

router = APIRouter()


@router.get("", response_model=list[StrategyOut])
async def list_strategies(session: AsyncSession = Depends(get_session)) -> list[StrategyOut]:
    result = await session.execute(select(Strategy).order_by(Strategy.id))
    strategies = result.scalars().all()
    return [StrategyOut.model_validate(s) for s in strategies]


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> StrategyOut:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyOut.model_validate(strategy)


@router.get("/{strategy_id}/params")
async def get_strategy_params(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    try:
        spec = get_strategy_spec(strategy.name)
        schema = spec.hot_param_schema()
        hot_schema_serialized = {
            k: {
                "type": v.type,
                "label": v.label,
                "min_value": v.min_value,
                "max_value": v.max_value,
                "exclusive_min": v.exclusive_min,
                "exclusive_max": v.exclusive_max,
                "description": v.description,
            }
            for k, v in schema.items()
        }
    except KeyError:
        hot_schema_serialized = {}  # unknown/legacy strategy — show params read-only

    return {
        "strategy_name": strategy.name,
        "version": strategy.version,
        "params": dict(strategy.params_json),
        "hot_schema": hot_schema_serialized,
    }


@router.post("/{strategy_id}/deploy")
async def deploy_strategy_params(
    strategy_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy_row = result.scalar_one_or_none()
    if strategy_row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # Resolve spec via registry
    try:
        spec = get_strategy_spec(strategy_row.name)
    except KeyError:
        raise HTTPException(
            status_code=501,
            detail=f"Strategy {strategy_row.name!r} is not registered — cannot hot-deploy",
        )

    # Engine check (must be running for this strategy)
    live_strategy = getattr(request.app.state, "strategy", None)
    live_strategy_id = getattr(request.app.state, "strategy_id", None)
    if live_strategy is None or live_strategy_id != strategy_id:
        raise HTTPException(status_code=503, detail="Engine not running for this strategy")

    # Validate body against spec
    try:
        validated = spec.validate_hot_params(body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Apply to live + persist to DB
    spec.apply_hot_params(live_strategy, validated)
    old_params = dict(strategy_row.params_json)
    merged = {**old_params, **validated}
    strategy_row.params_json = merged
    await session.flush()

    # Publish event
    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish(Event(
            ts=datetime.now(UTC),
            level="INFO",
            source="api",
            kind="strategy.params_updated",
            message=f"Strategy {strategy_id} ({strategy_row.name}) params updated",
            payload_json={"old": old_params, "new": merged},
        ))

    return {
        "strategy_name": strategy_row.name,
        "version": strategy_row.version,
        "params": merged,
    }


@router.post("/{strategy_id}/force-tick")
async def force_hour_tick(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark the next minute tick to also run hour-tick logic.

    Useful for testing decisions (open/close) without waiting up to an hour.
    Fires within ~60s on the next wall-clock minute boundary.
    """
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy_row = result.scalar_one_or_none()
    if strategy_row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    engine = getattr(request.app.state, "engine", None)
    live_strategy_id = getattr(request.app.state, "strategy_id", None)
    if engine is None or live_strategy_id != strategy_id:
        raise HTTPException(status_code=503, detail="Engine not running for this strategy")

    engine.force_hour_tick()

    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish(Event(
            ts=datetime.now(UTC),
            level="INFO",
            source="api",
            kind="engine.force_tick_requested",
            message=f"Hour tick forced for strategy {strategy_id}",
            payload_json=None,
        ))

    return {"status": "scheduled", "message": "Hour tick will fire on the next minute boundary (≤60s)"}
