"""ClosingShortState — closes the perp short leg and advances to CLOSING_LONG."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states._helpers import load_position

logger = logging.getLogger(__name__)


class ClosingShortState(State):
    state = FarbState.CLOSING_SHORT

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._sf = ctx.session_factory

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
