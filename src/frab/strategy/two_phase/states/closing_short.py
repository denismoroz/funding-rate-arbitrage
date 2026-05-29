"""ClosingShortState — closes the perp short leg and advances to CLOSING_LONG."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.states._helpers import load_position
from frab.strategy.two_phase.states.base import State

logger = logging.getLogger(__name__)


class ClosingShortState(State):
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
        if fp.perp_position_id is None:
            raise RuntimeError(f"FarbPosition {fp.id} has no perp_position_id in CLOSING_SHORT")
        perp_pos = await load_position(self._sf, fp.perp_position_id)
        await self._exchange.close_position(perp_pos)
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.CLOSING_SHORT,
            to_state=FarbState.CLOSING_LONG,
        )
        return FarbState.CLOSING_LONG
