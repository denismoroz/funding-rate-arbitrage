"""AccountSnapshotAction: read account/wallet state for the API and equity views."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from frab.exchanges.hyperliquid.actions._base import HLAction, HLActionContext
from frab.exchanges.hyperliquid.wire import HLPerpState, HLSpotState

logger = logging.getLogger(__name__)


class AccountSnapshotAction(HLAction):
    """Read-side projection of HL account state for API + equity consumers."""

    requires_session = False

    def __init__(self, ctx: HLActionContext) -> None:
        super().__init__(ctx)
        self._client = ctx.client
        self._symbols = ctx.symbols
        self._address = ctx.address

    async def get_snapshot(self) -> tuple[HLPerpState, HLSpotState]:
        """Fetch typed perp + spot account state in parallel."""
        if self._address is None:
            raise RuntimeError("account_address required")
        perp_state, spot_state = await asyncio.gather(
            self._client.user_state(self._address),
            self._client.spot_user_state(self._address),
        )
        return perp_state, spot_state

    async def get_wallet_state(
        self,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Normalized wallet snapshot for the /api/equity/wallet endpoint."""
        perp_state, spot_state = await self.get_snapshot()

        perp_account_value = perp_state.account_value
        perp_unrealized_pnl = sum(ap.unrealized_pnl for ap in perp_state.asset_positions)

        prices = mark_prices or {}
        spot_balances: list[dict[str, Any]] = []
        usdc_spot = 0.0

        for bal in spot_state.balances:
            total = bal.total
            if total <= 0:
                continue
            canonical = self._symbols.normalize_spot_coin(bal.coin)
            if canonical in ("USDC", "USD"):
                usdc_spot += total
                continue
            mark = prices.get(canonical, 0.0)
            spot_balances.append({
                "coin": canonical,
                "qty": total,
                "mark": mark,
                "usd_value": total * mark,
            })

        spot_tokens_usd = sum(b["usd_value"] for b in spot_balances)
        total_usd = perp_account_value + spot_tokens_usd + usdc_spot

        return {
            "perp_account_value": perp_account_value,
            "perp_unrealized_pnl": perp_unrealized_pnl,
            "spot_balances": spot_balances,
            "usdc_spot": usdc_spot,
            "total_usd": total_usd,
        }

    async def get_perp_unrealized_by_coin(self) -> dict[str, float]:
        """{coin: unrealizedPnl_USDC} from HL assetPositions."""
        if self._address is None:
            raise RuntimeError("account_address required")
        try:
            state = await self._client.user_state(self._address)
        except Exception as exc:
            logger.warning("get_perp_unrealized_by_coin: user_state failed: %s", exc)
            return {}
        return {ap.coin: ap.unrealized_pnl for ap in state.asset_positions if ap.coin}
