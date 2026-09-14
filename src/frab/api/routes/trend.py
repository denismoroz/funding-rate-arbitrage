"""Trend paper test API: summary, equity curve, fills."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from frab.db.models import Strategy
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.params import TrendParams

router = APIRouter()
HOUR_MS = 3_600_000


def _state(request: Request) -> tuple[int, TrendRepo]:
    sid = getattr(request.app.state, "trend_strategy_id", None)
    if sid is None:
        raise HTTPException(status_code=503, detail="trend engine is not running")
    return sid, TrendRepo(request.app.state.session_factory)


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    sid, repo = _state(request)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    params = TrendParams.from_dict(dict(row.params_json))
    book = await repo.get_book(sid)
    latest = await repo.latest_equity(sid)
    series = await repo.equity_series(sid)
    engine = getattr(request.app.state, "trend_engine", None)

    base = dict(status=row.status, capital=params.capital_usd, coins=list(params.coins),
                lookbacks=list(params.lookbacks), vol_target_daily=params.vol_target_daily,
                leverage_cap=params.leverage_cap, risk_scale=params.risk_scale,
                book_vol_target_ann=params.book_vol_target_ann,
                universe=list(getattr(engine, "universe", []) or []),
                last_tick_ms=getattr(engine, "last_tick_ms", None),
                last_error=getattr(engine, "last_error", None),
                hours=len(series), started=book is not None and book.started)
    if book is None or latest is None:
        return base | {"positions": []}

    prices = dict(book.prices)
    equity = latest.equity
    pnl = equity - book.capital
    hours = len(series)
    positions = []
    for coin in sorted(set(book.units) | set(book.weights)):
        price = prices.get(coin)
        units = book.units.get(coin, 0.0)
        entry = book.entry.get(coin)
        notional = units * (price or 0.0)
        positions.append({
            "coin": coin, "signal": book.signals.get(coin), "weight": book.weights.get(coin, 0.0),
            "units": units, "price": price, "entry": entry, "notional": notional,
            "unrealized": (units * (price - entry)) if (price and entry) else 0.0,
            "side": "long" if units > 0 else "short" if units < 0 else "flat",
        })
    return base | {
        "equity": equity, "pnl": pnl, "pnl_pct": pnl / book.capital * 100 if book.capital else None,
        "apr_pct": (pnl / book.capital * 100) * 8760 / hours if book.capital and hours >= 24 else None,
        "cash": latest.cash, "unrealized": latest.unrealized, "gross_notional": latest.gross_notional,
        "net_notional": latest.net_notional,
        "gross_leverage": latest.gross_notional / equity if equity else None,
        "net_leverage": latest.net_notional / equity if equity else None,
        "legs": latest.legs, "funding_total": book.funding_total, "fees": book.fees,
        "realized": book.realized, "trades": book.trades, "rebalances": book.rebalances,
        "liquidations": book.liquidations, "skipped_min_order": book.skipped_min_order,
        "size_scale": book.size_scale,
        "last_rebalance_ms": book.last_rebalance_ms, "last_bar_ms": book.last_bar_ms,
        "positions": positions,
    }


@router.get("/equity")
async def get_equity(request: Request) -> list[dict]:
    sid, repo = _state(request)
    return [{"ts_ms": ts, "equity": eq} for ts, eq in await repo.equity_series(sid)]


@router.get("/events")
async def get_events(request: Request, limit: int = 200) -> list[dict]:
    sid, repo = _state(request)
    rows = await repo.events(sid, limit=limit)
    return [{"ts_ms": e.ts_ms, "coin": e.coin, "kind": e.kind, "qty": e.qty, "price": e.price,
             "notional": e.notional, "fee": e.fee, "details": e.details_json} for e in rows]
