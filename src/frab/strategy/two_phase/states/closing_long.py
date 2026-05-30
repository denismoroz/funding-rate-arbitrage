"""ClosingLongState — closes the spot leg and advances to RELEASING_MARGIN."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states._helpers import load_position, publish_event

logger = logging.getLogger(__name__)


class ClosingLongState(State):
    state = FarbState.CLOSING_LONG

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._sf = ctx.session_factory
        self._bus = ctx.event_bus

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        if fp.spot_position_id is None:
            raise RuntimeError(f"FarbPosition {fp.id} has no spot_position_id in CLOSING_LONG")
        spot_pos = await load_position(self._sf, fp.spot_position_id)
        await self._exchange.close_position(spot_pos)
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.CLOSING_LONG,
            to_state=FarbState.RELEASING_MARGIN,
        )
        await publish_event(
            self._bus,
            level="INFO",
            kind="farb.closed",
            message=f"{fp.coin} CLOSED (held {fp.state_data.get('hours_in_position', '?')}h)",
            payload={
                "farb_position_id": fp.id,
                "coin": fp.coin,
                "exit_signal_apr": fp.state_data.get("exit_signal_apr"),
                "exit_decision": fp.state_data.get("exit_decision"),
            },
        )
        return FarbState.RELEASING_MARGIN
