"""AccountSnapshotAction: read account/wallet state for the API and equity views."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols

logger = logging.getLogger(__name__)


class AccountSnapshotAction:
    """Read-side projection of HL account state for API + equity consumers."""

    def __init__(
        self,
        *,
        client: HLClient,
        symbols: HLSymbols,
        address: str | None,
    ) -> None:
        self._client = client
        self._symbols = symbols
        self._address = address

    async def get_state(self) -> dict[str, Any]:
        """Return raw perp + spot account state as dicts (legacy shape).

        NOTE: this re-serializes typed wire objects back into the dict form
        legacy API routes expect. Future PR: extend HLPerpAssetPosition with
        marginUsed/leverage/positionValue and migrate callers to typed objects.
        """
        if self._address is None:
            raise RuntimeError("account_address required")
        perp_state, spot_state = await asyncio.gather(
            self._client.user_state(self._address),
            self._client.spot_user_state(self._address),
        )
        # Re-serialize to match the dict shape callers (API routes) expect
        perp_dict = {
            "marginSummary": {"accountValue": str(perp_state.account_value)},
            "assetPositions": [
                {
                    "position": {
                        "coin": ap.coin,
                        "szi": str(ap.szi),
                        "unrealizedPnl": str(ap.unrealized_pnl),
                        "cumFunding": {"sinceOpen": str(ap.cum_funding_since_open)},
                    }
                }
                for ap in perp_state.asset_positions
            ],
        }
        spot_dict = {
            "balances": [
                {"coin": b.coin, "total": str(b.total), "hold": str(b.hold)}
                for b in spot_state.balances
            ]
        }
        return {"perp": perp_dict, "spot": spot_dict}

    async def get_wallet_state(
        self,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Normalized wallet snapshot for the /api/equity/wallet endpoint."""
        if self._address is None:
            raise RuntimeError("account_address required")

        raw = await self.get_state()
        perp_state = raw["perp"]
        spot_state = raw["spot"]

        margin_summary = perp_state.get("marginSummary", {})
        perp_account_value = float(margin_summary.get("accountValue", 0.0))

        perp_unrealized_pnl = sum(
            float(entry.get("position", {}).get("unrealizedPnl", 0.0))
            for entry in perp_state.get("assetPositions", [])
        )

        prices = mark_prices or {}
        spot_balances: list[dict[str, Any]] = []
        usdc_spot = 0.0

        for balance in spot_state.get("balances", []):
            hl_coin: str = balance.get("coin", "")
            total = float(balance.get("total", 0.0))
            if total <= 0:
                continue

            canonical = self._symbols.normalize_spot_coin(hl_coin)

            if canonical in ("USDC", "USD"):
                usdc_spot += total
                continue

            mark = prices.get(canonical, 0.0)
            usd_value = total * mark
            spot_balances.append({
                "coin": canonical,
                "qty": total,
                "mark": mark,
                "usd_value": usd_value,
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
