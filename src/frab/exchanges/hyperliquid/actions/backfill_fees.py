"""BackfillFeesAction: fill in zero-fee DB rows from HL userFills."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import (
    FarbPosition as DBFarbPosition,
    Fill as DBFill,
    Position as DBPosition,
)
from frab.db.session import session_scope
from frab.domain import Instrument, Side
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import SPOT_TOKEN_INVERSE
from frab.exchanges.hyperliquid.wire import HLUserFill

logger = logging.getLogger(__name__)


class BackfillFeesAction:
    """For all DB fills with fee==0.0 belonging to a strategy, look up
    the real fee from HL userFillsByTime and update the row."""

    def __init__(
        self,
        *,
        client: HLClient,
        session_factory: async_sessionmaker[AsyncSession],
        address: str | None,
    ) -> None:
        self._client = client
        self._sf = session_factory
        self._address = address

    async def execute(self, strategy_id: int) -> int:
        if self._address is None:
            raise RuntimeError("account_address required")

        # 1. Query zero-fee fills joined to position + farb_position
        async with session_scope(self._sf) as s:
            result = await s.execute(
                select(DBFill, DBPosition).join(
                    DBPosition, DBFill.position_id == DBPosition.id
                ).join(
                    DBFarbPosition, DBPosition.farb_position_id == DBFarbPosition.id
                ).where(
                    DBFarbPosition.strategy_id == strategy_id,
                    DBFill.fee == 0.0,
                )
            )
            zero_fee_fills = result.all()

        if not zero_fee_fills:
            return 0

        # 2. Single HL fetch for the whole window
        min_ts = min(f.ts_ms for f, _ in zero_fee_fills) - 60_000
        try:
            hl_fills = await self._client.user_fills_by_time(self._address, min_ts)
        except Exception as exc:
            logger.warning("backfill_fill_fees: user_fills_by_time failed: %s", exc)
            return 0

        # 3. For each zero-fee row, find matching HL fill, compute fee_usdc, write back
        updated = 0
        for fill_row, pos_row in zero_fee_fills:
            match = self._match_hl_fill(hl_fills, fill_row, pos_row)
            if match is None:
                continue
            fee_usdc = self._compute_fee_usdc(match)
            if fee_usdc <= 0:
                continue
            async with session_scope(self._sf) as s:
                row = await s.get(DBFill, fill_row.id)
                if row is not None:
                    row.fee = fee_usdc
                    updated += 1
            logger.info(
                "backfill_fill_fees: fill_id=%d coin=%s qty=%s → fee=%.6f USDC",
                fill_row.id, pos_row.coin, fill_row.qty, fee_usdc,
            )
        return updated

    @staticmethod
    def _match_hl_fill(
        hl_fills: list[HLUserFill],
        db_fill: Any,
        db_pos: Any,
    ) -> HLUserFill | None:
        """Find HL fill matching DB (fill, position) by side / qty / time / coin."""
        want_side = "B" if db_fill.side == Side.LONG.value else "A"
        for f in hl_fills or []:
            if f.side != want_side:
                continue
            if abs(f.sz - db_fill.qty) > max(db_fill.qty * 0.01, 1e-9):
                continue
            if abs(f.ts_ms - db_fill.ts_ms) > 30_000:
                continue
            if db_pos.instrument == Instrument.PERP.value:
                if f.coin != db_pos.coin:
                    continue
            else:  # SPOT — accept either symbolic or @-prefixed form
                if not (f.coin.startswith("@") or "/" in f.coin):
                    continue
            return f
        return None

    @staticmethod
    def _compute_fee_usdc(match: HLUserFill) -> float:
        """Convert HL raw fee (in fee_token) to USDC."""
        if match.fee_token in ("USDC", "USD"):
            return match.fee_raw
        if match.fee_token in SPOT_TOKEN_INVERSE:
            return match.fee_raw * match.px
        return match.fee_raw
