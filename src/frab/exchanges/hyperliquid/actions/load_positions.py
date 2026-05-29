"""LoadOpenPositionsAction: read DB OPEN positions, reconcile against HL live state."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as DBExchange, Position as DBPosition
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols

logger = logging.getLogger(__name__)


class LoadOpenPositionsAction:
    def __init__(
        self,
        *,
        client: HLClient,
        symbols: HLSymbols,
        session_factory: async_sessionmaker[AsyncSession],
        exchange_name: str,
        address: str | None,
    ) -> None:
        self._client = client
        self._symbols = symbols
        self._sf = session_factory
        self._exchange_name = exchange_name
        self._address = address

    async def execute(self) -> list[Position]:
        if self._address is None:
            raise RuntimeError("account_address required")

        perp_state, spot_state = await asyncio.gather(
            self._client.user_state(self._address),
            self._client.spot_user_state(self._address),
        )

        hl_perp_coins: set[str] = {
            ap.coin for ap in perp_state.asset_positions if abs(ap.szi) > 1e-12
        }
        hl_spot_coins: set[str] = {
            bal.coin for bal in spot_state.balances if bal.total > 1e-12
        }

        async with session_scope(self._sf) as s:
            exc_row = (await s.execute(
                select(DBExchange).where(DBExchange.name == self._exchange_name)
            )).scalar_one_or_none()
            if exc_row is None:
                raise RuntimeError(
                    f"Exchange {self._exchange_name!r} not found in DB; run `frab seed` first."
                )
            db_rows = (await s.execute(
                select(DBPosition).where(
                    DBPosition.exchange_id == exc_row.id,
                    DBPosition.status == PositionStatus.OPEN.value,
                )
            )).scalars().all()

        positions: list[Position] = []
        for row in db_rows:
            if row.instrument == Instrument.PERP.value and row.coin not in hl_perp_coins:
                logger.warning(
                    "get_open_positions: DB has OPEN PERP %s but HL reports no position",
                    row.coin,
                )
            spot_hl_coin = self._symbols.spot_token_map.get(row.coin, row.coin)
            if row.instrument == Instrument.SPOT.value and spot_hl_coin not in hl_spot_coins:
                logger.warning(
                    "get_open_positions: DB has OPEN SPOT %s but HL reports no balance",
                    row.coin,
                )
            positions.append(_row_to_domain(row, exchange_name=self._exchange_name))

        return positions


def _row_to_domain(row: DBPosition, *, exchange_name: str) -> Position:
    return Position(
        id=row.id, exchange_name=exchange_name, coin=row.coin,
        instrument=Instrument(row.instrument), side=Side(row.side),
        qty=row.qty, entry_price=row.entry_price,
        opened_at=datetime.fromtimestamp(row.opened_at / 1000, tz=UTC),
        closed_at=datetime.fromtimestamp(row.closed_at / 1000, tz=UTC) if row.closed_at is not None else None,
        status=PositionStatus(row.status), farb_position_id=row.farb_position_id,
    )
