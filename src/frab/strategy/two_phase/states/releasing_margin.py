"""ReleasingMarginState — closes the COLLATERAL bookkeeping row and marks the FP closed."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.states._helpers import load_position
from frab.strategy.two_phase.states.base import State

logger = logging.getLogger(__name__)


class ReleasingMarginState(State):
    def __init__(
        self,
        *,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._sf = session_factory

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        # HL cross-margin releases spot.USDC.hold automatically when the perp
        # leg closes — we only mark our COLLATERAL bookkeeping row CLOSED.
        if fp.margin_position_id is not None:
            coll_pos = await load_position(self._sf, fp.margin_position_id)
            await self._exchange.close_position(coll_pos)
        await self._farb_repo.mark_closed(fp.id)
        return None
