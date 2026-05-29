"""Equity routes — updated for new schema (ts_ms)."""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import EquitySnapshot

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
        .order_by(EquitySnapshot.ts_ms.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    snapshots = result.scalars().all()
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
        state, spot_mids = await asyncio.gather(
            exchange.fetch_account_state(),
            exchange.get_spot_mids_by_coin(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to fetch HL state: {exc}",
        ) from exc

    perp_state = state.get("perp") or {}
    spot_state = state.get("spot") or {}

    margin = perp_state.get("marginSummary") or {}
    account_value = float(margin.get("accountValue", 0.0))
    margin_used = float(margin.get("totalMarginUsed", 0.0))

    short_notional = 0.0
    long_notional = 0.0
    unrealized = 0.0
    cum_funding_received = 0.0
    for entry in perp_state.get("assetPositions") or []:
        pos = entry.get("position") or {}
        try:
            szi = float(pos.get("szi", 0.0))
            pv = float(pos.get("positionValue", 0.0))
        except (TypeError, ValueError):
            continue
        if szi < 0:
            short_notional += pv
        elif szi > 0:
            long_notional += pv
        try:
            unrealized += float(pos.get("unrealizedPnl", 0.0))
        except (TypeError, ValueError):
            pass
        cf = pos.get("cumFunding") or {}
        try:
            cum_funding_received += -float(cf.get("sinceOpen", 0.0))
        except (TypeError, ValueError):
            pass

    # Local imports keep this route self-contained
    from frab.exchanges.hyperliquid.exchange import _SPOT_TOKEN_INVERSE

    spot_total_usdc = 0.0
    spot_hold_usdc = 0.0
    spot_tokens_value = 0.0
    for bal in spot_state.get("balances") or []:
        hl_coin = str(bal.get("coin", ""))
        try:
            total = float(bal.get("total", 0.0))
            hold = float(bal.get("hold", 0.0))
        except (TypeError, ValueError):
            continue
        if hl_coin in ("USDC", "USD"):
            spot_total_usdc += total
            spot_hold_usdc += hold
            continue
        canonical = _SPOT_TOKEN_INVERSE.get(hl_coin)
        if canonical is None:
            continue
        mid = spot_mids.get(canonical, 0.0)
        spot_tokens_value += total * mid

    spot_free_usdc = spot_total_usdc - spot_hold_usdc
    perp_standalone = account_value - spot_hold_usdc - unrealized - cum_funding_received
    total_equity = spot_total_usdc + perp_standalone + spot_tokens_value + unrealized + cum_funding_received

    return {
        "ts_ms": int(time.time() * 1000),
        "total": total_equity,
        "long": spot_tokens_value + long_notional,
        "short": short_notional,
        "free": spot_free_usdc,
        "margin": margin_used,
    }
