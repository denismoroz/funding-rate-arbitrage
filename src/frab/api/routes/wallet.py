"""GET /api/equity/wallet — live or synthesized wallet balance."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import SpotBalanceItem, WalletBalance
from frab.db.models import Market, Position, PositionStatus, Price
from frab.exchanges.dry_run import DryRunAdapterGuard

router = APIRouter()


async def _synthesize_paper_wallet(
    strategy_id: int,
    request: Request,
    session: AsyncSession,
) -> WalletBalance:
    """Build a WalletBalance from local DB state for paper-mode.

    - usdc_spot = strategy.cash (in-memory accumulator)
    - spot_balances = OPEN positions: spot_units * latest mark
    - perp_unrealized = Σ abs(perp_units) * (entry_perp_price - mark)
    - total_usd = sum of all
    """
    strategy = getattr(request.app.state, "strategy", None)
    live_strategy_id = getattr(request.app.state, "strategy_id", None)

    if strategy is None or live_strategy_id != strategy_id:
        raise HTTPException(status_code=503, detail="Engine not running for this strategy")

    # OPEN positions with coin names
    pos_rows = (await session.execute(
        select(Position, Market.coin)
        .join(Market, Position.market_id == Market.id)
        .where(
            Position.strategy_id == strategy_id,
            Position.status == PositionStatus.OPEN,
        )
    )).all()

    # Fetch latest mark prices for all coins in use
    coins_in_use = {coin for _, coin in pos_rows}
    latest_marks: dict[str, float] = {}
    for coin in coins_in_use:
        price_q = await session.execute(
            select(Price.mark)
            .join(Market, Price.market_id == Market.id)
            .where(Market.coin == coin)
            .order_by(Price.ts.desc())
            .limit(1)
        )
        mark = price_q.scalar_one_or_none()
        if mark is not None:
            latest_marks[coin] = mark

    spot_balances: list[SpotBalanceItem] = []
    perp_unrealized = 0.0

    for pos, coin in pos_rows:
        mark = latest_marks.get(coin, 0.0)
        if pos.spot_units > 0:
            spot_balances.append(SpotBalanceItem(
                coin=coin,
                qty=pos.spot_units,
                mark=mark,
                usd_value=pos.spot_units * mark,
            ))
        # perp unrealized: short perp means profit when mark < entry_perp_price
        if abs(pos.perp_units) > 0:
            perp_unrealized += abs(pos.perp_units) * (pos.entry_perp_price - mark)

    usdc_spot = strategy.cash
    spot_tokens_usd = sum(b.usd_value for b in spot_balances)
    # perp_account_value for paper: cash + perp unrealized (mirrors how HL accounts work)
    perp_account_value = usdc_spot + perp_unrealized
    total_usd = perp_account_value + spot_tokens_usd

    return WalletBalance(
        perp_account_value=perp_account_value,
        perp_unrealized_pnl=perp_unrealized,
        spot_balances=spot_balances,
        usdc_spot=usdc_spot,
        total_usd=total_usd,
    )


@router.get("/wallet", response_model=WalletBalance)
async def get_wallet(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> WalletBalance:
    """Return live wallet balance from HL (live/testnet) or synthesized from DB (paper)."""
    adapter = getattr(request.app.state, "adapter", None)
    executor = getattr(request.app.state, "executor", None)

    # Paper mode detection: adapter wrapped by DryRunAdapterGuard.
    # Falls back to executor duck-typing only when the adapter is absent
    # (e.g. older tests that don't wire it).
    is_paper = (
        isinstance(adapter, DryRunAdapterGuard)
        if adapter is not None
        else (executor is None or not callable(getattr(executor, "fetch_wallet_state", None)))
    )
    if is_paper:
        return await _synthesize_paper_wallet(strategy_id, request, session)

    # Live mode — pre-fetch latest mark per coin from DB so HL spot
    # holdings get priced (executor.fetch_wallet_state returns mark=0 otherwise).
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
