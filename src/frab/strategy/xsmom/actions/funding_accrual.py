"""XsmomFundingAccrual — refreshes funding accruals for OPENED XsmomPositions.

Mirrors frab.strategy.two_phase.actions.funding_accrual.FundingAccrual but
reads XsmomRepo.list_active instead of FarbRepo.list_active and uses
``xsmom_position_id`` in log messages.

Full sweep from pos.opened_at is done on first call and once every 24 h to
repair any gaps or corrections; all other calls are incremental.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.exchanges.protocol import Exchange
from frab.repo.xsmom_repo import XsmomRepo
from frab.strategy.two_phase.states._helpers import load_position

logger = logging.getLogger(__name__)

_FULL_SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000


class XsmomFundingAccrual:
    """Refreshes funding accruals from the exchange for each OPENED XsmomPosition."""

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        xsmom_repo: XsmomRepo,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._strategy_id = strategy_id
        self._exchange = exchange
        self._repo = xsmom_repo
        self._sf = session_factory
        self._last_full_sweep_ms: int | None = None

    async def accrue(self, *, now_ms: int) -> None:
        """For each OPENED XsmomPosition, refresh funding accruals from HL.

        Uses Exchange.get_accrued_funding which is idempotent (dedupes by
        (position_id, ts_ms) before insert) and returns the cumulative DB sum.
        We mirror that sum into state_data.

        First call after process start is always a full sweep; subsequent calls
        within 24 h are incremental; full sweep repeats every 24 h.
        """
        full = (
            self._last_full_sweep_ms is None
            or (now_ms - self._last_full_sweep_ms) >= _FULL_SWEEP_INTERVAL_MS
        )

        open_fps = await self._repo.list_active(self._strategy_id)
        for fp in open_fps:
            if fp.perp_position_id is None:
                continue
            perp_pos = await load_position(self._sf, fp.perp_position_id)
            try:
                gross = await self._exchange.get_accrued_funding(perp_pos, full=full)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "xsmom accrue_funding: get_accrued_funding failed id=%s coin=%s: %s",
                    fp.id, fp.coin, exc,
                )
                continue

            sd = dict(fp.state_data)
            sd["gross_funding_so_far"] = float(gross)
            await self._repo.update_state_data(fp.id, sd)

            logger.info(
                "xsmom funding accrued id=%s coin=%s gross_from_HL=%.6f full=%s",
                fp.id, fp.coin, gross, full,
            )

        if full:
            self._last_full_sweep_ms = now_ms
