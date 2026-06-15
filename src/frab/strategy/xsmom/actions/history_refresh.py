"""XsmomHistoryRefresh — fetches daily candles for the universe and upserts into the DB.

Called by the orchestrator before each scan so signal.compute_scores sees fresh data.
A single coin's fetch failure is logged as a warning and does NOT abort the rest.
"""
from __future__ import annotations

import logging

from frab.exchanges.protocol import Exchange
from frab.repo.xsmom_repo import XsmomRepo
from frab.strategy.xsmom.params import XsmomParams

logger = logging.getLogger(__name__)


class XsmomHistoryRefresh:
    """Fetches and stores daily OHLC (close) data for the configured universe."""

    def __init__(
        self,
        *,
        exchange: Exchange,
        xsmom_repo: XsmomRepo,
        params: XsmomParams,
    ) -> None:
        self._exchange = exchange
        self._repo = xsmom_repo
        self._params = params

    async def refresh(self, *, days: int = 90) -> None:
        """Fetch ``days`` of daily candles for each universe coin and upsert closes.

        Each coin is fetched independently; a failure on one coin is swallowed so the
        rest of the universe is always updated.
        """
        rows: list[tuple[str, int, float]] = []
        for coin in self._params.universe:
            try:
                candles = await self._exchange.get_daily_candles(coin, days)
                for day_ms, close in candles:
                    rows.append((coin, day_ms, close))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "xsmom history_refresh: fetch failed coin=%s: %s — skipping",
                    coin, exc,
                )
        if rows:
            await self._repo.upsert_daily_prices(rows)
            logger.info(
                "xsmom history_refresh: upserted %d price rows for %d coins",
                len(rows), len(self._params.universe),
            )
