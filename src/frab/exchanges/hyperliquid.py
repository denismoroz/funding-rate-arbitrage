from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from frab.exchanges.base import FundingTick, Leg, MarketSpec, Quote, Side, UserFill

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly

# Inverse of MAINNET_SPOT_TOKEN_MAP in server.py:
# HL spot coins like "UBTC/USDC" → underlying perp coin "BTC".
_SPOT_TOKEN_INVERSE: dict[str, str] = {
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
    "AVAX0": "AVAX",
    "LINK0": "LINK",
    "AAVE0": "AAVE",
}


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class _RetryableHTTPError(Exception):
    pass


_RETRYABLE = retry_if_exception_type(
    (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, _RetryableHTTPError)
)
_WAIT = wait_exponential(multiplier=0.3, min=0.3, max=4)
_STOP = stop_after_attempt(4)


class HLMarketData:
    name = "hyperliquid"

    def __init__(
        self,
        api_url: str = "https://api.hyperliquid.xyz/info",
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_url = api_url
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=timeout_s)
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HLMarketData":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

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
        # Fall back to mark when an orderbook side is empty (HL testnet often
        # has thin/empty books — engine must not crash on this).
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

    def _normalize_hl_coin(self, hl_coin: str) -> tuple[str, Leg]:
        """Normalize HL coin field to (coin, leg).

        Perp coins are plain names like "BTC". Spot coins have a slash like
        "UBTC/USDC" — strip the slash, look up the wrapped token in the inverse
        map (UBTC → BTC), fall back to the part before the slash if not found.
        """
        if "/" in hl_coin:
            wrapped = hl_coin.split("/")[0]
            coin = _SPOT_TOKEN_INVERSE.get(wrapped, wrapped)
            return coin, Leg.SPOT
        return hl_coin, Leg.PERP

    async def fetch_user_fills(self, user_address: str, since_ms: int) -> list[UserFill]:
        """Fetch user's actual fills with real fees from HL.

        POST /info  body: {"type": "userFillsByTime", "user": user_address, "startTime": since_ms}
        Returns parsed list ordered by ts ascending.
        """
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
            coin, leg = self._normalize_hl_coin(hl_coin)
            # HL side: "B" → BUY, "A" → SELL (maker/taker from the aggressor side)
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
