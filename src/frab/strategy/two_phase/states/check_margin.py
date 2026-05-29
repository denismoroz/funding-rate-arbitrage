"""CheckMarginState — verifies spot wallet balance before opening a position."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from frab.domain import FarbPosition, FarbState
from frab.events.bus import Event, EventBus
from frab.exchanges.protocol import Exchange, WalletKind
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states.base import State

logger = logging.getLogger(__name__)


class CheckMarginState(State):
    def __init__(
        self,
        *,
        exchange: Exchange,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
        event_bus: EventBus | None = None,
    ) -> None:
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._params = params
        self._bus = event_bus

    async def _publish(
        self,
        *,
        level: str,
        kind: str,
        message: str,
        payload: dict | None = None,
    ) -> None:
        if self._bus is None:
            return
        await self._bus.publish(Event(
            ts=datetime.now(timezone.utc),
            level=level,
            source="strategy",
            kind=kind,
            message=message,
            payload_json=payload,
        ))

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        required = self._params.required_margin()
        balance = await self._exchange.get_wallet("USDC", WalletKind.SPOT)
        if balance < required:
            reason = f"insufficient_margin: need {required:.4f}, have {balance:.4f}"
            logger.warning(
                "check_margin failed farb_position_id=%s coin=%s "
                "required=%.4f available=%.4f → FAILED",
                fp.id, fp.coin, required, balance,
            )
            await self._farb_repo.mark_failed(fp.id, reason=reason)
            await self._publish(
                level="WARNING",
                kind="farb.failed",
                message=f"{fp.coin} FAILED at CHECK_MARGIN: {reason}",
                payload={
                    "farb_position_id": fp.id,
                    "coin": fp.coin,
                    "state": FarbState.CHECK_MARGIN.value,
                    "required": required,
                    "available": balance,
                    "reason": reason,
                },
            )
            return None
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.CHECK_MARGIN,
            to_state=FarbState.OPENING_MARGIN,
            state_data={**fp.state_data, "required_margin": required},
        )
        return FarbState.OPENING_MARGIN
