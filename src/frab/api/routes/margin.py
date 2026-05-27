"""GET /api/equity/margin — margin watchdog state for the running strategy."""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import MarginEventBrief, MarginStatusOut
from frab.db.models import Event as DbEvent
from frab.domain.exchange import Exchange

router = APIRouter()


_DEFAULTS_WHEN_DISABLED = dict(
    margin_manager_enabled=False,
    perp_cash=0.0,
    perp_unrealized=0.0,
    effective_equity=0.0,
    total_maintenance=0.0,
    margin_ratio=None,
    top_up_trigger=None,
    healthy_ratio=None,
    budget_committed=0.0,
    budget_cap_usd=None,
    n_open_positions=0,
    concurrency_cap=0,
    n_skipped_opens_capital=0,
    last_event=None,
)


async def _fetch_last_margin_event(session: AsyncSession) -> MarginEventBrief | None:
    stmt = (
        select(DbEvent)
        .where(DbEvent.source == "margin_watchdog")
        .order_by(DbEvent.ts.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    payload = row.payload_json or {}
    return MarginEventBrief(
        ts=row.ts,
        kind=row.kind,
        level=row.level,
        coin=payload.get("coin"),
        amount_transferred=float(payload.get("amount_transferred", 0.0)),
        ratio=float(payload.get("ratio", 0.0)),
    )


@router.get("/margin", response_model=MarginStatusOut)
async def get_margin_status(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MarginStatusOut:
    """Live margin state. When no MarginManager is wired, all margin-specific
    fields are null and `margin_manager_enabled=False`."""
    strategy = getattr(request.app.state, "strategy", None)
    live_strategy_id = getattr(request.app.state, "strategy_id", None)

    if strategy is None or live_strategy_id != strategy_id:
        raise HTTPException(status_code=503, detail="Engine not running for this strategy")

    last_event = await _fetch_last_margin_event(session)

    # Read portfolio state from canonical source.
    portfolio = await request.app.state.portfolio_service.current()
    hl_wallet = portfolio.wallet_per_exchange.get(Exchange.HYPERLIQUID)
    n_open_positions = len(portfolio.open_coins(Exchange.HYPERLIQUID))
    perp_cash = float(hl_wallet.reserved_usdc) if hl_wallet is not None else 0.0
    budget_committed = portfolio.total_committed(Exchange.HYPERLIQUID)

    mgr = getattr(strategy, "_margin_manager", None)
    if mgr is None:
        params = getattr(strategy, "_params", None)
        return MarginStatusOut(
            **{**_DEFAULTS_WHEN_DISABLED,
               "n_open_positions": n_open_positions,
               "perp_cash": perp_cash,
               "budget_committed": budget_committed,
               "concurrency_cap": int(getattr(params, "concurrency_cap", 0)),
               "last_event": last_event},
        )

    # Pull live marks from in-memory _last_quotes; positions without a quote
    # are skipped from the maintenance/unrealized totals.
    last_quotes = getattr(strategy, "_last_quotes", {})
    opens = strategy._open_position_snapshots_for_manager()
    opens_with_marks = [p for p in opens if p.coin in last_quotes]
    marks = {p.coin: last_quotes[p.coin].mark for p in opens_with_marks}

    if opens_with_marks:
        total_maint = mgr.compute_total_maintenance(opens_with_marks, marks)
        unrealized = mgr.compute_perp_unrealized(opens_with_marks, marks)
    else:
        total_maint = 0.0
        unrealized = 0.0

    effective_equity = perp_cash + unrealized
    if total_maint > 0:
        ratio: float | None = effective_equity / total_maint
    else:
        ratio = None

    params = strategy._params
    return MarginStatusOut(
        margin_manager_enabled=True,
        perp_cash=perp_cash,
        perp_unrealized=unrealized,
        effective_equity=effective_equity,
        total_maintenance=total_maint,
        margin_ratio=ratio if ratio is None or math.isfinite(ratio) else None,
        top_up_trigger=mgr.top_up_trigger,
        healthy_ratio=mgr.healthy_ratio,
        budget_committed=budget_committed,
        budget_cap_usd=mgr.budget_cap_usd,
        n_open_positions=n_open_positions,
        concurrency_cap=int(params.concurrency_cap),
        n_skipped_opens_capital=int(strategy.n_skipped_opens_capital),
        last_event=last_event,
    )
