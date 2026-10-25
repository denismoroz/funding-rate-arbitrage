"""ClosingShortState — closes the perp short leg and advances to CLOSING_LONG."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states._base import CloseBlocked, State, StrategyContext
from frab.strategy.two_phase.states._helpers import load_position, now_ms

logger = logging.getLogger(__name__)


class ClosingShortState(State):
    state = FarbState.CLOSING_SHORT

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._sf = ctx.session_factory
        self._gate = ctx.close_gate

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        if fp.perp_position_id is None:
            raise RuntimeError(f"FarbPosition {fp.id} has no perp_position_id in CLOSING_SHORT")
        # Last look before the first leg goes: the check made when the exit was admitted
        # can be a minute old, and a position already closing when the gate was deployed
        # was never checked. Blocked: nothing is closed, the next tick looks again.
        state_data = None
        if self._gate is not None:
            admitted = await self._gate.admit(
                fp, decision=str(fp.state_data.get("exit_decision", "unknown")),
                state_data=fp.state_data, now_ms=now_ms(),
            )
            if not admitted.ok:
                raise CloseBlocked(admitted.reason)
            if fp.state_data.get("close_blocked"):
                state_data = admitted.state_data
        perp_pos = await load_position(self._sf, fp.perp_position_id)
        await self._exchange.close_position(perp_pos)
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.CLOSING_SHORT,
            to_state=FarbState.CLOSING_LONG,
            state_data=state_data,
        )
        return FarbState.CLOSING_LONG
