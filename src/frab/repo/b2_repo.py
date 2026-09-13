"""Persistence for Strategy B v2 books, fills and hourly equity."""
from __future__ import annotations

import time

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.db.models import B2Book, B2Equity, B2Event
from frab.db.session import session_scope
from frab.strategy.b2.book import CoinBook

HOUR_MS = 3_600_000


class B2Repo:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get_book(self, strategy_id: int, coin: str) -> CoinBook | None:
        async with self._sf() as s:
            row = (await s.execute(
                select(B2Book).where(B2Book.strategy_id == strategy_id, B2Book.coin == coin)
            )).scalar_one_or_none()
        return CoinBook.from_state(dict(row.state_json)) if row else None

    async def save_progress(self, strategy_id: int, book: CoinBook, events: list[dict],
                            equity_rows: list[dict]) -> None:
        """Book state, its fills and its equity snapshots in ONE transaction, so a crash can
        never leave a book advanced without its history (or history without the book)."""
        now = int(time.time() * 1000)
        async with session_scope(self._sf) as s:
            row = (await s.execute(
                select(B2Book).where(B2Book.strategy_id == strategy_id, B2Book.coin == book.coin)
            )).scalar_one_or_none()
            if row is None:
                s.add(B2Book(strategy_id=strategy_id, coin=book.coin, state_json=book.to_state(),
                             last_bar_ms=book.last_bar_ms, updated_at_ms=now))
            else:
                row.state_json = book.to_state()
                row.last_bar_ms = book.last_bar_ms
                row.updated_at_ms = now
            for e in events:
                details = {k: v for k, v in e.items() if k not in ("kind", "qty", "price", "notional", "fee", "bar_ms")}
                s.add(B2Event(strategy_id=strategy_id, ts_ms=int(e["bar_ms"]) + HOUR_MS, coin=book.coin,
                              kind=e["kind"], qty=float(e["qty"]), price=float(e["price"]),
                              notional=float(e["notional"]), fee=float(e["fee"]), is_paper=True,
                              details_json=details or None))
            for r in equity_rows:
                s.add(B2Equity(strategy_id=strategy_id, coin=book.coin, **r))

    async def books(self, strategy_id: int) -> list[CoinBook]:
        async with self._sf() as s:
            rows = (await s.execute(select(B2Book).where(B2Book.strategy_id == strategy_id))).scalars().all()
        return [CoinBook.from_state(dict(r.state_json)) for r in rows]

    async def latest_equity(self, strategy_id: int) -> list[B2Equity]:
        async with self._sf() as s:
            sub = (select(B2Equity.coin, func.max(B2Equity.ts_ms).label("ts"))
                   .where(B2Equity.strategy_id == strategy_id).group_by(B2Equity.coin).subquery())
            rows = (await s.execute(
                select(B2Equity).join(sub, (B2Equity.coin == sub.c.coin) & (B2Equity.ts_ms == sub.c.ts))
                .where(B2Equity.strategy_id == strategy_id)
            )).scalars().all()
        return list(rows)

    async def equity_series(self, strategy_id: int, n_coins: int) -> list[tuple[int, float]]:
        """Portfolio equity per hour, only for hours where every coin book has a snapshot."""
        async with self._sf() as s:
            rows = (await s.execute(
                select(B2Equity.ts_ms, func.sum(B2Equity.equity), func.count(B2Equity.id))
                .where(B2Equity.strategy_id == strategy_id)
                .group_by(B2Equity.ts_ms).order_by(B2Equity.ts_ms)
            )).all()
        return [(int(ts), float(total)) for ts, total, n in rows if n == n_coins]

    async def events(self, strategy_id: int, limit: int = 200) -> list[B2Event]:
        async with self._sf() as s:
            rows = (await s.execute(
                select(B2Event).where(B2Event.strategy_id == strategy_id)
                .order_by(B2Event.ts_ms.desc(), B2Event.id.desc()).limit(limit)
            )).scalars().all()
        return list(rows)

    async def reset(self, strategy_id: int) -> None:
        async with session_scope(self._sf) as s:
            for model in (B2Equity, B2Event, B2Book):
                await s.execute(delete(model).where(model.strategy_id == strategy_id))
