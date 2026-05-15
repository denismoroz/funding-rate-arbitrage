from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import StrategyOut, StrategyParamsIn, StrategyParamsOut
from frab.db.models import Strategy
from frab.events.bus import Event

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


@router.get("/{strategy_id}/params", response_model=StrategyParamsOut)
async def get_strategy_params(
    strategy_id: int,
    session: AsyncSession = Depends(get_session),
) -> StrategyParamsOut:
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyParamsOut.model_validate(strategy.params_json)


@router.post("/{strategy_id}/deploy", response_model=StrategyParamsOut)
async def deploy_strategy_params(
    strategy_id: int,
    params_in: StrategyParamsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StrategyParamsOut:
    # 1. Load strategy; 404 if missing.
    result = await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy_row = result.scalar_one_or_none()
    if strategy_row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # 2. Check engine is running for this strategy.
    live_strategy = getattr(request.app.state, "strategy", None)
    live_strategy_id = getattr(request.app.state, "strategy_id", None)
    if live_strategy is None or live_strategy_id != strategy_id:
        raise HTTPException(status_code=503, detail="Engine not running for this strategy")

    # 3. Build merged dict: keep cold fields, override hot fields.
    old_params = dict(strategy_row.params_json)
    hot_fields = {
        "entry_threshold": params_in.entry_threshold,
        "exit_threshold": params_in.exit_threshold,
        "min_hold_hours": params_in.min_hold_hours,
        "concurrency_cap": params_in.concurrency_cap,
        "position_size_usdc": params_in.position_size_usdc,
    }
    merged = {**old_params, **hot_fields}

    # 4. Call live strategy's update_hot_params.
    live_strategy.update_hot_params(**hot_fields)

    # 5. Persist merged params to DB.
    strategy_row.params_json = merged
    await session.flush()

    # 6. Publish event to bus if available.
    bus = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        await bus.publish(Event(
            ts=datetime.now(UTC),
            level="INFO",
            source="api",
            kind="strategy.params_updated",
            message=f"Strategy {strategy_id} params updated",
            payload_json={"old": old_params, "new": merged},
        ))

    # 7. Return merged params.
    return StrategyParamsOut.model_validate(merged)


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
