"""ClosingLongState — closes the spot leg and advances to RELEASING_MARGIN."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.states._helpers import load_position, publish_event
from frab.strategy.two_phase.states.base import State

logger = logging.getLogger(__name__)


class ClosingLongState(State):
    def __init__(
        self,
        *,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus | None = None,
    ) -> None:
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._sf = session_factory
        self._bus = event_bus

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
