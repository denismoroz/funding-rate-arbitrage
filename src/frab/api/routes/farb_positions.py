"""FarbPosition routes — composite arb positions (1 collateral + 1 spot + 1 perp leg)."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
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


def _fp_to_dict(
    fp: FarbPositionRow,
    legs: dict,
    hours_held: float | None,
    unrealized_pnl: float | None,
    margin_used_by_coin: dict[str, float] | None = None,
    leverage_by_coin: dict[str, int] | None = None,
    spot_mids: dict[str, float] | None = None,
    unrealized_pnl_by_coin: dict[str, float] | None = None,
) -> dict:
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

    # locked_margin_usdc: only meaningful for OPEN positions still tracked by HL
    state_str = fp.state.upper() if isinstance(fp.state, str) else fp.state
    is_open = (state_str == "OPEN")
    locked_margin = 0.0
    if is_open and margin_used_by_coin is not None:
        locked_margin = margin_used_by_coin.get(fp.coin, 0.0)

    # leverage: use live HL value for OPEN positions; fall back to state_data
    # historical record for CLOSED/FAILED (what was set at open time).
    if is_open and leverage_by_coin is not None:
        leverage = leverage_by_coin.get(fp.coin)
    else:
        leverage = sd.get("leverage")

    # capital_usdc: spot leg value (entry_price basis) + reserved margin.
    # Answers "how much capital does this position occupy" without mark drift.
    spot_leg = legs.get("spot")
    spot_value = (
        float(spot_leg["qty"]) * float(spot_leg["entry_price"])
        if spot_leg and spot_leg.get("qty") is not None and spot_leg.get("entry_price") is not None
        else 0.0
    )
    reserved = float(sd.get("required_margin") or 0)
    capital_usdc = spot_value + reserved

    # Per-leg PnL — prefer live HL data when exchange is available.
    # Falls back to the legacy _compute_unrealized_pnl result (unrealized_pnl)
    # when neither spot_mids nor unrealized_pnl_by_coin is provided (exchange=None).
    perp_pnl: float | None = None
    spot_pnl: float | None = None
    if is_open and (spot_mids is not None or unrealized_pnl_by_coin is not None):
        if unrealized_pnl_by_coin is not None:
            perp_pnl = unrealized_pnl_by_coin.get(fp.coin)
        if spot_leg and spot_mids is not None and spot_mids.get(fp.coin) is not None:
            spot_pnl = spot_leg["qty"] * (spot_mids[fp.coin] - spot_leg["entry_price"])
        total_pnl: float | None = None
        if perp_pnl is not None or spot_pnl is not None:
            total_pnl = (perp_pnl or 0.0) + (spot_pnl or 0.0)
    else:
        # Fallback path: exchange=None, use legacy combined PnL from DB prices
        total_pnl = unrealized_pnl

    return {
        "id": fp.id,
        "strategy_id": fp.strategy_id,
        "coin": fp.coin,
        "state": state_str,
        "state_data": sd,
        "opened_at_ms": fp.opened_at,
        "closed_at_ms": fp.closed_at,
        "legs": legs,
        "hours_held": hours_held,
        "target_signal_apr": sd.get("target_signal_apr"),
        "exit_signal_apr": sd.get("exit_signal_apr"),
        "current_signal_apr": current_apr,
        "consec_negative_hours": sd.get("consec_negative_hours"),
        "unrealized_pnl_usdc": total_pnl,
        "spot_unrealized_pnl_usdc": spot_pnl,
        "perp_unrealized_pnl_usdc": perp_pnl,
        "funding_usdc": funding_usdc,
        "fees_usdc": fees_usdc,
        "breakeven_hours_remaining": breakeven_hours,
        "locked_margin_usdc": locked_margin,
        "leverage": leverage,
        "capital_usdc": capital_usdc,
    }


async def _enrich(
    session: AsyncSession,
    fp: FarbPositionRow,
    margin_used_by_coin: dict[str, float] | None = None,
    leverage_by_coin: dict[str, int] | None = None,
    spot_mids: dict[str, float] | None = None,
    unrealized_pnl_by_coin: dict[str, float] | None = None,
) -> dict:
    """Load legs + compute hours_held + compute unrealized PnL for a single FarbPositionRow."""
    collateral = await _load_leg(session, fp.margin_position_id)
    spot = await _load_leg(session, fp.spot_position_id)
    perp = await _load_leg(session, fp.perp_position_id)

    legs = {"collateral": collateral, "spot": spot, "perp": perp}

    now = _now_ms()
    hours_held: float | None = None
    if fp.opened_at is not None:
        hours_held = (now - fp.opened_at) / 3_600_000

    # Fallback legacy PnL (used when exchange=None, i.e. no live HL data)
    unrealized_pnl: float | None = None
    if spot_mids is None and unrealized_pnl_by_coin is None:
        unrealized_pnl = await _compute_unrealized_pnl(session, fp, spot, perp)

    return _fp_to_dict(
        fp, legs, hours_held, unrealized_pnl,
        margin_used_by_coin, leverage_by_coin,
        spot_mids, unrealized_pnl_by_coin,
    )


@router.get("")
async def list_farb_positions(
    request: Request,
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

    # Fetch HL account state + spot mids once per request (best-effort; falls back to None)
    # None signals "exchange unavailable" → _enrich falls back to legacy DB-price PnL.
    margin_used_by_coin: dict[str, float] = {}
    leverage_by_coin: dict[str, int] = {}
    unrealized_pnl_by_coin: dict[str, float] | None = None
    spot_mids: dict[str, float] | None = None
    exchange = getattr(request.app.state, "exchange", None)
    if exchange is not None:
        unrealized_pnl_by_coin = {}
        spot_mids = {}
        try:
            hl_state, spot_mids = await asyncio.gather(
                exchange.fetch_account_state(),
                exchange.get_spot_mids_by_coin(),
            )
            hl_positions = hl_state.get("perp", {}).get("assetPositions") or []
        except Exception:
            hl_positions = []
            spot_mids = {}
        for ap in hl_positions:
            p = ap.get("position") or {}
            coin = p.get("coin")
            try:
                m = float(p.get("marginUsed", 0))
            except (TypeError, ValueError):
                continue
            if coin:
                margin_used_by_coin[coin] = m
            lev = (p.get("leverage") or {}).get("value")
            if coin and lev is not None:
                try:
                    leverage_by_coin[coin] = int(lev)
                except (TypeError, ValueError):
                    pass
            try:
                upnl = float(p.get("unrealizedPnl", 0))
                if coin:
                    unrealized_pnl_by_coin[coin] = upnl
            except (TypeError, ValueError):
                pass

    return [
        await _enrich(session, fp, margin_used_by_coin, leverage_by_coin, spot_mids, unrealized_pnl_by_coin)
        for fp in rows
    ]


@router.get("/{farb_position_id}")
async def get_farb_position(
    request: Request,
    farb_position_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    fp = await session.get(FarbPositionRow, farb_position_id)
    if fp is None:
        raise HTTPException(status_code=404, detail="FarbPosition not found")

    # Fetch HL account state + spot mids best-effort for single FP
    # None signals "exchange unavailable" → _enrich falls back to legacy DB-price PnL.
    margin_used_by_coin: dict[str, float] = {}
    leverage_by_coin: dict[str, int] = {}
    unrealized_pnl_by_coin: dict[str, float] | None = None
    spot_mids: dict[str, float] | None = None
    exchange = getattr(request.app.state, "exchange", None)
    if exchange is not None:
        unrealized_pnl_by_coin = {}
        spot_mids = {}
        try:
            hl_state, spot_mids = await asyncio.gather(
                exchange.fetch_account_state(),
                exchange.get_spot_mids_by_coin(),
            )
            hl_positions = hl_state.get("perp", {}).get("assetPositions") or []
        except Exception:
            hl_positions = []
            spot_mids = {}
        for ap in hl_positions:
            p = ap.get("position") or {}
            coin = p.get("coin")
            try:
                m = float(p.get("marginUsed", 0))
            except (TypeError, ValueError):
                continue
            if coin:
                margin_used_by_coin[coin] = m
            lev = (p.get("leverage") or {}).get("value")
            if coin and lev is not None:
                try:
                    leverage_by_coin[coin] = int(lev)
                except (TypeError, ValueError):
                    pass
            try:
                upnl = float(p.get("unrealizedPnl", 0))
                if coin:
                    unrealized_pnl_by_coin[coin] = upnl
            except (TypeError, ValueError):
                pass

    return await _enrich(session, fp, margin_used_by_coin, leverage_by_coin, spot_mids, unrealized_pnl_by_coin)
