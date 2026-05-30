"""CheckMarginState — verifies spot wallet balance before opening a position."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState
from frab.exchanges.protocol import WalletKind
from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states._helpers import publish_event

logger = logging.getLogger(__name__)


class CheckMarginState(State):
    state = FarbState.CHECK_MARGIN

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._params = ctx.params
        self._settings = ctx.settings
        self._bus = ctx.event_bus

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        required = self._params.compute_required_margin_for(fp.coin, self._settings)
        balance = await self._exchange.get_wallet("USDC", WalletKind.SPOT)
        if balance < required:
            reason = f"insufficient_margin: need {required:.4f}, have {balance:.4f}"
            logger.warning(
                "check_margin failed farb_position_id=%s coin=%s "
                "required=%.4f available=%.4f → FAILED",
                fp.id, fp.coin, required, balance,
            )
            await self._farb_repo.mark_failed(fp.id, reason=reason)
            await publish_event(
                self._bus,
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
