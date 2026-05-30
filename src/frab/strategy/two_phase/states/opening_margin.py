"""OpeningMarginState — records COLLATERAL position and advances to OPENING_LONG."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState, Instrument, Side
from frab.exchanges.protocol import OpenRequest
from frab.strategy.two_phase.states._base import State, StrategyContext

logger = logging.getLogger(__name__)


class OpeningMarginState(State):
    state = FarbState.OPENING_MARGIN

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._params = ctx.params
        self._settings = ctx.settings

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        # HL is cross-margin on one account — no spot→perp transfer needed.
        # We still record a COLLATERAL Position row so the FP has a tracked
        # margin obligation (qty = USDC reserved; entry_price = 1.0). The
        # actual hold on spot USDC is created by HL when the perp leg opens.
        required = fp.state_data.get("required_margin", self._params.compute_required_margin_for(fp.coin, self._settings))
        coll_req = OpenRequest(
            coin="USDC",
            instrument=Instrument.COLLATERAL,
            side=Side.NONE,
            qty=required,
            farb_position_id=fp.id,
        )
        coll_pos = await self._exchange.open_position(coll_req)
        await self._farb_repo.set_leg(
            fp.id, instrument=Instrument.COLLATERAL, position_id=coll_pos.id
        )
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_MARGIN,
            to_state=FarbState.OPENING_LONG,
        )
        return FarbState.OPENING_LONG
