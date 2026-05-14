from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from frab.exchanges.base import FundingTick, MarketSpec, Quote

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly


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
        ts_ms = int(record["time"])
        return FundingTick(
            coin=coin,
            ts_ms=ts_ms,
            rate=rate,
            premium=premium,
            annualized_pct=rate * _PERIODS_PER_YEAR * 100,
        )

    async def fetch_funding(self, coin: str) -> FundingTick:
        now_ms = int(time.time() * 1000)
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
        ticks.sort(key=lambda t: t.ts_ms)
        return ticks

    async def fetch_quote(self, coin: str) -> Quote:
        mids_data, book = await asyncio.gather(
            self._post({"type": "allMids"}),
            self._post({"type": "l2Book", "coin": coin}),
        )
        mark = float(mids_data[coin])
        levels = book["levels"]
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
        ts_ms = int(book["time"])
        return Quote(coin=coin, ts_ms=ts_ms, bid=bid, ask=ask, mark=mark, spot=None)

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
