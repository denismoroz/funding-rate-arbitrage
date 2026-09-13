"""Strategy B v2 (paper) API: summary, equity curve, fills."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from frab.db.models import Strategy
from frab.repo.b2_repo import B2Repo
from frab.strategy.b2.params import B2Params

router = APIRouter()


def _state(request: Request, test: str) -> tuple[int, B2Repo]:
    """Strategy id of the paper test `test` ("b2" = main, "b2_cold" = cold wallet)."""
    tests = getattr(request.app.state, "b2_tests", None)
    if tests is None:                                   # single-test wiring (tests, older app state)
        sid = getattr(request.app.state, "b2_strategy_id", None)
        tests = {"b2": sid} if sid is not None else {}
    if not tests:
        raise HTTPException(status_code=503, detail="B2 engine is not running")
    if test not in tests:
        raise HTTPException(status_code=404, detail=f"unknown B2 test {test!r}; available: {sorted(tests)}")
    return tests[test], B2Repo(request.app.state.session_factory)


def _engine(request: Request, sid: int):
    engines = getattr(request.app.state, "b2_engines", None)
    if engines is not None:
        return engines.get(sid)
    return getattr(request.app.state, "b2_engine", None)


@router.get("/summary")
async def get_summary(request: Request, test: str = "b2") -> dict:
    sid, repo = _state(request, test)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    params = B2Params.from_dict(dict(row.params_json))
    books = {b.coin: b for b in await repo.books(sid)}
    latest = {e.coin: e for e in await repo.latest_equity(sid)}
    series = await repo.equity_series(sid, len(params.coins))
    engine = _engine(request, sid)

    coins = []
    for coin in params.coins:
        b, e = books.get(coin), latest.get(coin)
        if b is None or e is None:
            coins.append({"coin": coin, "started": False})
            continue
        liq = b.liquidation_price() if params.margin_enabled else None
        av = b.account_value(e.price)
        hedge_notional = b.hedge_units() * e.price
        carry_notional = b.carry_units * e.price
        coins.append({
            "coin": coin, "started": True, "price": e.price, "equity": e.equity, "capital": b.capital,
            "spot_target": b.position_size, "hl_reserve": b.reserve,
            "leverage": b.leverage if params.margin_enabled else None,
            "hl_account_value": av if params.margin_enabled else None,
            "margin_used": b.initial_margin(e.price) if params.margin_enabled else None,
            "free_margin": b.free_margin(e.price) if params.margin_enabled else None,
            "hedge_notional": hedge_notional, "carry_notional": carry_notional,
            "liq_price": liq, "liq_distance_pct": (liq / e.price - 1) * 100 if liq else None,
            "margin_rebalances": b.margin_rebals, "liquidations": b.liquidations,
            "hedge_limited": b.hedge_limited, "carry_blocked_hours": b.carry_blocked_hours,
            "pnl": e.equity - b.capital, "pnl_pct": (e.equity / b.capital - 1) * 100,
            "hedge_on": e.hedge_on, "carry_on": e.carry_on, "spot_value": e.spot_value,
            "short_pnl": e.short_pnl, "cash": e.cash, "carry_cash": e.carry_cash,
            "funding_on_hedge": b.funding_total, "hedge_realized": b.short_realized,
            "carry_funding": b.carry_funding,
            "fees": b.perp_fees + b.spot_fees + b.carry_fees + b.margin_fees,
            "hedges": b.trades, "rebalances": b.rebals, "carry_entries": b.carry_trades,
        })
    started = [c for c in coins if c["started"]]
    capital = params.capital_usd
    complete = len(started) == len(params.coins)
    equity = sum(c["equity"] for c in started) if complete else None
    hours = len(series)
    pnl_pct = None if equity is None else (equity / capital - 1) * 100

    def total(key: str) -> float | None:
        return sum(c[key] for c in started) if complete and params.margin_enabled else None

    short_notional = sum(c["hedge_notional"] + c["carry_notional"] for c in started)
    hl_av = total("hl_account_value")
    return {
        "test": test, "mode": params.mode, "status": row.status, "params": params.to_dict(),
        "capital": capital, "equity": equity,
        "pnl": None if equity is None else equity - capital,
        "pnl_pct": pnl_pct,
        # simple annualisation of the P&L since start: pure noise for the first days
        "apr_pct": None if pnl_pct is None or hours == 0 else pnl_pct * 8760 / hours,
        "margin_enabled": params.margin_enabled,
        "spot_value": sum(c["spot_value"] for c in started) if complete else None,
        "hl_account_value": hl_av, "margin_used": total("margin_used"), "free_margin": total("free_margin"),
        "short_notional": short_notional,
        "effective_leverage": short_notional / hl_av if hl_av else None,
        "started_ms": series[0][0] if series else None, "last_bar_ms": series[-1][0] if series else None,
        "hours": hours, "engine_last_tick_ms": getattr(engine, "last_tick_ms", None),
        "engine_last_error": getattr(engine, "last_error", None), "coins": coins,
    }


@router.get("/equity")
async def get_equity(request: Request, test: str = "b2") -> list[dict]:
    sid, repo = _state(request, test)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    n = len(B2Params.from_dict(dict(row.params_json)).coins)
    return [{"ts_ms": ts, "equity": eq} for ts, eq in await repo.equity_series(sid, n)]


@router.get("/events")
async def get_events(request: Request, limit: int = 200, test: str = "b2") -> list[dict]:
    sid, repo = _state(request, test)
    return [{"ts_ms": e.ts_ms, "coin": e.coin, "kind": e.kind, "qty": e.qty, "price": e.price,
             "notional": e.notional, "fee": e.fee, "is_paper": e.is_paper, "details": e.details_json}
            for e in await repo.events(sid, limit=min(limit, 1000))]
