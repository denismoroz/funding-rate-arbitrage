"""Equity routes — updated for new schema (ts_ms)."""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import EquitySnapshot, FarbPosition as DBFarbPosition
from frab.domain.enums import FarbState
from frab.domain.equity import total_equity_usd

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("")
async def list_equity(
    strategy_id: int,
    limit: int = 1000,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.strategy_id == strategy_id)
        .order_by(EquitySnapshot.ts_ms.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    snapshots = result.scalars().all()
    snapshots.reverse()  # ascending order for chart
    return [
        {
            "id": s.id,
            "strategy_id": s.strategy_id,
            "ts_ms": s.ts_ms,
            "total_equity": s.total_equity,
            "cash": s.cash,
            "spot_value": s.spot_value,
            "perp_unrealized": s.perp_unrealized,
            "perp_realized_cum": s.perp_realized_cum,
            "funding_cum": s.funding_cum,
            "fees_cum": s.fees_cum,
        }
        for s in snapshots
    ]


@router.get("/wallet-history")
async def list_wallet_history() -> list:
    return []


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    """Live HL account breakdown: total / long / short / free."""
    exchange = getattr(request.app.state, "exchange", None)
    if exchange is None:
        raise HTTPException(status_code=503, detail="Exchange not configured")

    try:
        (perp_state, spot_state), spot_mids = await asyncio.gather(
            exchange.get_account_snapshot(),
            exchange.get_spot_mids_by_coin(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to fetch HL state: {exc}",
        ) from exc

    short_notional = 0.0
    long_notional = 0.0
    for ap in perp_state.asset_positions:
        if ap.szi < 0:
            short_notional += ap.position_value
        elif ap.szi > 0:
            long_notional += ap.position_value

    from frab.exchanges.hyperliquid.symbols import SPOT_TOKEN_INVERSE

    spot_total_usdc = 0.0
    spot_hold_usdc = 0.0
    spot_tokens_value = 0.0
    for bal in spot_state.balances:
        if bal.coin in ("USDC", "USD"):
            spot_total_usdc += bal.total
            spot_hold_usdc += bal.hold
            continue
        canonical = SPOT_TOKEN_INVERSE.get(bal.coin)
        if canonical is None:
            continue
        mid = spot_mids.get(canonical, 0.0)
        spot_tokens_value += bal.total * mid

    # locked: sum of marginUsed across HL assetPositions
    locked_usdc = sum(ap.margin_used for ap in perp_state.asset_positions)

    # reserved: sum of state_data.required_margin over all currently OPEN FarbPositions in DB
    sf = getattr(request.app.state, "session_factory", None)
    reserved_usdc = 0.0
    if sf is not None:
        async with sf() as s:
            rows = (await s.execute(
                select(DBFarbPosition).where(DBFarbPosition.state == FarbState.OPEN.name)
            )).scalars().all()
        reserved_usdc = sum(
            float((r.state_data or {}).get("required_margin", 0) or 0) for r in rows
        )

    # free: spot USDC total minus reserved (not minus hold, which is a HL internal concept)
    spot_free_usdc = spot_total_usdc - reserved_usdc

    total_equity = total_equity_usd(spot_total_usdc, spot_tokens_value)

    return {
        "ts_ms": int(time.time() * 1000),
        "total": total_equity,
        "long": spot_tokens_value + long_notional,
        "short": short_notional,
        "free": spot_free_usdc,
        "locked": locked_usdc,
        "reserved": reserved_usdc,
    }
