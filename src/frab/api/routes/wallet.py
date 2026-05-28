"""GET /api/equity/wallet — live wallet balance from executor."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import SpotBalanceItem, WalletBalance
from frab.db.models import Market, Price

router = APIRouter()


@router.get("/wallet", response_model=WalletBalance)
async def get_wallet(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> WalletBalance:
    """Return live wallet balance from executor, or 503 when no executor is wired."""
    executor = getattr(request.app.state, "executor", None)
    if executor is None:
        raise HTTPException(status_code=503, detail="Engine not configured")

    # Pre-fetch latest mark per coin from DB so HL spot holdings get priced.
    mark_prices: dict[str, float] = {}
    latest_q = await session.execute(
        select(Market.coin, Price.mark, Price.ts)
        .join(Price, Price.market_id == Market.id)
        .order_by(Price.ts.desc())
        .limit(500)
    )
    for coin, mark, _ts in latest_q.all():
        if coin not in mark_prices:
            mark_prices[coin] = float(mark)

    try:
        raw = await executor.fetch_wallet_state(mark_prices=mark_prices)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to fetch wallet state from exchange: {exc}",
        ) from exc

    spot_balances = [
        SpotBalanceItem(
            coin=b["coin"],
            qty=b["qty"],
            mark=b["mark"],
            usd_value=b["usd_value"],
        )
        for b in raw["spot_balances"]
    ]

    return WalletBalance(
        perp_account_value=raw["perp_account_value"],
        perp_unrealized_pnl=raw["perp_unrealized_pnl"],
        spot_balances=spot_balances,
        usdc_spot=raw["usdc_spot"],
        total_usd=raw["total_usd"],
    )
