"""RollbackAction — best-effort cleanup of partially-opened FarbPositions."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState
from frab.exchanges.protocol import Exchange, WalletKind
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._helpers import load_position


class RollbackAction:
    """Best-effort cleanup of partially-opened positions. Does NOT re-raise."""

    def __init__(
        self,
        *,
        exchange: Exchange,
        session_factory: async_sessionmaker[AsyncSession],
        params: TwoPhaseParams,
    ) -> None:
        self._exchange = exchange
        self._sf = session_factory
        self._params = params

    async def execute(self, fp: FarbPosition, *, partial_state: FarbState, error: Exception) -> None:
        """Best-effort cleanup of partially-opened positions.

        Does NOT re-raise. Logs all inner failures at ERROR level.
        """
        _pkg.logger.info(
            "rollback starting farb_position_id=%s partial_state=%s error=%s",
            fp.id,
            partial_state.value,
            error,
        )
        try:
            if partial_state == FarbState.OPENING_SHORT:
                # Spot leg is open; close it
                if fp.spot_position_id is not None:
                    try:
                        spot_pos = await load_position(self._sf, fp.spot_position_id)
                        await self._exchange.close_position(spot_pos)
                        _pkg.logger.info(
                            "rollback: closed spot leg farb_position_id=%s spot_position_id=%s",
                            fp.id,
                            fp.spot_position_id,
                        )
                    except Exception as inner:  # noqa: BLE001
                        _pkg.logger.error(
                            "rollback: failed to close spot leg farb_position_id=%s: %s",
                            fp.id,
                            inner,
                        )

            elif partial_state == FarbState.OPENING_LONG:
                # Margin is reserved; transfer it back to spot
                required = fp.state_data.get("required_margin", 0.0)
                try:
                    await self._exchange.transfer("USDC", required, WalletKind.PERP, WalletKind.SPOT)
                    _pkg.logger.info(
                        "rollback: returned margin farb_position_id=%s amount=%.4f",
                        fp.id,
                        required,
                    )
                except Exception as inner:  # noqa: BLE001
                    _pkg.logger.error(
                        "rollback: failed to return margin farb_position_id=%s: %s",
                        fp.id,
                        inner,
                    )

            elif partial_state in (FarbState.CLOSING_LONG, FarbState.CLOSING_SHORT):
                # Close-side failure: log for human/oncall, do NOT auto-reopen
                _pkg.logger.error(
                    "rollback: close-side failure farb_position_id=%s state=%s — "
                    "manual intervention required, NOT auto-reopening",
                    fp.id,
                    partial_state.value,
                )

        except Exception as outer:  # noqa: BLE001
            _pkg.logger.error(
                "rollback: unexpected error farb_position_id=%s: %s",
                fp.id,
                outer,
            )
