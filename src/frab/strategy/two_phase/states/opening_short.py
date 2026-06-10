"""OpeningShortState — opens the perp short leg and advances to PRE_BREAKEVEN."""
from __future__ import annotations

import logging

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.domain import FarbPosition, FarbState, Instrument, Side
from frab.engine.two_phase_signals import compute_position_min_hold
from frab.exchanges.protocol import OpenRequest
from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states._helpers import now_ms, publish_event

logger = logging.getLogger(__name__)


class OpeningShortState(State):
    state = FarbState.OPENING_SHORT

    def __init__(self, ctx: StrategyContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._farb_repo = ctx.farb_repo
        self._params = ctx.params
        self._settings = ctx.settings
        self._bus = ctx.event_bus

    async def execute(self, fp: FarbPosition) -> FarbState | None:
        spec = self._settings.get_coin_spec(fp.coin)
        size_usdc = self._params.compute_size_for(fp.coin, self._settings)
        spot_qty = fp.state_data.get("spot_qty")
        if spot_qty is None:
            # Fallback: recompute from current price
            quote = await self._exchange.get_quote(fp.coin)
            price = quote.spot if quote.spot is not None else quote.mark
            spot_qty = size_usdc / price

        # Round to nearest (not floor) so the perp short matches spot fill precisely:
        # for a 0.000149895 BTC spot delta, HALF_UP → 0.00015 (~$0.01 dust) vs FLOOR → 0.00014 (~$0.76 dust).
        hedge_qty = await self._exchange.round_qty_to_nearest(fp.coin, spot_qty)
        req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.PERP,
            side=Side.SHORT,
            qty=hedge_qty,
            farb_position_id=fp.id,
            leverage=spec.leverage,
        )
        pos = await self._exchange.open_position(req)
        await self._farb_repo.set_leg(fp.id, instrument=Instrument.PERP, position_id=pos.id)
        # Record two-phase dynamic state at entry
        entry_signal = fp.state_data.get("target_signal_apr", 0.0)
        pos_min_hold = compute_position_min_hold(
            entry_signal_annual=entry_signal,
            safety_mult=self._params.safety_mult,
            base_min_hold_hours=self._params.base_min_hold_hours,
            cap_min_hold_hours=self._params.cap_min_hold_hours,
        )
        # total_fees_paid: round-trip fees at entry (perp taker + spot taker, both sides)
        total_fees_paid = size_usdc * (PERP_TAKER + SPOT_TAKER) * 2
        await self._farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_SHORT,
            to_state=FarbState.PRE_BREAKEVEN,
            state_data={
                **fp.state_data,
                "position_min_hold_hours": pos_min_hold,
                "gross_funding_so_far": 0.0,
                "total_fees_paid": total_fees_paid,
                "consec_negative_hours": 0,
                "opened_at_ms": now_ms(),
                "leverage": spec.leverage,
            },
        )
        await publish_event(
            self._bus,
            level="INFO",
            kind="farb.opened",
            message=(
                f"{fp.coin} PRE_BREAKEVEN: spot={spot_qty:.6f} @ "
                f"{fp.state_data.get('spot_entry_price', 0):.2f}, "
                f"perp_short={pos.qty:.6f} @ {pos.entry_price:.2f}"
            ),
            payload={
                "farb_position_id": fp.id,
                "coin": fp.coin,
                "spot_qty": spot_qty,
                "perp_qty": pos.qty,
                "spot_entry_price": fp.state_data.get("spot_entry_price"),
                "perp_entry_price": pos.entry_price,
                "target_signal_apr": entry_signal,
                "position_min_hold_hours": pos_min_hold,
            },
        )
        return FarbState.PRE_BREAKEVEN
