"""Strategy B v2 (paper) API: summary, equity curve, fills."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from frab.db.models import Strategy
from frab.repo.b2_repo import B2Repo
from frab.strategy.b2.params import B2Params

router = APIRouter()


def _state(request: Request) -> tuple[int, B2Repo]:
    sid = getattr(request.app.state, "b2_strategy_id", None)
    if sid is None:
        raise HTTPException(status_code=503, detail="B2 engine is not running")
    return sid, B2Repo(request.app.state.session_factory)


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    sid, repo = _state(request)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    params = B2Params.from_dict(dict(row.params_json))
    books = {b.coin: b for b in await repo.books(sid)}
    latest = {e.coin: e for e in await repo.latest_equity(sid)}
    series = await repo.equity_series(sid, len(params.coins))
    engine = getattr(request.app.state, "b2_engine", None)

    coins = []
    for coin in params.coins:
        b, e = books.get(coin), latest.get(coin)
        if b is None or e is None:
            coins.append({"coin": coin, "started": False})
            continue
        coins.append({
            "coin": coin, "started": True, "price": e.price, "equity": e.equity, "capital": b.capital,
            "pnl": e.equity - b.capital, "pnl_pct": (e.equity / b.capital - 1) * 100,
            "hedge_on": e.hedge_on, "carry_on": e.carry_on, "spot_value": e.spot_value,
            "short_pnl": e.short_pnl, "cash": e.cash, "carry_cash": e.carry_cash,
            "funding_on_hedge": b.funding_total, "hedge_realized": b.short_realized,
            "carry_funding": b.carry_funding, "fees": b.perp_fees + b.spot_fees + b.carry_fees,
            "hedges": b.trades, "rebalances": b.rebals, "carry_entries": b.carry_trades,
        })
    started = [c for c in coins if c["started"]]
    capital = params.capital_usd
    equity = sum(c["equity"] for c in started) if len(started) == len(params.coins) else None
    return {
        "mode": params.mode, "status": row.status, "params": params.to_dict(),
        "capital": capital, "equity": equity,
        "pnl": None if equity is None else equity - capital,
        "pnl_pct": None if equity is None else (equity / capital - 1) * 100,
        "started_ms": series[0][0] if series else None, "last_bar_ms": series[-1][0] if series else None,
        "hours": len(series), "engine_last_tick_ms": getattr(engine, "last_tick_ms", None),
        "engine_last_error": getattr(engine, "last_error", None), "coins": coins,
    }


@router.get("/equity")
async def get_equity(request: Request) -> list[dict]:
    sid, repo = _state(request)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    n = len(B2Params.from_dict(dict(row.params_json)).coins)
    return [{"ts_ms": ts, "equity": eq} for ts, eq in await repo.equity_series(sid, n)]


@router.get("/events")
async def get_events(request: Request, limit: int = 200) -> list[dict]:
    sid, repo = _state(request)
    return [{"ts_ms": e.ts_ms, "coin": e.coin, "kind": e.kind, "qty": e.qty, "price": e.price,
             "notional": e.notional, "fee": e.fee, "is_paper": e.is_paper, "details": e.details_json}
            for e in await repo.events(sid, limit=min(limit, 1000))]
