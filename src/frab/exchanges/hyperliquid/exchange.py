"""HLExchange: single class owning both read (httpx) and write (HL SDK) paths.

Merged from reader.py (HLExchangeReader) and live.py (LiveHLExecutor).
Read methods use an httpx.AsyncClient against the HL /info REST endpoint.
Write methods use the hyperliquid-python-sdk Info/Exchange objects (sync,
run via asyncio.to_thread).

The two HTTP clients cannot be shared because reader uses httpx while the
SDK uses its own internal requests session.  Both are initialized at
construction and closed together in aclose()/aexit.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable, Literal

import httpx
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from frab.exchanges.base import (
    FillReport,
    FundingPayment,
    FundingTick,
    Leg,
    MarketSpec,
    OrderRequest,
    PositionState,
    Quote,
    Side,
    UserFill,
)
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST

logger = logging.getLogger(__name__)

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly

# Inverse of MAINNET_SPOT_TOKEN_MAP: HL wrapped token → canonical perp coin.
# BRIDGE_TOKEN_BLACKLIST names are explicitly excluded (independent price discovery).
_SPOT_TOKEN_INVERSE: dict[str, str] = {
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
}


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _base_url(network: Literal["testnet", "mainnet"]) -> str:
    if network == "testnet":
        return constants.TESTNET_API_URL
    if network == "mainnet":
        return constants.MAINNET_API_URL
    raise ValueError(f"unsupported network: {network!r}")


class _RetryableHTTPError(Exception):
    pass


_RETRYABLE = retry_if_exception_type(
    (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, _RetryableHTTPError)
)
_WAIT = wait_exponential(multiplier=0.3, min=0.3, max=4)
_STOP = stop_after_attempt(4)


class HLTransferError(RuntimeError):
    """Raised when a usdClassTransfer action is rejected by Hyperliquid."""


class PartialFillError(RuntimeError):
    """Raised when HL filled less than the requested qty beyond tolerance."""

    def __init__(self, requested_qty: float, filled_qty: float, fill: FillReport) -> None:
        super().__init__(
            f"partial fill: requested {requested_qty}, filled {filled_qty} "
            f"({filled_qty / requested_qty * 100:.1f}%)"
        )
        self.requested_qty = requested_qty
        self.filled_qty = filled_qty
        self.fill = fill


class HLExchange:
    """Unified Hyperliquid exchange class: read (httpx) + write (SDK)."""

    name = "hyperliquid"

    def __init__(
        self,
        *,
        # --- Read-side (httpx) ---
        api_url: str = "https://api.hyperliquid.xyz/info",
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
        # --- Write-side (SDK) ---
        private_key: str | None = None,
        account_address: str | None = None,
        network: Literal["testnet", "mainnet"] = "testnet",
        info: Info | None = None,
        exchange: Exchange | None = None,
        # --- Shared order settings ---
        spot_token_map: dict[str, str] | None = None,
        spot_quote_token: str = "USDC",
        slippage: float = 0.01,
        partial_fill_tolerance: float = 0.01,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # --- httpx client for read methods ---
        self._api_url = api_url
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=timeout_s)
            self._owns_client = True

        # Lazy cache: spot pair asset_id → pair name (e.g. 142 → "UBTC/USDC").
        self._spot_idx_to_name: dict[int, str] | None = None

        # --- SDK objects for write methods ---
        if info is None:
            info = Info(_base_url(network), skip_ws=True)
        if exchange is None and (private_key is not None and account_address is not None):
            wallet = Account.from_key(private_key)
            exchange = Exchange(
                wallet=wallet,
                base_url=_base_url(network),
                account_address=account_address,
            )

        self._info = info
        self._exchange = exchange

        if account_address is not None:
            self._address: str | None = account_address
        else:
            candidate = getattr(exchange, "account_address", None) if exchange is not None else None
            self._address = candidate if isinstance(candidate, str) else None

        self._spot_token_map: dict[str, str] = spot_token_map if spot_token_map is not None else {}
        self._spot_quote_token = spot_quote_token
        self._slippage = slippage
        self._partial_fill_tolerance = partial_fill_tolerance
        self._clock_fn = clock_fn if clock_fn is not None else lambda: datetime.now(UTC)
        self._sz_decimals_cache: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HLExchange":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal: httpx retry wrapper
    # ------------------------------------------------------------------

    async def _post(self, body: dict) -> Any:
        async for attempt in AsyncRetrying(
            retry=_RETRYABLE,
            wait=_WAIT,
            stop=_STOP,
            reraise=True,
        ):
            with attempt:
                resp = await self._client.post(self._api_url, json=body)
                if resp.status_code >= 500:
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

    # ------------------------------------------------------------------
    # Internal: spot index map (lazy cache)
    # ------------------------------------------------------------------

    async def _load_spot_idx_map(self) -> None:
        """Cache pair_idx → base_token_name (e.g. 142 → 'UBTC')."""
        if self._spot_idx_to_name is not None:
            return
        meta = await self._post({"type": "spotMeta"})
        token_by_idx: dict[int, str] = {}
        for t in meta.get("tokens", []):
            tidx = t.get("index")
            tname = t.get("name", "")
            if isinstance(tidx, int) and tname:
                token_by_idx[tidx] = tname
        mapping: dict[int, str] = {}
        for entry in meta.get("universe", []):
            idx = entry.get("index")
            if not isinstance(idx, int):
                continue
            name = entry.get("name", "")
            if "/" in name:
                mapping[idx] = name
                continue
            toks = entry.get("tokens") or []
            if toks and isinstance(toks[0], int):
                base = token_by_idx.get(toks[0])
                if base:
                    mapping[idx] = f"{base}/USDC"
        self._spot_idx_to_name = mapping

    # ------------------------------------------------------------------
    # Internal: coin normalization
    # ------------------------------------------------------------------

    async def _normalize_hl_coin(self, hl_coin: str) -> tuple[str, Leg]:
        """Normalize HL coin field to (coin, leg).

        Raises ValueError if the resolved token is in BRIDGE_TOKEN_BLACKLIST
        (independent price discovery — must not be aliased to a perp coin).
        """
        if hl_coin.startswith("@"):
            try:
                idx = int(hl_coin[1:])
            except ValueError:
                return hl_coin, Leg.PERP
            await self._load_spot_idx_map()
            name = (self._spot_idx_to_name or {}).get(idx)
            if name and "/" in name:
                wrapped = name.split("/")[0]
                if wrapped in BRIDGE_TOKEN_BLACKLIST:
                    raise ValueError(
                        f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                        f"(independent price discovery — not safe to map to perp coin)"
                    )
                return _SPOT_TOKEN_INVERSE.get(wrapped, wrapped), Leg.SPOT
            return hl_coin, Leg.PERP
        if "/" in hl_coin:
            wrapped = hl_coin.split("/")[0]
            if wrapped in BRIDGE_TOKEN_BLACKLIST:
                raise ValueError(
                    f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                    f"(independent price discovery — not safe to map to perp coin)"
                )
            coin = _SPOT_TOKEN_INVERSE.get(wrapped, wrapped)
            return coin, Leg.SPOT
        return hl_coin, Leg.PERP

    def _record_to_tick(self, coin: str, record: dict) -> FundingTick:
        rate = float(record["fundingRate"])
        premium = float(record["premium"])
        ts = _ms_to_dt(int(record["time"]))
        return FundingTick(
            coin=coin,
            ts=ts,
            rate=rate,
            premium=premium,
            annualized_pct=rate * _PERIODS_PER_YEAR * 100,
        )

    # ------------------------------------------------------------------
    # Read methods (httpx)
    # ------------------------------------------------------------------

    async def fetch_funding(self, coin: str) -> FundingTick:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        data: list[dict] = await self._post({
            "type": "fundingHistory",
            "coin": coin,
            "startTime": now_ms - 2 * 3600 * 1000,
        })
        if not data:
            raise ValueError(f"no recent funding for {coin}")
        return self._record_to_tick(coin, data[-1])

    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]:
        ticks: list[FundingTick] = []
        start = since_ms
        while True:
            data: list[dict] = await self._post({
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start,
            })
            for rec in data:
                ticks.append(self._record_to_tick(coin, rec))
            if len(data) < 500:
                break
            start = int(data[-1]["time"]) + 1
        ticks.sort(key=lambda t: t.ts)
        return ticks

    async def fetch_quote(self, coin: str) -> Quote:
        mids_data, book = await asyncio.gather(
            self._post({"type": "allMids"}),
            self._post({"type": "l2Book", "coin": coin}),
        )
        mark = float(mids_data[coin])
        levels = book.get("levels") or []
        bids = levels[0] if len(levels) >= 1 else []
        asks = levels[1] if len(levels) >= 2 else []
        bid = float(bids[0]["px"]) if bids else mark
        ask = float(asks[0]["px"]) if asks else mark
        ts = _ms_to_dt(int(book["time"]))
        return Quote(coin=coin, ts=ts, bid=bid, ask=ask, mark=mark, spot=None)

    async def fetch_meta(self) -> list[MarketSpec]:
        data = await self._post({"type": "meta"})
        specs: list[MarketSpec] = []
        for entry in data["universe"]:
            sz = int(entry["szDecimals"])
            min_size = 10 ** -sz
            exp = 6 - sz
            tick_size = 10 ** -exp if exp >= 0 else 1.0
            specs.append(MarketSpec(
                coin=entry["name"],
                has_spot=False,
                has_perp=True,
                min_size=min_size,
                tick_size=tick_size,
            ))
        return specs

    async def fetch_user_fills(self, user_address: str, since_ms: int) -> list[UserFill]:
        data: list[dict] = await self._post({
            "type": "userFillsByTime",
            "user": user_address,
            "startTime": since_ms,
        })
        if not data:
            return []

        fills: list[UserFill] = []
        for record in data:
            hl_coin = record["coin"]
            coin, leg = await self._normalize_hl_coin(hl_coin)
            side = Side.BUY if record["side"] == "B" else Side.SELL
            fills.append(UserFill(
                coin=coin,
                ts=_ms_to_dt(int(record["time"])),
                leg=leg,
                side=side,
                qty=abs(float(record["sz"])),
                price=float(record["px"]),
                fee=float(record["fee"]),
                fee_token=record["feeToken"],
                hl_oid=int(record["oid"]),
                hl_tid=int(record["tid"]),
            ))

        fills.sort(key=lambda f: f.ts)
        return fills

    async def fetch_user_funding(self, user_address: str, since_ms: int) -> list[FundingPayment]:
        data: list[dict] = await self._post({
            "type": "userFunding",
            "user": user_address,
            "startTime": since_ms,
        })
        if not data:
            return []

        payments: list[FundingPayment] = []
        for record in data:
            delta = record["delta"]
            payments.append(FundingPayment(
                coin=delta["coin"],
                ts=_ms_to_dt(int(record["time"])),
                usdc=float(delta["usdc"]),
                szi=float(delta["szi"]),
                rate=float(delta["fundingRate"]),
                hash=record["hash"],
            ))

        payments.sort(key=lambda p: p.ts)
        return payments

    # ------------------------------------------------------------------
    # Write methods (SDK via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _require_exchange(self) -> Exchange:
        if self._exchange is None:
            raise RuntimeError(
                "HLExchange write methods require private_key + account_address "
                "(or an injected exchange object)"
            )
        return self._exchange

    def _require_address(self) -> str:
        if self._address is None:
            raise RuntimeError("account_address required")
        return self._address

    def _make_order_name(self, req: OrderRequest) -> str:
        if req.leg == Leg.PERP:
            return req.coin
        base = self._spot_token_map.get(req.coin, req.coin)
        return f"{base}/{self._spot_quote_token}"

    async def submit(self, req: OrderRequest) -> FillReport:
        exchange = self._require_exchange()
        name = self._make_order_name(req)
        is_buy = req.side == Side.BUY

        resp = await asyncio.to_thread(
            exchange.market_open, name, is_buy, req.qty, None, self._slippage
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
            fee = float(filled.get("fee", 0.0))
        elif "error" in status0:
            raise RuntimeError(f"HL order error: {status0['error']!r}")
        elif "resting" in status0:
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
            client_ref=req.client_ref,
        )

        if qty < req.qty * (1 - self._partial_fill_tolerance):
            raise PartialFillError(requested_qty=req.qty, filled_qty=qty, fill=fill)

        return fill

    async def get_position(self, coin: str) -> PositionState | None:
        address = self._require_address()

        perp_state, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, address),
            asyncio.to_thread(self._info.spot_user_state, address),
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
        logger.debug("reconcile is no-op in HLExchange")

    async def _sz_decimals(self, coin: str) -> int:
        if self._sz_decimals_cache is None:
            meta = await asyncio.to_thread(self._info.meta)
            self._sz_decimals_cache = {u["name"]: int(u["szDecimals"]) for u in meta["universe"]}
        sz_dec = self._sz_decimals_cache.get(coin)
        if sz_dec is None:
            raise ValueError(f"unknown coin {coin!r} (not in perp meta)")
        return sz_dec

    async def round_qty(self, coin: str, qty: float) -> float:
        """Floor qty to asset's szDecimals (conservative; used for initial sizing)."""
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_DOWN))

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        """Round qty to asset's szDecimals with ROUND_HALF_UP (minimises hedge residual)."""
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_HALF_UP))

    async def close_position(self, coin: str) -> FillReport | None:
        """Reduce-only market close for a perp position. Returns None if flat."""
        exchange = self._require_exchange()
        pos = await self.get_position(coin)
        if pos is None or abs(pos.perp_units) < 1e-12:
            return None
        closing_side = Side.BUY if pos.perp_units < 0 else Side.SELL

        resp = await asyncio.to_thread(
            exchange.market_close, coin, None, None, self._slippage
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
            client_ref=None,
        )

    async def fetch_account_state(self) -> dict[str, Any]:
        """Return raw perp + spot account state dicts."""
        address = self._require_address()
        perp_state, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, address),
            asyncio.to_thread(self._info.spot_user_state, address),
        )
        return {"perp": perp_state, "spot": spot_state}

    async def transfer_spot_to_perp(self, usdc_amount: float) -> dict:
        """Transfer USDC from spot wallet to perp margin account."""
        exchange = self._require_exchange()
        if usdc_amount <= 0:
            raise ValueError(f"usdc_amount must be positive, got {usdc_amount!r}")
        resp = await asyncio.to_thread(exchange.usd_class_transfer, usdc_amount, True)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise HLTransferError(f"HL usdClassTransfer spot→perp rejected: {resp!r}")
        logger.info("transfer_spot_to_perp amount=%.4f ok", usdc_amount)
        return {"status": "ok", "amount": usdc_amount, "direction": "spot_to_perp", "response": resp}

    async def transfer_perp_to_spot(self, usdc_amount: float) -> dict:
        """Transfer USDC from perp margin account to spot wallet."""
        exchange = self._require_exchange()
        if usdc_amount <= 0:
            raise ValueError(f"usdc_amount must be positive, got {usdc_amount!r}")
        resp = await asyncio.to_thread(exchange.usd_class_transfer, usdc_amount, False)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise HLTransferError(f"HL usdClassTransfer perp→spot rejected: {resp!r}")
        logger.info("transfer_perp_to_spot amount=%.4f ok", usdc_amount)
        return {"status": "ok", "amount": usdc_amount, "direction": "perp_to_spot", "response": resp}

    def _inverse_spot_token_map(self) -> dict[str, str]:
        return {v: k for k, v in self._spot_token_map.items()}

    def _normalize_spot_coin(self, hl_coin: str) -> str:
        """Translate a HL spot coin name to the canonical coin name via inverse map."""
        return self._inverse_spot_token_map().get(hl_coin, hl_coin)

    async def fetch_wallet_state(
        self,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Return a normalized wallet snapshot suitable for the /api/equity/wallet endpoint."""
        address = self._require_address()  # noqa: F841 — triggers address check early

        raw = await self.fetch_account_state()
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

            canonical = self._normalize_spot_coin(hl_coin)

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
