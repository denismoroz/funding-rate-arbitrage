"""HLClient: thin async transport over Hyperliquid /info + SDK.

All raw HL JSON parsing happens here. Callers receive typed dataclasses
from wire.py. No DB, no business logic, no coin normalization.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from frab.exchanges.hyperliquid.wire import (
    HLFillRecord,
    HLFundingDelta,
    HLFundingRecord,
    HLL2Snapshot,
    HLOrderResponse,
    HLOrderStatus,
    HLPerpAssetPosition,
    HLPerpMarketSpec,
    HLPerpState,
    HLSpotBalance,
    HLSpotMeta,
    HLSpotPair,
    HLSpotState,
    HLUserFill,
)

logger = logging.getLogger(__name__)


class _RetryableHTTPError(Exception):
    pass


_RETRYABLE = retry_if_exception_type(
    (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, _RetryableHTTPError)
)
_WAIT = wait_exponential(multiplier=0.3, min=0.3, max=4)
_STOP = stop_after_attempt(4)


class HLTransferError(RuntimeError):
    """Raised when a usdClassTransfer action is rejected by Hyperliquid."""


class HLClient:
    """Thin async wrapper over Hyperliquid /info endpoint (httpx) and SDK Info/Exchange (asyncio.to_thread).

    All raw HL JSON parsing happens here. Callers receive typed dataclasses from wire.py.
    No DB, no business logic, no coin normalization.
    """

    def __init__(
        self,
        *,
        api_url: str = "https://api.hyperliquid.xyz/info",
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
        info: Info | None = None,
        exchange: Exchange | None = None,
    ) -> None:
        self._api_url = api_url
        if client is not None:
            self._http = client
            self._owns_http = False
        else:
            self._http = httpx.AsyncClient(timeout=timeout_s)
            self._owns_http = True
        self._info = info
        self._exchange = exchange

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "HLClient":
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
                resp = await self._http.post(self._api_url, json=body)
                if resp.status_code >= 500:
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

    # ------------------------------------------------------------------
    # Escape hatch (raw)
    # ------------------------------------------------------------------

    async def info_request(self, body: dict) -> Any:
        """Send a raw /info request and return the parsed JSON response."""
        return await self._post(body)

    # ------------------------------------------------------------------
    # Read (httpx)
    # ------------------------------------------------------------------

    async def all_mids(self) -> dict[str, float]:
        """Return {coin: mid_price} for all active markets."""
        data = await self._post({"type": "allMids"})
        return {k: float(v) for k, v in (data or {}).items()}

    async def l2_book(self, coin: str) -> HLL2Snapshot:
        """Return best bid/ask and timestamp for a coin."""
        data = await self._post({"type": "l2Book", "coin": coin})
        levels = data.get("levels") or []
        bids = levels[0] if len(levels) >= 1 else []
        asks = levels[1] if len(levels) >= 2 else []
        bid = float(bids[0]["px"]) if bids else 0.0
        ask = float(asks[0]["px"]) if asks else 0.0
        ts_ms = int(data.get("time", 0))
        return HLL2Snapshot(bid=bid, ask=ask, ts_ms=ts_ms)

    async def perp_meta(self) -> list[HLPerpMarketSpec]:
        """Return perp market specs for all universe entries."""
        data = await self._post({"type": "meta"})
        return [
            HLPerpMarketSpec(
                name=entry["name"],
                sz_decimals=int(entry["szDecimals"]),
            )
            for entry in data.get("universe", [])
        ]

    async def spot_meta(self) -> HLSpotMeta:
        """Return spot meta with resolved pair names.

        Pair name resolution: looks up tokens[universe[i].tokens[0]] /
        tokens[universe[i].tokens[1]]. If the entry's raw name already
        contains "/", it is used as-is.
        """
        data = await self._post({"type": "spotMeta"})
        token_by_idx: dict[int, str] = {}
        for t in data.get("tokens", []):
            tidx = t.get("index")
            tname = t.get("name", "")
            if isinstance(tidx, int) and tname:
                token_by_idx[tidx] = tname

        pairs: list[HLSpotPair] = []
        for entry in data.get("universe", []):
            idx = entry.get("index")
            if not isinstance(idx, int):
                continue
            raw_name = entry.get("name", "")
            if "/" in raw_name:
                pairs.append(HLSpotPair(index=idx, name=raw_name))
                continue
            toks = entry.get("tokens") or []
            if len(toks) >= 2 and isinstance(toks[0], int) and isinstance(toks[1], int):
                base = token_by_idx.get(toks[0])
                quote = token_by_idx.get(toks[1])
                if base and quote:
                    pairs.append(HLSpotPair(index=idx, name=f"{base}/{quote}"))
                    continue
            pairs.append(HLSpotPair(index=idx, name=raw_name))

        return HLSpotMeta(tokens=token_by_idx, pairs=pairs)

    async def funding_history(self, coin: str, since_ms: int) -> list[HLFundingRecord]:
        """Fetch paginated funding history for coin since since_ms.

        Paginates until batch size < 500, sorts by ts_ms before returning.
        """
        records: list[HLFundingRecord] = []
        start = since_ms
        while True:
            batch: list[dict] = await self._post({
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start,
            })
            for rec in batch:
                records.append(HLFundingRecord(
                    coin=coin,
                    ts_ms=int(rec["time"]),
                    rate=float(rec["fundingRate"]),
                    premium=float(rec["premium"]),
                ))
            if len(batch) < 500:
                break
            start = int(batch[-1]["time"]) + 1
        records.sort(key=lambda r: r.ts_ms)
        return records

    async def user_funding(self, address: str, since_ms: int) -> list[HLFundingDelta]:
        """Return user funding settlement deltas since since_ms."""
        data: list[dict] = await self._post({
            "type": "userFunding",
            "user": address,
            "startTime": since_ms,
        })
        deltas: list[HLFundingDelta] = []
        for record in data or []:
            delta = record.get("delta", {})
            coin = delta.get("coin")
            if not coin:
                continue
            deltas.append(HLFundingDelta(
                coin=coin,
                ts_ms=int(record["time"]),
                amount_usdc=float(delta["usdc"]),
            ))
        return deltas

    # ------------------------------------------------------------------
    # Read (SDK Info, run via to_thread)
    # ------------------------------------------------------------------

    def _require_info(self) -> Info:
        if self._info is None:
            raise RuntimeError("HLClient SDK reads require `info` handle")
        return self._info

    async def user_state(self, address: str) -> HLPerpState:
        """Return parsed perp account state for address."""
        info = self._require_info()
        raw = await asyncio.to_thread(info.user_state, address)
        margin = raw.get("marginSummary") or {}
        account_value = float(margin.get("accountValue", 0.0))
        positions: list[HLPerpAssetPosition] = []
        for entry in raw.get("assetPositions") or []:
            pos = entry.get("position") or {}
            coin = pos.get("coin", "")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi", 0.0))
            except (TypeError, ValueError):
                szi = 0.0
            try:
                unrealized_pnl = float(pos.get("unrealizedPnl", 0.0))
            except (TypeError, ValueError):
                unrealized_pnl = 0.0
            cf = pos.get("cumFunding") or {}
            try:
                cum_funding_since_open = float(cf.get("sinceOpen", 0.0))
            except (TypeError, ValueError):
                cum_funding_since_open = 0.0
            try:
                margin_used = float(pos.get("marginUsed", 0.0))
            except (TypeError, ValueError):
                margin_used = 0.0
            try:
                position_value = float(pos.get("positionValue", 0.0))
            except (TypeError, ValueError):
                position_value = 0.0
            lev_raw = (pos.get("leverage") or {}).get("value")
            if lev_raw is None:
                leverage_value: int | None = None
            else:
                try:
                    leverage_value = int(lev_raw)
                except (TypeError, ValueError):
                    leverage_value = None
            positions.append(HLPerpAssetPosition(
                coin=coin,
                szi=szi,
                unrealized_pnl=unrealized_pnl,
                cum_funding_since_open=cum_funding_since_open,
                margin_used=margin_used,
                position_value=position_value,
                leverage_value=leverage_value,
            ))
        return HLPerpState(account_value=account_value, asset_positions=positions)

    async def spot_user_state(self, address: str) -> HLSpotState:
        """Return all spot balances for address (caller filters zero balances if desired)."""
        info = self._require_info()
        raw = await asyncio.to_thread(info.spot_user_state, address)
        balances: list[HLSpotBalance] = []
        for bal in raw.get("balances") or []:
            coin = bal.get("coin", "")
            if not coin:
                continue
            try:
                total = float(bal.get("total", 0.0))
            except (TypeError, ValueError):
                total = 0.0
            try:
                hold = float(bal.get("hold", 0.0))
            except (TypeError, ValueError):
                hold = 0.0
            balances.append(HLSpotBalance(coin=coin, total=total, hold=hold))
        return HLSpotState(balances=balances)

    async def user_fills_by_time(
        self,
        address: str,
        since_ms: int,
        end_ms: int | None = None,
    ) -> list[HLUserFill]:
        """Return typed user fills between since_ms and end_ms."""
        info = self._require_info()
        if end_ms is not None:
            raw = await asyncio.to_thread(info.user_fills_by_time, address, since_ms, end_ms)
        else:
            raw = await asyncio.to_thread(info.user_fills_by_time, address, since_ms)
        if not isinstance(raw, list):
            return []
        fills: list[HLUserFill] = []
        for f in raw:
            if not isinstance(f, dict):
                continue
            try:
                fills.append(HLUserFill(
                    oid=int(f.get("oid", 0)),
                    side=str(f.get("side", "")),
                    sz=float(f.get("sz", 0.0)),
                    px=float(f.get("px", 0.0)),
                    ts_ms=int(f.get("time", 0)),
                    fee_raw=float(f.get("fee", 0.0)),
                    fee_token=str(f.get("feeToken") or "USDC").upper(),
                    coin=str(f.get("coin", "")),
                ))
            except (TypeError, ValueError):
                continue
        return fills

    # ------------------------------------------------------------------
    # Write (SDK Exchange, run via to_thread)
    # ------------------------------------------------------------------

    def _require_exchange(self) -> Exchange:
        if self._exchange is None:
            raise RuntimeError("HLClient write methods require `exchange` (SDK with keys)")
        return self._exchange

    @staticmethod
    def _parse_order_response(resp: Any) -> HLOrderResponse:
        """Parse an HL order response dict into HLOrderResponse."""
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"HL order rejected/shape: {resp!r}")
        try:
            raw_statuses = resp["response"]["data"]["statuses"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"HL order rejected/shape: {resp!r}") from exc

        statuses: list[HLOrderStatus] = []
        for status in raw_statuses:
            if not isinstance(status, dict):
                raise RuntimeError(f"HL order rejected/shape: {resp!r}")
            if "filled" in status:
                filled = status["filled"]
                oid = int(filled.get("oid", 0)) or None
                fee_usdc = float(filled["fee"]) if "fee" in filled else None
                statuses.append(HLOrderStatus(filled=HLFillRecord(
                    qty=float(filled["totalSz"]),
                    price=float(filled["avgPx"]),
                    oid=oid,
                    fee_usdc=fee_usdc,
                )))
            elif "error" in status:
                statuses.append(HLOrderStatus(error=str(status["error"])))
            elif "resting" in status:
                resting_oid = int(status["resting"].get("oid", 0)) or None
                statuses.append(HLOrderStatus(resting_oid=resting_oid))
            else:
                raise RuntimeError(f"HL order unrecognized status: {status!r}")

        return HLOrderResponse(statuses=statuses)

    async def market_open(
        self,
        symbol: str,
        is_buy: bool,
        qty: float,
        slippage: float,
    ) -> HLOrderResponse:
        """Open a market position. Requires exchange handle."""
        exchange = self._require_exchange()
        resp = await asyncio.to_thread(
            exchange.market_open, symbol, is_buy, qty, None, slippage
        )
        return self._parse_order_response(resp)

    async def market_close(self, coin: str, slippage: float) -> HLOrderResponse:
        """Close a perp market position. Requires exchange handle."""
        exchange = self._require_exchange()
        resp = await asyncio.to_thread(
            exchange.market_close, coin, None, None, slippage
        )
        return self._parse_order_response(resp)

    async def update_leverage(self, coin: str, leverage: int) -> None:
        """Set cross-margin leverage for a perp asset.

        Raises RuntimeError if HL returns non-'ok' status.
        """
        exchange = self._require_exchange()
        resp = await asyncio.to_thread(
            exchange.update_leverage, int(leverage), coin, True
        )
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(
                f"HL updateLeverage rejected for coin={coin} leverage={leverage}: {resp!r}"
            )

    async def usd_class_transfer(self, amount: float, to_perp: bool) -> None:
        """Transfer USDC between spot and perp wallets.

        Raises HLTransferError if HL returns non-'ok' status.
        """
        exchange = self._require_exchange()
        resp = await asyncio.to_thread(exchange.usd_class_transfer, amount, to_perp)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise HLTransferError(
                f"HL usdClassTransfer rejected: {resp!r}"
            )
