"""LoadAccruedFundingAction: pull HL userFunding for a position, idempotently persist accruals, return cumulative sum."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.db.models import FundingAccrual as DBFundingAccrual
from frab.db.session import session_scope
from frab.domain import Position
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.wire import HLFundingDelta

_DAY_MS = 86_400_000


class LoadAccruedFundingAction(HLAction):
    requires_session = True

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._sf = ctx.session_factory
        self._address = ctx.address

    async def execute(self, pos: Position, *, full: bool = False) -> float:
        if self._address is None:
            raise RuntimeError("account_address required")
        if pos.id is None:
            raise ValueError("Position must have a DB id to fetch accrued funding")

        async with session_scope(self._sf) as s:
            existing_ts = {
                ts for (ts,) in (await s.execute(
                    select(DBFundingAccrual.ts_ms).where(
                        DBFundingAccrual.position_id == pos.id
                    )
                )).all()
            }

        last_ts = max(existing_ts) if existing_ts else None

        if full or last_ts is None:
            since_ms = int(pos.opened_at.timestamp() * 1000)
        else:
            since_ms = last_ts

        deltas = [d for d in await self._client.user_funding(self._address, since_ms)
                  if d.coin == pos.coin]
        stored = set(existing_ts)

        async with session_scope(self._sf) as s:
            for d in deltas:
                if d.n_samples is None and d.ts_ms not in stored:
                    s.add(DBFundingAccrual(
                        position_id=pos.id, ts_ms=d.ts_ms, amount=d.amount_usdc,
                    ))
                    stored.add(d.ts_ms)
            for d in deltas:
                if d.n_samples is not None:
                    await self._apply_daily_summary(s, pos.id, d, stored)

        async with session_scope(self._sf) as s:
            return sum(amt for (amt,) in (await s.execute(
                select(DBFundingAccrual.amount).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )).all())

    async def _apply_daily_summary(
        self, s: AsyncSession, position_id: int, d: HLFundingDelta, stored: set[int],
    ) -> None:
        """A daily summary replaces hourly payments HL no longer returns one by
        one (wire.HLFundingDelta). Its stamp (00:00:00.000) differs from the
        hourly ones (a few ms past the hour), so deduping by ts_ms alone stored
        it next to the hours it sums and doubled gross funding. If the day's hours are all stored, the summary is
        dropped (and a copy stored earlier is deleted); if some are missing,
        the day's rows are replaced by the summary."""
        day_end = d.ts_ms + _DAY_MS
        hours = [ts for ts in stored if d.ts_ms < ts < day_end]
        if len(hours) >= (d.n_samples or 0):
            if d.ts_ms in stored:
                await s.execute(delete(DBFundingAccrual).where(
                    DBFundingAccrual.position_id == position_id,
                    DBFundingAccrual.ts_ms == d.ts_ms,
                ))
                stored.discard(d.ts_ms)
            return
        await s.execute(delete(DBFundingAccrual).where(
            DBFundingAccrual.position_id == position_id,
            DBFundingAccrual.ts_ms >= d.ts_ms,
            DBFundingAccrual.ts_ms < day_end,
        ))
        stored.difference_update(ts for ts in list(stored) if d.ts_ms <= ts < day_end)
        s.add(DBFundingAccrual(position_id=position_id, ts_ms=d.ts_ms, amount=d.amount_usdc))
        stored.add(d.ts_ms)
