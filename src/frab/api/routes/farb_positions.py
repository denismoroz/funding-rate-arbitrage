"""FarbPosition routes — composite arb positions (1 collateral + 1 spot + 1 perp leg)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import FarbPosition as FarbPositionRow, Position, Price
from frab.domain.enums import FarbState

router = APIRouter()

# Terminal states — FarbPosition in one of these is not "active"
_TERMINAL_STATES = {FarbState.OPEN.value, FarbState.CLOSED.value, FarbState.FAILED.value}
_NON_TERMINAL_STATES = [
    s.value for s in FarbState
    if s not in (FarbState.OPEN, FarbState.CLOSED, FarbState.FAILED)
]


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _load_leg(session: AsyncSession, position_id: int | None) -> dict | None:
    """Load a Position row and return {id, qty, entry_price} or None."""
    if position_id is None:
        return None
    row = await session.get(Position, position_id)
    if row is None:
        return None
    return {"id": row.id, "qty": row.qty, "entry_price": row.entry_price}


async def _latest_price(session: AsyncSession, coin: str, exchange_id: int | None) -> Price | None:
    """Fetch the most recent Price row for a coin."""
    stmt = (
        select(Price)
        .where(Price.coin == coin)
        .order_by(Price.ts_ms.desc())
        .limit(1)
    )
    if exchange_id is not None:
        stmt = stmt.where(Price.exchange_id == exchange_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _compute_unrealized_pnl(
    session: AsyncSession,
    fp: FarbPositionRow,
    spot_leg: dict | None,
    perp_leg: dict | None,
) -> float | None:
    """
    Compute unrealized PnL for the position.

    perp short: pnl = qty * (entry_price - latest_mark)
    spot long:  pnl = qty * (latest_spot - entry_price)

    Returns None if there is no perp leg yet or no recent price data.
    """
    if perp_leg is None:
        return None

    # Resolve exchange_id from one of the leg positions to use for price lookup
    exchange_id: int | None = None
    if fp.perp_position_id is not None:
        row = await session.get(Position, fp.perp_position_id)
        if row is not None:
            exchange_id = row.exchange_id

    price_row = await _latest_price(session, fp.coin, exchange_id)
    if price_row is None:
        return None

    # Perp short P&L
    pnl = perp_leg["qty"] * (perp_leg["entry_price"] - price_row.mark)

    # Add spot long P&L if available
    if spot_leg is not None and price_row.spot is not None:
        pnl += spot_leg["qty"] * (price_row.spot - spot_leg["entry_price"])

    return pnl


_HOURS_PER_YEAR = 8760


def _fp_to_dict(fp: FarbPositionRow, legs: dict, hours_held: float | None, unrealized_pnl: float | None) -> dict:
    """Serialize a FarbPositionRow to the response shape."""
    sd = fp.state_data or {}

    funding_usdc = float(sd.get("gross_funding_so_far") or 0.0)
    fees_usdc = float(sd.get("total_fees_paid") or 0.0)
    current_apr = sd.get("current_signal_apr")

    # Break-even based on the *current* smoothed signal (refreshed each hour by
    # the strategy's accrual step). Hours of forward funding at this APR needed
    # to cover (fees - funding_so_far). None if signal ≤ 0 or no spot leg.
    spot = legs.get("spot")
    breakeven_hours: float | None = None
    if (
        current_apr is not None
        and current_apr > 0
        and spot is not None
        and spot.get("qty")
        and spot.get("entry_price")
    ):
        notional = spot["qty"] * spot["entry_price"]
        hourly_income = notional * current_apr / _HOURS_PER_YEAR
        if hourly_income > 0:
            remaining = max(fees_usdc - funding_usdc, 0.0)
            breakeven_hours = remaining / hourly_income

    return {
        "id": fp.id,
        "strategy_id": fp.strategy_id,
        "coin": fp.coin,
        "state": fp.state.upper() if isinstance(fp.state, str) else fp.state,
        "state_data": sd,
        "opened_at_ms": fp.opened_at,
        "closed_at_ms": fp.closed_at,
        "legs": legs,
        "hours_held": hours_held,
        "target_signal_apr": sd.get("target_signal_apr"),
        "exit_signal_apr": sd.get("exit_signal_apr"),
        "current_signal_apr": current_apr,
        "consec_negative_hours": sd.get("consec_negative_hours"),
        "unrealized_pnl_usdc": unrealized_pnl,
        "funding_usdc": funding_usdc,
        "fees_usdc": fees_usdc,
        "breakeven_hours_remaining": breakeven_hours,
    }


async def _enrich(session: AsyncSession, fp: FarbPositionRow) -> dict:
    """Load legs + compute hours_held + compute unrealized PnL for a single FarbPositionRow."""
    collateral = await _load_leg(session, fp.margin_position_id)
    spot = await _load_leg(session, fp.spot_position_id)
    perp = await _load_leg(session, fp.perp_position_id)

    legs = {"collateral": collateral, "spot": spot, "perp": perp}

    now = _now_ms()
    hours_held: float | None = None
    if fp.opened_at is not None:
        hours_held = (now - fp.opened_at) / 3_600_000

    unrealized_pnl = await _compute_unrealized_pnl(session, fp, spot, perp)

    return _fp_to_dict(fp, legs, hours_held, unrealized_pnl)


@router.get("")
async def list_farb_positions(
    strategy_id: int,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """
    List FarbPositions for a strategy, optionally filtered by status.

    status values:
      - "active"  — all non-terminal states (NOT OPEN, CLOSED, FAILED)
      - "open"    — state = OPEN
      - "closed"  — state = CLOSED
      - "failed"  — state = FAILED
      - None      — all states
    """
    stmt = (
        select(FarbPositionRow)
        .where(FarbPositionRow.strategy_id == strategy_id)
        .order_by(FarbPositionRow.id.desc())
        .limit(500)
    )

    if status is not None:
        s = status.lower()
        if s == "active":
            stmt = stmt.where(FarbPositionRow.state.in_(_NON_TERMINAL_STATES))
        elif s == "open":
            stmt = stmt.where(FarbPositionRow.state == FarbState.OPEN.value)
        elif s == "closed":
            stmt = stmt.where(FarbPositionRow.state == FarbState.CLOSED.value)
        elif s == "failed":
            stmt = stmt.where(FarbPositionRow.state == FarbState.FAILED.value)
        else:
            raise HTTPException(status_code=422, detail=f"Unknown status filter: {status!r}")

    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [await _enrich(session, fp) for fp in rows]


@router.get("/{farb_position_id}")
async def get_farb_position(
    farb_position_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    fp = await session.get(FarbPositionRow, farb_position_id)
    if fp is None:
        raise HTTPException(status_code=404, detail="FarbPosition not found")
    return await _enrich(session, fp)
