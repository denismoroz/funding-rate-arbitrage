"""NewState — end-to-end handler for XsmomState.NEW.

Folds CheckMargin + COLLATERAL lock + open perp leg into one atomic handler
(mirrors FRAB's separate CHECK_MARGIN / OPENING_MARGIN / OPENING_SHORT states
but simplified to a single NEW state because XSMOM has only one perp leg and
no spot leg).

Order of operations (rollback-safe: collateral before perp):
  1. Determine notional + required margin (from state_data if present, else compute).
  2. Check USDC wallet balance → mark_failed if insufficient.
  3. Open COLLATERAL bookkeeping row (USDC reservation).
  4. Open PERP leg (directional LONG or SHORT).
  5. Transition to OPENED with enriched state_data.

NOTE on ``farb_position_id`` reuse: the underlying ``positions`` table uses
``farb_position_id`` as its FK column for both FRAB and XSMOM positions.
We pass ``farb_position_id=fp.id`` on both OpenRequests (collateral + perp)
so the position rows are linked to the XsmomPosition.  The column name is
a legacy artifact; it is acceptable/consistent with how FRAB state handlers
populate it.
"""
from __future__ import annotations

import logging

from frab.constants import PERP_TAKER
from frab.domain import Instrument, Side, XsmomState
from frab.domain.xsmom_position import XsmomPosition
from frab.exchanges.protocol import OpenRequest, WalletKind
from frab.strategy.two_phase.states._helpers import now_ms, publish_event, load_position  # noqa: F401 — load_position re-exported for consistency
from frab.strategy.xsmom.states._base import State, XsmomContext

logger = logging.getLogger(__name__)


class NewState(State):
    state = XsmomState.NEW

    def __init__(self, ctx: XsmomContext) -> None:
        super().__init__(ctx)
        self._exchange = ctx.exchange
        self._repo = ctx.xsmom_repo
        self._params = ctx.params
        self._sf = ctx.session_factory
        self._bus = ctx.event_bus

    async def execute(self, fp: XsmomPosition) -> XsmomState | None:
        # ── 1. Determine notional + required margin ───────────────────────────
        # Phase D will inject notional/required_margin into state_data during the
        # rebalance scan (so reconcile can override sizing). Fallback: compute now.
        notional: float
        required: float

        if "notional" in fp.state_data and "required_margin" in fp.state_data:
            notional = float(fp.state_data["notional"])
            required = float(fp.state_data["required_margin"])
        else:
            if fp.target_qty is None:
                reason = f"target_qty is None on XsmomPosition {fp.id}; cannot compute notional"
                logger.error("new_state failed xsmom_position_id=%s: %s", fp.id, reason)
                await self._repo.mark_failed(fp.id, reason=reason)
                await publish_event(
                    self._bus,
                    level="ERROR",
                    kind="xsmom.failed",
                    message=f"{fp.coin} FAILED at NEW: {reason}",
                    payload={"xsmom_position_id": fp.id, "coin": fp.coin, "reason": reason},
                )
                return None

            quote = await self._exchange.get_quote(fp.coin)
            price = quote.mark if quote.spot is None else quote.spot
            notional = fp.target_qty * price
            required = self._params.compute_required_margin(notional)

        # ── 2. CheckMargin ────────────────────────────────────────────────────
        balance = await self._exchange.get_wallet("USDC", WalletKind.SPOT)
        if balance < required:
            reason = f"insufficient_margin: need {required:.4f}, have {balance:.4f}"
            logger.warning(
                "new_state failed xsmom_position_id=%s coin=%s "
                "required=%.4f available=%.4f → FAILED",
                fp.id, fp.coin, required, balance,
            )
            await self._repo.mark_failed(fp.id, reason=reason)
            await publish_event(
                self._bus,
                level="WARNING",
                kind="xsmom.failed",
                message=f"{fp.coin} FAILED at NEW (check_margin): {reason}",
                payload={
                    "xsmom_position_id": fp.id,
                    "coin": fp.coin,
                    "required": required,
                    "available": balance,
                    "reason": reason,
                },
            )
            return None

        # ── 3. COLLATERAL lock ────────────────────────────────────────────────
        # Passing farb_position_id=fp.id links the Position row back to this
        # XsmomPosition. The column name is a FRAB legacy; it is acceptable to
        # reuse it for xsmom (see module docstring).
        coll_req = OpenRequest(
            coin="USDC",
            instrument=Instrument.COLLATERAL,
            side=Side.NONE,
            qty=required,
            farb_position_id=fp.id,
        )
        coll_pos = await self._exchange.open_position(coll_req)
        await self._repo.set_leg(fp.id, collateral_position_id=coll_pos.id)

        # ── 4. Open perp leg ──────────────────────────────────────────────────
        target_qty: float = fp.target_qty  # type: ignore[assignment]  # guarded above
        rounded_qty = await self._exchange.round_qty_to_nearest(fp.coin, target_qty)
        perp_req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.PERP,
            side=fp.side,
            qty=rounded_qty,
            farb_position_id=fp.id,
            leverage=self._params.leverage,
        )
        perp_pos = await self._exchange.open_position(perp_req)
        await self._repo.set_leg(fp.id, perp_position_id=perp_pos.id)

        # ── 5. Transition to OPENED ───────────────────────────────────────────
        # Round-trip taker fee estimate: open + close, single perp leg only.
        total_fees_paid = notional * PERP_TAKER * 2
        new_state_data = {
            **fp.state_data,
            "required_margin": required,
            "notional": notional,
            "leverage": self._params.leverage,
            "gross_funding_so_far": 0.0,
            "total_fees_paid": total_fees_paid,
            "opened_at_ms": now_ms(),
        }
        await self._repo.transition(
            fp.id,
            from_state=XsmomState.NEW,
            to_state=XsmomState.OPENED,
            state_data=new_state_data,
        )
        await publish_event(
            self._bus,
            level="INFO",
            kind="xsmom.opened",
            message=(
                f"{fp.coin} OPENED: side={fp.side.value} qty={rounded_qty:.6f} "
                f"@ {perp_pos.entry_price:.4f} notional={notional:.2f}"
            ),
            payload={
                "xsmom_position_id": fp.id,
                "coin": fp.coin,
                "side": fp.side.value,
                "qty": rounded_qty,
                "entry_price": perp_pos.entry_price,
                "notional": notional,
                "required_margin": required,
                "leverage": self._params.leverage,
            },
        )
        logger.info(
            "xsmom NEW→OPENED id=%s coin=%s side=%s qty=%.6f entry=%.4f",
            fp.id, fp.coin, fp.side.value, rounded_qty, perp_pos.entry_price,
        )
        return XsmomState.OPENED
