"""DB-backed Recorder — stubbed in Step 3. FarbRepo in Step 5 will replace this."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class DbRecorder:
    """Stub recorder — all write methods are no-ops until Step 5 (FarbRepo)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        strategy_id: int,
        exchange_id: int,
        mode: str = "live",
    ) -> None:
        self._session_factory = session_factory
        self._strategy_id = strategy_id
        self._exchange_id = exchange_id
        self._mode = mode

    async def prime(self) -> None:  # stub
        pass

    async def save_quote(self, quote) -> None:  # stub
        pass

    async def save_funding(self, tick) -> None:  # stub
        pass

    async def save_tick_report(self, report) -> None:  # stub
        pass

    async def save_equity(self, snapshot) -> None:  # stub
        pass

    async def record_wallet_snapshot(self, **kwargs) -> None:  # stub
        pass

    async def latest_hour_ts(self):  # stub
        return None
