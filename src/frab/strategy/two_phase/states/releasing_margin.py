"""ReleasingMarginState — closes the COLLATERAL bookkeeping row and marks the FP closed."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states._helpers import load_position

logger = logging.getLogger(__name__)


class ReleasingMarginState(State):
    state = FarbState.RELEASING_MARGIN

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._sf = ctx.session_factory

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        # HL cross-margin releases spot.USDC.hold automatically when the perp
        # leg closes — we only mark our COLLATERAL bookkeeping row CLOSED.
        if fp.margin_position_id is not None:
            coll_pos = await load_position(self._sf, fp.margin_position_id)
            await self._exchange.close_position(coll_pos)
        await self._farb_repo.mark_closed(fp.id)
        return None
