"""Trend paper test API: summary, equity curve, fills."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from frab.db.models import Strategy
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.params import TrendParams
from frab.strategy.trend.retune import RetuneState, sharpe
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_params import RetuneParams

router = APIRouter()
HOUR_MS = 3_600_000


RETUNE_VARIANT = "trend_retune"


def _state(request: Request, variant: str = "trend") -> tuple[int, TrendRepo]:
    """`variant` picks the trend paper strategy: the hand-listed `trend` (default) or the re-tuned one."""
    if variant == RETUNE_VARIANT:
        sid = getattr(request.app.state, "trend_retune_strategy_id", None)
        if sid is None:
            raise HTTPException(status_code=503, detail="trend_retune engine is not running")
        return sid, TrendRepo(request.app.state.session_factory, RetuneBook)
    if variant != "trend":
        raise HTTPException(status_code=404, detail=f"unknown trend variant {variant!r}")
    sid = getattr(request.app.state, "trend_strategy_id", None)
    if sid is None:
        raise HTTPException(status_code=503, detail="trend engine is not running")
    return sid, TrendRepo(request.app.state.session_factory)


def _retune_block(book, params: RetuneParams) -> dict:
    """Selector state for the dashboard: who is chosen and how every candidate has done so far."""
    st = RetuneState.from_dict(getattr(book, "retune", None), params)
    cands = []
    for name, c in st.candidates.items():
        rets = [r for _, r in c.returns]
        sh = sharpe(rets)
        cands.append({"name": name, "universe": c.universe, "lookbacks": c.lookbacks, "days": len(rets),
                      "sharpe": None if sh != sh else sh,       # NaN -> null
                      "first_active_ms": c.first_active_ms,
                      "gross": sum(abs(w) for w in c.weights.values())})
    snap = st.cap_snapshot or {}
    return {"chosen": st.chosen, "chosen_score": st.chosen_score, "refits": st.refits,
            "last_refit_ms": st.last_refit_ms, "last_sim_bar_ms": st.last_bar_ms,
            "cap_snapshot_week": snap.get("week"), "cap_snapshot_ms": snap.get("taken_ms"),
            "candidates": cands}


@router.get("/summary")
async def get_summary(request: Request, variant: str = "trend") -> dict:
    sid, repo = _state(request, variant)
    async with request.app.state.session_factory() as s:
        row = await s.get(Strategy, sid)
    retune_params = RetuneParams.from_dict(dict(row.params_json)) if variant == RETUNE_VARIANT else None
    params = retune_params.trend if retune_params else TrendParams.from_dict(dict(row.params_json))
    book = await repo.get_book(sid)
    latest = await repo.latest_equity(sid)
    series = await repo.equity_series(sid)
    engine = getattr(request.app.state, "trend_retune_engine" if retune_params else "trend_engine", None)

    base = dict(status=row.status, capital=params.capital_usd, coins=list(params.coins),
                lookbacks=list(params.lookbacks), vol_target_daily=params.vol_target_daily,
                leverage_cap=params.leverage_cap, risk_scale=params.risk_scale,
                book_vol_target_ann=params.book_vol_target_ann,
                universe=list(getattr(engine, "universe", []) or []),
                last_tick_ms=getattr(engine, "last_tick_ms", None),
                last_error=getattr(engine, "last_error", None),
                hours=len(series), started=book is not None and book.started)
    if retune_params:
        base["coins"] = []                       # the universe is chosen by code every day
        base["retune"] = _retune_block(book, retune_params) if book is not None else None
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
async def get_equity(request: Request, variant: str = "trend") -> list[dict]:
    sid, repo = _state(request, variant)
    return [{"ts_ms": ts, "equity": eq} for ts, eq in await repo.equity_series(sid)]


@router.get("/events")
async def get_events(request: Request, limit: int = 200, variant: str = "trend") -> list[dict]:
    sid, repo = _state(request, variant)
    rows = await repo.events(sid, limit=limit)
    return [{"ts_ms": e.ts_ms, "coin": e.coin, "kind": e.kind, "qty": e.qty, "price": e.price,
             "notional": e.notional, "fee": e.fee, "details": e.details_json} for e in rows]


@router.get("/retunes")
async def get_retunes(request: Request, limit: int = 200) -> list[dict]:
    """Every weekly re-tune of the trend_retune variant: the winner and all candidates' scores."""
    sid, repo = _state(request, RETUNE_VARIANT)
    rows = await repo.events(sid, limit=limit, kinds=("retune",))
    return [{"ts_ms": e.ts_ms, "details": e.details_json} for e in rows[:limit]]
