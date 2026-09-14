"""Persistence for the trend paper book, its fills and its hourly equity."""
from __future__ import annotations

import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.db.models import TrendBookRow, TrendEquity, TrendEvent
from frab.db.session import session_scope
from frab.strategy.trend.book import TrendBook

HOUR_MS = 3_600_000


class TrendRepo:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get_book(self, strategy_id: int) -> TrendBook | None:
        async with self._sf() as s:
            row = (await s.execute(
                select(TrendBookRow).where(TrendBookRow.strategy_id == strategy_id)
            )).scalar_one_or_none()
        return TrendBook.from_state(dict(row.state_json)) if row else None

    async def save_progress(self, strategy_id: int, book: TrendBook, events: list[dict],
                            equity_rows: list[dict]) -> None:
        """Book state, fills and equity snapshots in ONE transaction: a crash can never leave the book
        advanced without its history."""
        now = int(time.time() * 1000)
        async with session_scope(self._sf) as s:
            row = (await s.execute(
                select(TrendBookRow).where(TrendBookRow.strategy_id == strategy_id)
            )).scalar_one_or_none()
            if row is None:
                s.add(TrendBookRow(strategy_id=strategy_id, state_json=book.to_state(),
                                   last_bar_ms=book.last_bar_ms, updated_at_ms=now))
            else:
                row.state_json = book.to_state()
                row.last_bar_ms = book.last_bar_ms
                row.updated_at_ms = now
            for e in events:
                details = {k: v for k, v in e.items()
                           if k not in ("kind", "coin", "qty", "price", "notional", "fee", "bar_ms")}
                s.add(TrendEvent(strategy_id=strategy_id, ts_ms=int(e["bar_ms"]) + HOUR_MS, coin=e["coin"],
                                 kind=e["kind"], qty=float(e["qty"]), price=float(e["price"]),
                                 notional=float(e["notional"]), fee=float(e["fee"]), is_paper=True,
                                 details_json=details or None))
            for r in equity_rows:
                s.add(TrendEquity(strategy_id=strategy_id, **r))

    async def latest_equity(self, strategy_id: int) -> TrendEquity | None:
        async with self._sf() as s:
            return (await s.execute(
                select(TrendEquity).where(TrendEquity.strategy_id == strategy_id)
                .order_by(TrendEquity.ts_ms.desc()).limit(1)
            )).scalar_one_or_none()

    async def equity_series(self, strategy_id: int) -> list[tuple[int, float]]:
        async with self._sf() as s:
            rows = (await s.execute(
                select(TrendEquity.ts_ms, TrendEquity.equity)
                .where(TrendEquity.strategy_id == strategy_id).order_by(TrendEquity.ts_ms)
            )).all()
        return [(int(ts), float(eq)) for ts, eq in rows]

    async def events(self, strategy_id: int, limit: int = 200) -> list[TrendEvent]:
        async with self._sf() as s:
            rows = (await s.execute(
                select(TrendEvent).where(TrendEvent.strategy_id == strategy_id)
                .order_by(TrendEvent.ts_ms.desc(), TrendEvent.id.desc()).limit(limit)
            )).scalars().all()
        return list(rows)

    async def reset(self, strategy_id: int) -> None:
        async with session_scope(self._sf) as s:
            for model in (TrendEquity, TrendEvent, TrendBookRow):
                await s.execute(delete(model).where(model.strategy_id == strategy_id))
