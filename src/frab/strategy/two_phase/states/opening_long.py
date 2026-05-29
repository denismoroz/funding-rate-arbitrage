"""OpeningLongState — opens the spot leg and advances to OPENING_SHORT."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState, Instrument, Side
from frab.exchanges.protocol import Exchange, OpenRequest
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states.base import State

logger = logging.getLogger(__name__)


class OpeningLongState(State):
    def __init__(
        self,
        *,
        exchange: Exchange,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
    ) -> None:
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._params = params

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        quote = await self._exchange.get_quote(fp.coin)
        price = quote.spot if quote.spot is not None else quote.mark
        spot_qty = self._params.position_size_usdc / price
        req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.SPOT,
            side=Side.LONG,
            qty=spot_qty,
            farb_position_id=fp.id,
        )
        pos = await self._exchange.open_position(req)
        await self._farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=pos.id)
        # Use the actual filled qty (pos.qty) for the perp short so spot and
        # perp legs match in size after HL's szDecimals flooring + partial fills.
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_LONG,
            to_state=FarbState.OPENING_SHORT,
            state_data={**fp.state_data, "spot_qty": pos.qty, "spot_entry_price": pos.entry_price},
        )
        return FarbState.OPENING_SHORT
