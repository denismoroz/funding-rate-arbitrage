"""Strategy routes — stubbed in Step 1 (engine not wired)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import StrategyOut
from frab.db.models import Strategy

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
