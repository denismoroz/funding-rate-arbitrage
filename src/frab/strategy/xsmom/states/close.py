"""CloseState — closes the perp and collateral legs, then marks the position CLOSED."""
from __future__ import annotations

import logging

from frab.domain import XsmomState
from frab.domain.xsmom_position import XsmomPosition
from frab.strategy.two_phase.states._helpers import load_position, publish_event
from frab.strategy.xsmom.states._base import State, XsmomContext

logger = logging.getLogger(__name__)


class CloseState(State):
    state = XsmomState.CLOSE

    def __init__(self, ctx: XsmomContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._repo = ctx.xsmom_repo
        self._sf = ctx.session_factory
        self._bus = ctx.event_bus

    async def execute(self, fp: XsmomPosition) -> XsmomState | None:
        # ── 1. Close perp leg ─────────────────────────────────────────────────
        if fp.perp_position_id is not None:
            perp_pos = await load_position(self._sf, fp.perp_position_id)
            await self._exchange.close_position(perp_pos)
        else:
            logger.warning(
                "close_state: XsmomPosition %s has no perp_position_id; skipping perp close",
                fp.id,
            )

        # ── 2. Release COLLATERAL bookkeeping row ─────────────────────────────
        if fp.collateral_position_id is not None:
            coll_pos = await load_position(self._sf, fp.collateral_position_id)
            await self._exchange.close_position(coll_pos)

        # ── 3. Mark closed ────────────────────────────────────────────────────
        await self._repo.mark_closed(fp.id)
        await publish_event(
            self._bus,
            level="INFO",
            kind="xsmom.closed",
            message=f"{fp.coin} CLOSED (side={fp.side.value})",
            payload={
                "xsmom_position_id": fp.id,
                "coin": fp.coin,
                "side": fp.side.value,
            },
        )
        logger.info(
            "xsmom CLOSE→CLOSED id=%s coin=%s side=%s", fp.id, fp.coin, fp.side.value
        )
        return None
