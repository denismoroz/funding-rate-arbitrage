"""Strategy routes — updated for new schema."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import Strategy

router = APIRouter()


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


@router.post("/{strategy_id}/deploy")
async def deploy_strategy_params(
    strategy_id: int,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    raise HTTPException(status_code=503, detail="Engine not configured")


@router.post("/{strategy_id}/force-tick")
async def force_hour_tick(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    raise HTTPException(status_code=503, detail="Engine not configured")
