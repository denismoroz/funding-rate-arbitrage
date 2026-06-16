"""TransferAction: transfer funds between HL sub-wallets, optionally persist snapshot."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.db.models import Exchange as DBExchange, WalletSnapshot as DBWalletSnapshot
from frab.db.session import session_scope
from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.actions.wallet_accounting import (
    compute_non_usdc_total,
    find_spot_balance,
)
from frab.exchanges.protocol import WalletKind

logger = logging.getLogger(__name__)


class TransferAction(HLAction):
    """Execute a USD class transfer on HL and optionally write a wallet snapshot."""

    requires_session = False

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._symbols = ctx.symbols
        self._sf = ctx.session_factory
        self._exchange_name = ctx.exchange_name
        self._address = ctx.address
        self._clock_fn = ctx.clock_fn

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

    async def execute(
        self,
        coin: str,
        amount: float,
        from_wallet: WalletKind,
        to_wallet: WalletKind,
    ) -> None:
        self._client._require_exchange()
        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount!r}")

        if from_wallet == WalletKind.SPOT and to_wallet == WalletKind.PERP:
            to_perp = True
        elif from_wallet == WalletKind.PERP and to_wallet == WalletKind.SPOT:
            to_perp = False
        else:
            raise ValueError(
                f"unsupported transfer direction: {from_wallet} → {to_wallet}"
            )

        await self._client.usd_class_transfer(amount, to_perp)
        logger.info(
            "transfer coin=%s amount=%.4f %s→%s ok",
            coin, amount, from_wallet, to_wallet,
        )

        if self._sf is None or self._address is None:
            return

        spot_state = await self._client.spot_user_state(self._address)

        spot_coin = self._symbols.spot_token_map.get(coin, coin)

        if coin in ("USDC", "USD"):
            # Unified margin: perp collateral is drawn from spot USDC, so do NOT
            # add perp_state.account_value (it double-counts; see compute_total_usdc).
            total_balance = find_spot_balance(spot_state, spot_coin=spot_coin, raw_coin=coin)[0]
        else:
            total_balance = compute_non_usdc_total(spot_state, spot_coin=spot_coin, raw_coin=coin)

        now_ms = int(self._clock_fn().timestamp() * 1000)

        async with session_scope(self._sf) as s:
            exchange_id = await self._get_exchange_id(s)
            s.add(DBWalletSnapshot(
                exchange_id=exchange_id,
                coin=coin,
                ts_ms=now_ms,
                balance=total_balance,
                source="hl_account_total",
                account=(self._address.lower() if self._address else None),
            ))
