"""LiveHLExecutor: routes real orders to Hyperliquid via hyperliquid-python-sdk.

The same coin maps to two different on-exchange names:
  - PERP leg uses bare coin name, e.g. "BTC" / "PURR".
  - SPOT leg uses a pair name, e.g. "UBTC/USDC" or "PURR/USDC".

`spot_token_map` lets callers override the spot base-token name (e.g. testnet
PURR == PURR identity; mainnet BTC → UBTC). Default is identity.

Reconcile-vs-DB cross-check lives outside this class (engine/reconcile.scan
plus a future helper); this executor's `reconcile()` is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable, Literal

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from frab.exchanges.base import (
    FillReport,
    Leg,
    OrderRequest,
    PositionState,
    Side,
)

logger = logging.getLogger(__name__)


def _base_url(network: Literal["testnet", "mainnet"]) -> str:
    if network == "testnet":
        return constants.TESTNET_API_URL
    if network == "mainnet":
        return constants.MAINNET_API_URL
    raise ValueError(f"unsupported network: {network!r}")


class PartialFillError(RuntimeError):
    """Raised when HL filled less than the requested qty beyond tolerance.

    Carries the actual fill so callers can decide to record a partial leg
    rather than discarding it entirely.
    """

    def __init__(self, requested_qty: float, filled_qty: float, fill: FillReport) -> None:
        super().__init__(
            f"partial fill: requested {requested_qty}, filled {filled_qty} "
            f"({filled_qty / requested_qty * 100:.1f}%)"
        )
        self.requested_qty = requested_qty
        self.filled_qty = filled_qty
        self.fill = fill


class LiveHLExecutor:
    name = "hyperliquid"

    def __init__(
        self,
        *,
        private_key: str | None = None,
        account_address: str | None = None,
        network: Literal["testnet", "mainnet"] = "testnet",
        info: Info | None = None,
        exchange: Exchange | None = None,
        spot_token_map: dict[str, str] | None = None,
        spot_quote_token: str = "USDC",
        slippage: float = 0.01,
        partial_fill_tolerance: float = 0.01,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if info is None:
            info = Info(_base_url(network), skip_ws=True)
        if exchange is None:
            if private_key is None or account_address is None:
                raise ValueError("private_key and account_address required when exchange is not injected")
            wallet = Account.from_key(private_key)
            exchange = Exchange(wallet=wallet, base_url=_base_url(network), account_address=account_address)

        self._info = info
        self._exchange = exchange

        if account_address is not None:
            self._address: str | None = account_address
        else:
            # Try to pull from injected exchange (only if it carries a real string attr)
            candidate = getattr(exchange, "account_address", None)
            self._address = candidate if isinstance(candidate, str) else None

        self._spot_token_map: dict[str, str] = spot_token_map if spot_token_map is not None else {}
        self._spot_quote_token = spot_quote_token
        self._slippage = slippage
        self._partial_fill_tolerance = partial_fill_tolerance
        self._clock_fn = clock_fn if clock_fn is not None else lambda: datetime.now(UTC)
        self._sz_decimals_cache: dict[str, int] | None = None

    def _make_name(self, req: OrderRequest) -> str:
        if req.leg == Leg.PERP:
            return req.coin
        # SPOT: pair name using token map
        base = self._spot_token_map.get(req.coin, req.coin)
        return f"{base}/{self._spot_quote_token}"

    async def submit(self, req: OrderRequest) -> FillReport:
        name = self._make_name(req)
        is_buy = req.side == Side.BUY

        resp = await asyncio.to_thread(
            self._exchange.market_open, name, is_buy, req.qty, None, self._slippage
        )

        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"HL order rejected: {resp!r}")
        try:
            status0 = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"HL order response shape unexpected: {resp!r}") from exc

        if "filled" in status0:
            filled = status0["filled"]
            qty = float(filled["totalSz"])
            price = float(filled["avgPx"])
            # Per-fill fee not always echoed in market_open response.
            # Default to 0.0 and let a separate reconcile pass derive it from user_fills later.
            fee = float(filled.get("fee", 0.0))
        elif "error" in status0:
            raise RuntimeError(f"HL order error: {status0['error']!r}")
        elif "resting" in status0:
            # Market IOC should not rest; if it did, treat as failure.
            raise RuntimeError(f"HL market order unexpectedly resting: {status0!r}")
        else:
            raise RuntimeError(f"HL order unrecognized status: {status0!r}")

        fill = FillReport(
            coin=req.coin,
            leg=req.leg,
            side=req.side,
            ts=self._clock_fn(),
            qty=qty,
            price=price,
            fee=fee,
            slippage_bps=self._slippage * 1e4,
            is_paper=False,
            client_ref=req.client_ref,
        )

        # HL IOC market orders can partial-fill when orderbook thins out
        # (common on testnet; rare on liquid mainnet). Leg-pair invariant
        # requires equal qty on both legs — surface partials so AtomicExecutor
        # treats them as failures and triggers reconcile.
        if qty < req.qty * (1 - self._partial_fill_tolerance):
            raise PartialFillError(requested_qty=req.qty, filled_qty=qty, fill=fill)

        return fill

    async def get_position(self, coin: str) -> PositionState | None:
        if self._address is None:
            raise RuntimeError("account_address required for get_position")

        perp_state, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, self._address),
            asyncio.to_thread(self._info.spot_user_state, self._address),
        )

        perp_units = 0.0
        avg_entry_perp: float | None = None
        for entry in perp_state.get("assetPositions", []):
            pos = entry.get("position", {})
            if pos.get("coin") == coin:
                perp_units = float(pos["szi"])
                if "entryPx" in pos:
                    avg_entry_perp = float(pos["entryPx"])
                break

        spot_coin = self._spot_token_map.get(coin, coin)
        spot_units = 0.0
        avg_entry_spot: float | None = None
        for balance in spot_state.get("balances", []):
            if balance.get("coin") == spot_coin:
                spot_units = float(balance["total"])
                entry_ntl = float(balance.get("entryNtl", 0))
                if spot_units > 0 and entry_ntl > 0:
                    avg_entry_spot = entry_ntl / spot_units
                break

        if abs(perp_units) < 1e-12 and abs(spot_units) < 1e-12:
            return None

        return PositionState(
            coin=coin,
            spot_units=spot_units,
            perp_units=perp_units,
            avg_entry_spot=avg_entry_spot,
            avg_entry_perp=avg_entry_perp,
        )

    async def reconcile(self) -> None:
        logger.debug("reconcile is no-op in LiveHLExecutor")
        return None

    async def _sz_decimals(self, coin: str) -> int:
        if self._sz_decimals_cache is None:
            meta = await asyncio.to_thread(self._info.meta)
            self._sz_decimals_cache = {u["name"]: int(u["szDecimals"]) for u in meta["universe"]}
        sz_dec = self._sz_decimals_cache.get(coin)
        if sz_dec is None:
            raise ValueError(f"unknown coin {coin!r} (not in perp meta)")
        return sz_dec

    async def round_qty(self, coin: str, qty: float) -> float:
        """Floor qty to the asset's szDecimals (HL rejects orders with finer precision).

        ROUND_DOWN — conservative, used for initial sizing (spot BUY $/price, spot SELL
        of own balance) where we must not exceed budget or balance.

        Uses Decimal arithmetic — naive float floor like int(0.00014 * 1e5) returns
        13 instead of 14 due to binary representation of 0.00014.
        """
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_DOWN))

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        """Round qty to the asset's szDecimals using ROUND_HALF_UP.

        Used for hedge-leg sizing to minimize the unhedged residual: for a
        spot fill of 0.000149895 BTC the floor gives 0.00014 (leaving $0.76
        long dust at $77k) while half-up gives 0.00015 (~$0.008 short dust,
        ~100x smaller).
        """
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_HALF_UP))

    async def close_position(self, coin: str) -> FillReport | None:
        """Reduce-only close for a perp position, using executor's configured slippage.

        Wraps SDK `Exchange.market_close` to override its default 5% slippage with
        `self._slippage` (typically 1%). Returns None if there was no position to close.
        Useful for residual cleanups and reconcile flows — bypasses HL's $10 min order
        size because the order is reduce-only.
        """
        pos = await self.get_position(coin)
        if pos is None or abs(pos.perp_units) < 1e-12:
            return None
        closing_side = Side.BUY if pos.perp_units < 0 else Side.SELL

        resp = await asyncio.to_thread(
            self._exchange.market_close, coin, None, None, self._slippage
        )

        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"HL market_close rejected: {resp!r}")
        try:
            status0 = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"HL market_close response shape unexpected: {resp!r}") from exc

        if "filled" not in status0:
            if "error" in status0:
                raise RuntimeError(f"HL market_close error: {status0['error']!r}")
            return None

        filled = status0["filled"]
        qty = float(filled["totalSz"])
        if qty <= 0:
            return None
        price = float(filled["avgPx"])
        fee = float(filled.get("fee", 0.0))
        return FillReport(
            coin=coin,
            leg=Leg.PERP,
            side=closing_side,
            ts=self._clock_fn(),
            qty=qty,
            price=price,
            fee=fee,
            slippage_bps=self._slippage * 1e4,
            is_paper=False,
            client_ref=None,
        )

    async def fetch_account_state(self) -> dict[str, Any]:
        """Return raw perp + spot account state dicts for external reconcile callers."""
        if self._address is None:
            raise RuntimeError("account_address required for fetch_account_state")
        perp_state, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, self._address),
            asyncio.to_thread(self._info.spot_user_state, self._address),
        )
        return {"perp": perp_state, "spot": spot_state}
