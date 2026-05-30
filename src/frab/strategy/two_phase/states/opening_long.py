"""OpeningLongState — opens the spot leg and advances to OPENING_SHORT."""
from __future__ import annotations

import logging

from frab.domain import FarbPosition, FarbState, Instrument, Side
from frab.exchanges.protocol import OpenRequest, WalletKind
from frab.strategy.two_phase.states._base import State, StrategyContext

logger = logging.getLogger(__name__)


class OpeningLongState(State):
    state = FarbState.OPENING_LONG

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._params = ctx.params
        self._settings = ctx.settings

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        quote = await self._exchange.get_quote(fp.coin)
        price = quote.spot if quote.spot is not None else quote.mark
        size_usdc = self._params.compute_size_for(fp.coin, self._settings)
        spot_qty = size_usdc / price
        req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.SPOT,
            side=Side.LONG,
            qty=spot_qty,
            farb_position_id=fp.id,
        )
        pos = await self._exchange.open_position(req)
        await self._farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=pos.id)
        # Anchor spot_qty to HL wallet balance (not just fill qty) so any pre-existing
        # dust from previous closes is absorbed into this position and matched by short.
        hl_balance = await self._exchange.get_wallet(fp.coin, WalletKind.SPOT)
        spot_qty = max(pos.qty, hl_balance)  # max guards against transient pre-settlement reads
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_LONG,
            to_state=FarbState.OPENING_SHORT,
            state_data={**fp.state_data, "spot_qty": spot_qty, "spot_entry_price": pos.entry_price},
        )
        return FarbState.OPENING_SHORT
