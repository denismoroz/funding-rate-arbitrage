"""FundingAccrual — refreshes funding accruals for OPEN FarbPositions."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.states._helpers import load_position

# Full sweep from pos.opened_at is done on first call and once every 24 h to
# repair any gaps or corrections; all other calls are incremental.
_FULL_SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000


class FundingAccrual:
    """Refreshes funding accruals from the exchange for each OPEN FarbPosition."""

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
        signal_computer: SignalComputer,
    ) -> None:
        self._strategy_id = strategy_id
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._sf = session_factory
        self._signal_computer = signal_computer
        self._last_full_sweep_ms: int | None = None

    async def accrue(self, *, now_ms: int) -> None:
        """For each OPEN FP, refresh funding accruals from HL's authoritative
        userFunding endpoint via Exchange.get_accrued_funding. That helper is
        already idempotent (dedupes by (position_id, ts_ms) before insert) and
        returns the cumulative DB sum. We just mirror that sum into state_data
        and refresh the cached current smoothed signal.

        First call after process start is always a full sweep (heals gaps on
        restart); subsequent calls within 24 h are incremental; full sweep
        repeats every 24 h.
        """
        full = (
            self._last_full_sweep_ms is None
            or (now_ms - self._last_full_sweep_ms) >= _FULL_SWEEP_INTERVAL_MS
        )

        open_fps = await self._farb_repo.list_open(self._strategy_id)
        for fp in open_fps:
            if fp.perp_position_id is None:
                continue
            perp_pos = await load_position(self._sf, fp.perp_position_id)
            try:
                gross = await self._exchange.get_accrued_funding(perp_pos, full=full)
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.warning(
                    "accrue_funding: get_accrued_funding failed fp=%s coin=%s: %s",
                    fp.id, fp.coin, exc,
                )
                continue

            sd = dict(fp.state_data)
            sd["gross_funding_so_far"] = float(gross)
            smoothed = await self._signal_computer.compute(fp.coin)
            if smoothed is not None:
                sd["current_signal_apr"] = smoothed
            await self._farb_repo.update_state_data(fp.id, sd)

            _pkg.logger.info(
                "funding accrued fp=%s coin=%s gross_from_HL=%.6f full=%s",
                fp.id, fp.coin, gross, full,
            )

        if full:
            self._last_full_sweep_ms = now_ms
