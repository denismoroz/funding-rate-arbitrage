"""XSMOM routes — cross-sectional momentum strategy positions, scans, and params.

Pause / resume are NOT here: the UI calls
  POST /api/strategies/{xsmom_strategy_id}/pause
  POST /api/strategies/{xsmom_strategy_id}/resume
which are implemented in the strategies router and reuse the existing
Strategy row (name="xsmom", version="v1").
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.db.models import Position, Price, Strategy, XsmomPosition as XsmomPositionRow
from frab.domain.enums import XsmomState, XSMOM_ACTIVE_STATES
from frab.exchanges.protocol import WalletKind
from frab.repo.xsmom_repo import XsmomStateConflict
from frab.strategy.xsmom.params import XsmomParams

router = APIRouter()

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {XsmomState.CLOSED, XsmomState.FAILED}
_TERMINAL_STATE_VALUES = [s.value for s in _TERMINAL_STATES]
_ACTIVE_STATE_VALUES = [s.value for s in XSMOM_ACTIVE_STATES]
_NON_TERMINAL_STATE_VALUES = [s.value for s in XsmomState if s not in _TERMINAL_STATES]

_VALID_PARAM_KEYS: frozenset[str] = frozenset(XsmomParams.__dataclass_fields__.keys())


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── app.state guard ───────────────────────────────────────────────────────────

def _xsmom_state(request: Request) -> tuple:
    """Return (strategy, repo, exchange, strategy_id, loop) from app.state.

    Raises 503 if xsmom is not configured (xsmom_strategy_id missing).
    """
    strategy_id = getattr(request.app.state, "xsmom_strategy_id", None)
    if strategy_id is None:
        raise HTTPException(status_code=503, detail="xsmom engine not configured")
    return (
        getattr(request.app.state, "xsmom_strategy", None),
        getattr(request.app.state, "xsmom_repo", None),
        getattr(request.app.state, "xsmom_exchange", None),
        strategy_id,
        getattr(request.app.state, "xsmom_loop", None),
    )


# ── HL enrichment ─────────────────────────────────────────────────────────────

async def _fetch_xsmom_hl_enrichment(exchange: object) -> tuple[
    dict[str, float],          # unrealized_pnl_by_coin
    dict[str, float],          # margin_used_by_coin
    dict[str, int],            # leverage_by_coin
]:
    """Best-effort fetch of HL account snapshot for xsmom_exchange.

    Returns ({}, {}, {}) if exchange is None or the call fails.
    """
    if exchange is None:
        return {}, {}, {}

    unrealized_pnl_by_coin: dict[str, float] = {}
    margin_used_by_coin: dict[str, float] = {}
    leverage_by_coin: dict[str, int] = {}

    try:
        (perp_state, _spot_state) = await exchange.get_account_snapshot()
        for ap in perp_state.asset_positions:
            coin = ap.coin
            if not coin:
                continue
            unrealized_pnl_by_coin[coin] = ap.unrealized_pnl
            margin_used_by_coin[coin] = ap.margin_used
            if ap.leverage_value is not None:
                leverage_by_coin[coin] = ap.leverage_value
    except Exception:  # noqa: BLE001
        pass

    return unrealized_pnl_by_coin, margin_used_by_coin, leverage_by_coin


# ── position helpers ──────────────────────────────────────────────────────────

async def _load_perp_leg(session: AsyncSession, position_id: int | None) -> dict | None:
    """Load a Position row and return {id, qty, entry_price} or None."""
    if position_id is None:
        return None
    row = await session.get(Position, position_id)
    if row is None:
        return None
    return {"id": row.id, "qty": row.qty, "entry_price": row.entry_price}


async def _latest_mark(session: AsyncSession, coin: str) -> float | None:
    """Fetch the most recent mark price for a coin from the DB."""
    stmt = (
        select(Price)
        .where(Price.coin == coin)
        .order_by(Price.ts_ms.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return row.mark if row is not None else None


def _compute_db_pnl(
    side: str,
    qty: float | None,
    entry_price: float | None,
    mark: float | None,
) -> float | None:
    """Compute DB-fallback unrealized PnL for a single perp leg."""
    if qty is None or entry_price is None or mark is None:
        return None
    side_upper = side.upper() if isinstance(side, str) else side
    if side_upper == "LONG":
        return qty * (mark - entry_price)
    if side_upper == "SHORT":
        return qty * (entry_price - mark)
    return None


async def _enrich_xsmom_position(
    session: AsyncSession,
    row: XsmomPositionRow,
    unrealized_pnl_by_coin: dict[str, float],
    margin_used_by_coin: dict[str, float],
    leverage_by_coin: dict[str, int],
) -> dict:
    """Serialise one XsmomPositionRow into the API response shape."""
    sd: dict = row.state_data or {}
    coin: str = row.coin
    state_str: str = row.state.upper() if isinstance(row.state, str) else row.state

    # ── legs ─────────────────────────────────────────────────────────────────
    perp_leg = await _load_perp_leg(session, row.perp_position_id)

    # ── time ─────────────────────────────────────────────────────────────────
    now = _now_ms()
    hours_held: float | None = None
    if row.opened_at is not None:
        hours_held = (now - row.opened_at) / 3_600_000

    # ── unrealized PnL ───────────────────────────────────────────────────────
    is_open = state_str == XsmomState.OPENED.value.upper()
    unrealized_pnl: float | None = None
    if is_open and unrealized_pnl_by_coin:
        # Prefer live HL data
        unrealized_pnl = unrealized_pnl_by_coin.get(coin)
    if unrealized_pnl is None and perp_leg is not None:
        # DB fallback
        mark = await _latest_mark(session, coin)
        unrealized_pnl = _compute_db_pnl(
            row.side if isinstance(row.side, str) else row.side.value,
            perp_leg.get("qty"),
            perp_leg.get("entry_price"),
            mark,
        )

    # ── margin / leverage ─────────────────────────────────────────────────────
    locked_margin: float = 0.0
    if is_open and margin_used_by_coin:
        locked_margin = margin_used_by_coin.get(coin, 0.0)
    if not locked_margin:
        locked_margin = float(sd.get("required_margin") or 0.0)

    leverage = None
    if is_open and leverage_by_coin:
        leverage = leverage_by_coin.get(coin)
    if leverage is None:
        leverage = sd.get("leverage")

    # ── derived fields ────────────────────────────────────────────────────────
    notional = float(sd.get("notional") or 0.0)
    if not notional and perp_leg and perp_leg.get("qty") and perp_leg.get("entry_price"):
        notional = perp_leg["qty"] * perp_leg["entry_price"]

    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "coin": coin,
        "side": row.side if isinstance(row.side, str) else row.side.value,
        "state": state_str,
        "state_data": sd,
        "score": sd.get("score"),
        "perp_leg": perp_leg,
        "hours_held": hours_held,
        "unrealized_pnl_usdc": unrealized_pnl,
        "funding_usdc": float(sd.get("gross_funding_so_far") or 0.0),
        "fees_usdc": float(sd.get("total_fees_paid") or 0.0),
        "notional": notional,
        "required_margin": float(sd.get("required_margin") or 0.0),
        "locked_margin_usdc": locked_margin,
        "leverage": leverage,
        "opened_at_ms": row.opened_at,
        "closed_at_ms": row.closed_at,
    }


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/summary")
async def get_xsmom_summary(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return high-level portfolio summary for the XSMOM strategy.

    cash: spot USDC wallet balance (best-effort; 0.0 on failure).
    long_total / short_total: sum of notional across OPENED long/short legs.
    pnl_total: sum of unrealized PnL across OPENED positions (null on HL failure,
               falls back to DB-derived values).
    """
    _strategy, _repo, exchange, strategy_id, _loop = _xsmom_state(request)

    # USDC spot cash (best-effort)
    cash: float = 0.0
    if exchange is not None:
        try:
            cash = await exchange.get_wallet("USDC", WalletKind.SPOT)
        except Exception:  # noqa: BLE001
            cash = 0.0

    # OPENED positions
    stmt = (
        select(XsmomPositionRow)
        .where(
            XsmomPositionRow.strategy_id == strategy_id,
            XsmomPositionRow.state.in_(_ACTIVE_STATE_VALUES),
        )
    )
    result = await session.execute(stmt)
    opened_rows = result.scalars().all()

    # HL enrichment for PnL
    unrealized_pnl_by_coin, margin_used_by_coin, leverage_by_coin = (
        await _fetch_xsmom_hl_enrichment(exchange)
    )

    long_total = 0.0
    short_total = 0.0
    locked = 0.0
    pnl_total: float | None = 0.0
    pnl_available = True

    for row in opened_rows:
        sd = row.state_data or {}
        notional = float(sd.get("notional") or 0.0)
        side_str = row.side if isinstance(row.side, str) else row.side.value

        if side_str.upper() == "LONG":
            long_total += notional
        else:
            short_total += notional

        # Locked margin: prefer live HL margin_used, fallback to reserved required_margin.
        coin_margin = margin_used_by_coin.get(row.coin) if margin_used_by_coin else None
        if coin_margin is None:
            coin_margin = float(sd.get("required_margin") or 0.0)
        locked += coin_margin

        # PnL: prefer HL, fallback DB
        if unrealized_pnl_by_coin:
            pnl = unrealized_pnl_by_coin.get(row.coin)
        else:
            pnl = None

        if pnl is None:
            # DB fallback — load perp leg
            perp_leg = await _load_perp_leg(session, row.perp_position_id)
            if perp_leg is not None:
                mark = await _latest_mark(session, row.coin)
                pnl = _compute_db_pnl(side_str, perp_leg.get("qty"), perp_leg.get("entry_price"), mark)

        if pnl is None:
            pnl_available = False
        elif pnl_total is not None:
            pnl_total += pnl

    if not pnl_available:
        pnl_total = None

    return {
        "cash": cash,
        "locked": locked,
        "free": max(cash - locked, 0.0),
        "long_total": long_total,
        "short_total": short_total,
        "pnl_total": pnl_total,
        "n_long": sum(
            1 for r in opened_rows
            if (r.side if isinstance(r.side, str) else r.side.value).upper() == "LONG"
        ),
        "n_short": sum(
            1 for r in opened_rows
            if (r.side if isinstance(r.side, str) else r.side.value).upper() == "SHORT"
        ),
    }


