"""LoadAccruedFundingAction: pull HL userFunding for a position, idempotently persist accruals, return cumulative sum."""
from __future__ import annotations

from sqlalchemy import select

from frab.db.models import FundingAccrual as DBFundingAccrual
from frab.db.session import session_scope
from frab.domain import Position
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext


class LoadAccruedFundingAction(HLAction):
    requires_session = True

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._sf = ctx.session_factory
        self._address = ctx.address

    async def execute(self, pos: Position) -> float:
        if self._address is None:
            raise RuntimeError("account_address required")
        if pos.id is None:
            raise ValueError("Position must have a DB id to fetch accrued funding")

        since_ms = int(pos.opened_at.timestamp() * 1000)
        deltas = await self._client.user_funding(self._address, since_ms)

        new_accruals: list[tuple[int, float]] = [
            (d.ts_ms, d.amount_usdc) for d in deltas if d.coin == pos.coin
        ]

        async with session_scope(self._sf) as s:
            existing_ts = {
                ts for (ts,) in (await s.execute(
                    select(DBFundingAccrual.ts_ms).where(
                        DBFundingAccrual.position_id == pos.id
                    )
                )).all()
            }
            for ts_ms, amount in new_accruals:
                if ts_ms not in existing_ts:
                    s.add(DBFundingAccrual(
                        position_id=pos.id, ts_ms=ts_ms, amount=amount,
                    ))

        async with session_scope(self._sf) as s:
            return sum(amt for (amt,) in (await s.execute(
                select(DBFundingAccrual.amount).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )).all())
