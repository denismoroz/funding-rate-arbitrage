"""Persistence of the `b2_retune` book: the simulation state plus the published rows, in one transaction."""
from __future__ import annotations

import time

from sqlalchemy import select

from frab.db.models import B2Book, B2Equity, B2Event
from frab.db.session import session_scope
from frab.repo.b2_repo import HOUR_MS, RETUNE_STATE_COIN, B2Repo
from frab.strategy.b2.book import CoinBook


class B2RetuneRepo(B2Repo):
    async def get_state(self, strategy_id: int) -> dict | None:
        async with self._sf() as s:
            row = (await s.execute(
                select(B2Book).where(B2Book.strategy_id == strategy_id, B2Book.coin == RETUNE_STATE_COIN)
            )).scalar_one_or_none()
        return dict(row.state_json) if row else None

    async def save_retune_progress(self, strategy_id: int, state: dict, views: dict[str, CoinBook],
                                   events: list[dict], equity_rows: list[dict]) -> None:
        """Simulation state, the published per-coin books, their fills and equity rows in ONE transaction, so a
        crash can never leave the state ahead of (or behind) what was published."""
        now = int(time.time() * 1000)
        last_bar = state.get("last_bar_ms")
        async with session_scope(self._sf) as s:
            rows = {r.coin: r for r in (await s.execute(
                select(B2Book).where(B2Book.strategy_id == strategy_id))).scalars().all()}
            payloads = {RETUNE_STATE_COIN: state} | {c: v.to_state() for c, v in views.items()}
            for coin, payload in payloads.items():
                row = rows.get(coin)
                if row is None:
                    s.add(B2Book(strategy_id=strategy_id, coin=coin, state_json=payload, last_bar_ms=last_bar,
                                 updated_at_ms=now))
                else:
                    row.state_json, row.last_bar_ms, row.updated_at_ms = payload, last_bar, now
            for e in events:
                details = {k: v for k, v in e.items()
                           if k not in ("kind", "coin", "qty", "price", "notional", "fee", "bar_ms")}
                s.add(B2Event(strategy_id=strategy_id, ts_ms=int(e["bar_ms"]) + HOUR_MS, coin=e["coin"],
                              kind=e["kind"], qty=float(e["qty"]), price=float(e["price"]),
                              notional=float(e["notional"]), fee=float(e["fee"]), is_paper=True,
                              details_json=details or None))
            for r in equity_rows:
                s.add(B2Equity(strategy_id=strategy_id, **r))