@router.get("/positions")
async def list_xsmom_positions(
    request: Request,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List XsmomPositions, optionally filtered by status.

    status values:
      - "active"  — all non-terminal states (not CLOSED, FAILED)
      - "open"    — state = OPENED
      - "closed"  — state = CLOSED
      - "failed"  — state = FAILED
      - None      — all states
    """
    _strategy, _repo, exchange, strategy_id, _loop = _xsmom_state(request)

    stmt = (
        select(XsmomPositionRow)
        .where(XsmomPositionRow.strategy_id == strategy_id)
        .order_by(XsmomPositionRow.id.desc())
        .limit(500)
    )

    if status is not None:
        s = status.lower()
        if s == "active":
            stmt = stmt.where(XsmomPositionRow.state.in_(_NON_TERMINAL_STATE_VALUES))
        elif s == "open":
            stmt = stmt.where(XsmomPositionRow.state.in_(_ACTIVE_STATE_VALUES))
        elif s == "closed":
            stmt = stmt.where(XsmomPositionRow.state == XsmomState.CLOSED.value)
        elif s == "failed":
            stmt = stmt.where(XsmomPositionRow.state == XsmomState.FAILED.value)
        else:
            raise HTTPException(status_code=422, detail=f"Unknown status filter: {status!r}")

    result = await session.execute(stmt)
    rows = result.scalars().all()

    unrealized_pnl_by_coin, margin_used_by_coin, leverage_by_coin = (
        await _fetch_xsmom_hl_enrichment(exchange)
    )

    return [
        await _enrich_xsmom_position(
            session, row, unrealized_pnl_by_coin, margin_used_by_coin, leverage_by_coin
        )
        for row in rows
    ]


@router.post("/positions/{xsmom_position_id}/close")
async def close_xsmom_position(
    xsmom_position_id: int,
    request: Request,
) -> dict:
    """Force-close a single OPENED XsmomPosition by transitioning it to CLOSE.

    The minute-tick drives it from CLOSE → CLOSED.
    Returns 404 if missing, 409 if not in OPENED state.
    """
    xsmom_strategy, xsmom_repo, _exchange, strategy_id, _loop = _xsmom_state(request)

    if xsmom_strategy is None:
        raise HTTPException(status_code=503, detail="xsmom_strategy not available")

    # Pre-check for clean 404 vs 409
    if xsmom_repo is not None:
        fp = await xsmom_repo.get(xsmom_position_id)
        if fp is None:
            raise HTTPException(status_code=404, detail="XsmomPosition not found")
        if fp.state != XsmomState.OPENED:
            raise HTTPException(
                status_code=409,
                detail=f"XsmomPosition {xsmom_position_id} is in state {fp.state.value!r}, not 'opened'",
            )

    try:
        updated = await xsmom_strategy.manual_close(xsmom_position_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="XsmomPosition not found")
    except XsmomStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "id": updated.id,
        "coin": updated.coin,
        "new_state": updated.state.value,
        "ts_ms": _now_ms(),
    }


@router.post("/positions/close-all")
async def close_all_xsmom_positions(request: Request) -> dict:
    """Transition all OPENED XsmomPositions to CLOSE.

    The minute-tick drives them to CLOSED. Returns the list of closed positions.
    """
    xsmom_strategy, _repo, _exchange, strategy_id, _loop = _xsmom_state(request)

    if xsmom_strategy is None:
        raise HTTPException(status_code=503, detail="xsmom_strategy not available")

    closed = await xsmom_strategy.close_all()
    return {
        "closed": [{"id": p.id, "coin": p.coin} for p in closed],
        "ts_ms": _now_ms(),
    }


@router.post("/rebalance")
async def manual_rebalance(request: Request) -> dict:
    """Force an immediate rebalance regardless of schedule or pause status.

    Refreshes history, runs scan, reconciles positions. Returns the reconcile
    summary {kept, opened, dropped, flipped} plus ts_ms.
    """
    xsmom_strategy, _repo, _exchange, strategy_id, _loop = _xsmom_state(request)

    if xsmom_strategy is None:
        raise HTTPException(status_code=503, detail="xsmom_strategy not available")

    now = _now_ms()
    result = await xsmom_strategy.manual_rebalance(now_ms=now)
    return {**result, "ts_ms": now}


@router.get("/scans")
async def list_xsmom_scans(
    request: Request,
    limit: int = 50,
) -> list[dict]:
    """Return the most recent XsmomScan records (most recent first)."""
    _strategy, xsmom_repo, _exchange, strategy_id, _loop = _xsmom_state(request)

    if xsmom_repo is None:
        raise HTTPException(status_code=503, detail="xsmom_repo not available")

    return await xsmom_repo.latest_scans(strategy_id, limit)


@router.get("/params")
async def get_xsmom_params(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the xsmom strategy's params_json."""
    _strategy, _repo, _exchange, strategy_id, _loop = _xsmom_state(request)

    row = await session.get(Strategy, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="xsmom strategy row not found")

    params = dict(row.params_json) if row.params_json else {}
    return {
        "params": params,
        "universe": params.get("universe", []),
    }


@router.post("/equity/reset")
async def reset_xsmom_equity(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Set equity_baseline_ms = now in the xsmom Strategy row's params_json (clip the equity chart start)."""
    _strategy, _repo, _exchange, strategy_id, _loop = _xsmom_state(request)
    row = await session.get(Strategy, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="xsmom strategy row not found")
    now = _now_ms()
    current = dict(row.params_json) if row.params_json else {}
    current["equity_baseline_ms"] = now
    row.params_json = current
    session.add(row)
    return {"equity_baseline_ms": now}


class PatchXsmomParamsBody(BaseModel):
    params: dict


@router.patch("/params")
async def patch_xsmom_params(
    body: PatchXsmomParamsBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Merge partial params onto the xsmom Strategy row's params_json.

    Validates keys against XsmomParams fields → 422 on unknown keys.
    Also validates:
      - n_positions: positive even int or null
      - budget_cap: > 0
      - universe: non-empty list of uppercase tickers

    Returns {params_json, restart_required: True}.
    Engine picks up the new params on the next hour-tick via EngineLoop params_loader.
    """
    _strategy, _repo, _exchange, strategy_id, loop = _xsmom_state(request)

    # Key validation
    unknown = sorted(set(body.params.keys()) - _VALID_PARAM_KEYS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown param keys: {unknown}. Valid keys: {sorted(_VALID_PARAM_KEYS)}",
        )

    # Value validation
    if "n_positions" in body.params:
        n = body.params["n_positions"]
        if n is not None:
            if not isinstance(n, int) or n <= 0 or n % 2 != 0:
                raise HTTPException(
                    status_code=422,
                    detail="n_positions must be a positive even integer or null",
                )

    if "budget_cap" in body.params:
        bc = body.params["budget_cap"]
        if not isinstance(bc, (int, float)) or bc <= 0:
            raise HTTPException(status_code=422, detail="budget_cap must be > 0")

    if "universe" in body.params:
        uni = body.params["universe"]
        if not isinstance(uni, list) or len(uni) == 0:
            raise HTTPException(
                status_code=422,
                detail="universe must be a non-empty list of uppercase tickers",
            )
        if not all(isinstance(t, str) and t == t.upper() for t in uni):
            raise HTTPException(
                status_code=422,
                detail="universe must be a non-empty list of uppercase tickers",
            )

    row = await session.get(Strategy, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="xsmom strategy row not found")

    current = dict(row.params_json) if row.params_json else {}
    new_params = {**current, **body.params}
    row.params_json = new_params
    session.add(row)

    # Persist now so the engine's separate-session reload sees the new params.
    await session.commit()

    reloaded = False
    if loop is not None:
        try:
            await loop.reload_params_from_db()
            reloaded = True
        except Exception:  # noqa: BLE001
            logger.exception("xsmom: live param reload failed; will apply on next hour tick")

    return {
        "params_json": new_params,
        "restart_required": not reloaded,
        "reloaded": reloaded,
    }
