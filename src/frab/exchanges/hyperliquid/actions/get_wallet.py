"""GetWalletAction: fetch per-kind balance, write wallet snapshot to DB."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as DBExchange, WalletSnapshot as DBWalletSnapshot
from frab.db.session import session_scope
from frab.exchanges.hyperliquid.actions.wallet_accounting import (
    compute_non_usdc_total,
    compute_total_usdc,
)
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.protocol import WalletKind

logger = logging.getLogger(__name__)


class GetWalletAction:
    """Fetch (coin, kind) balance from HL, persist wallet snapshot, return per-kind balance."""

    def __init__(
        self,
        *,
        client: HLClient,
        symbols: HLSymbols,
        session_factory: async_sessionmaker[AsyncSession],
        exchange_name: str,
        address: str | None,
        clock_fn: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._symbols = symbols
        self._sf = session_factory
        self._exchange_name = exchange_name
        self._address = address
        self._clock_fn = clock_fn

    async def _get_exchange_id(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(DBExchange).where(DBExchange.name == self._exchange_name)
        )
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {self._exchange_name!r} not found in DB; run `frab seed` first."
            )
        return exc.id

    async def execute(self, coin: str, kind: WalletKind) -> float:
        if self._address is None:
            raise RuntimeError("account_address required")

        perp_state, spot_state = await asyncio.gather(
            self._client.user_state(self._address),
            self._client.spot_user_state(self._address),
        )

        now_ms = int(self._clock_fn().timestamp() * 1000)
        spot_coin = self._symbols.spot_token_map.get(coin, coin)

        # Per-kind balance (returned to caller)
        if kind == WalletKind.PERP:
            if coin in ("USDC", "USD"):
                balance = perp_state.account_value
            else:
                balance = 0.0
        else:  # SPOT
            balance = 0.0
            for bal in spot_state.balances:
                if bal.coin == spot_coin or bal.coin == coin:
                    balance = bal.total
                    break

        # Total balance across BOTH sub-wallets (equity-relevant cash)
        if coin in ("USDC", "USD"):
            total_balance = compute_total_usdc(
                perp_state, spot_state, spot_coin=spot_coin, raw_coin=coin
            )
        else:
            total_balance = compute_non_usdc_total(
                spot_state, spot_coin=spot_coin, raw_coin=coin
            )

        async with session_scope(self._sf) as s:
            exchange_id = await self._get_exchange_id(s)
            s.add(DBWalletSnapshot(
                exchange_id=exchange_id,
                coin=coin,
                ts_ms=now_ms,
                balance=total_balance,
                source="hl_account_total",
            ))

        return balance
