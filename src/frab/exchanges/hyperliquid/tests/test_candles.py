"""Unit tests for HLClient.candle_snapshot and HLExchange.get_daily_candles."""
from __future__ import annotations

import pytest
import httpx
import respx
from unittest.mock import AsyncMock, MagicMock

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.exchanges.hyperliquid.wire import HLCandle

BASE_URL = "https://api.hyperliquid.xyz"
INFO_URL = f"{BASE_URL}/info"

# Canned candleSnapshot payload — 5 candles, intentionally out of order to verify sort.
_CANDLE_PAYLOAD = [
    {"t": 1_700_086_400_000, "T": 1_700_172_800_000, "s": "BTC", "o": "29900", "h": "30500", "l": "29800", "c": "30000.5", "v": "100", "n": 50},
    {"t": 1_700_000_000_000, "T": 1_700_086_400_000, "s": "BTC", "o": "29000", "h": "29500", "l": "28900", "c": "29200.0", "v": "90",  "n": 40},
    {"t": 1_700_172_800_000, "T": 1_700_259_200_000, "s": "BTC", "o": "30100", "h": "31000", "l": "29900", "c": "30800.0", "v": "110", "n": 60},
    # Earlier candle to ensure sort ascending works.
    {"t": 1_699_913_600_000, "T": 1_700_000_000_000, "s": "BTC", "o": "28500", "h": "29100", "l": "28400", "c": "29000.0", "v": "80",  "n": 35},
    {"t": 1_700_259_200_000, "T": 1_700_345_600_000, "s": "BTC", "o": "30900", "h": "31500", "l": "30700", "c": "31200.0", "v": "120", "n": 70},
]

# Expected close_ms order (ascending by T).
_EXPECTED_CLOSE_MS = [
    1_700_000_000_000,
    1_700_086_400_000,
    1_700_172_800_000,
    1_700_259_200_000,
    1_700_345_600_000,
]
_EXPECTED_CLOSES = [29000.0, 30000.5, 30800.0, 31200.0, 31200.0]

# Recompute expected closes from the payload (keyed by T) to avoid typos.
_T_TO_CLOSE = {int(r["T"]): float(r["c"]) for r in _CANDLE_PAYLOAD}


@pytest.fixture
def http_client():
    return httpx.AsyncClient(base_url=BASE_URL)


@pytest.fixture
def hl_client(http_client):
    return HLClient(client=http_client)


# ---------------------------------------------------------------------------
# 1. candle_snapshot: happy path — parses and sorts ascending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_candle_snapshot_parses_and_sorts(hl_client):
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=_CANDLE_PAYLOAD)
        candles = await hl_client.candle_snapshot("BTC", "1d", 0, 9_999_999_999_999)

    assert len(candles) == 5
    assert all(isinstance(c, HLCandle) for c in candles)
    # Verify sorted ascending by close_ms.
    close_ms_list = [c.close_ms for c in candles]
    assert close_ms_list == sorted(close_ms_list), "candles must be sorted ascending by close_ms"
    # Verify first and last.
    assert candles[0].close_ms == 1_700_000_000_000
    assert candles[0].close == pytest.approx(29000.0)
    assert candles[-1].close_ms == 1_700_345_600_000
    assert candles[-1].close == pytest.approx(31200.0)
    # Verify all coin fields.
    assert all(c.coin == "BTC" for c in candles)
    await hl_client.aclose()


# ---------------------------------------------------------------------------
# 2. candle_snapshot: empty/None response returns []
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_candle_snapshot_empty_response(hl_client):
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[])
        candles = await hl_client.candle_snapshot("BTC", "1d", 0, 9_999_999_999_999)
    assert candles == []
    await hl_client.aclose()


@pytest.mark.asyncio
async def test_candle_snapshot_skips_malformed_entries(hl_client):
    """Malformed candle entries (missing keys) are skipped; valid ones are parsed."""
    payload = [
        # Valid candle.
        {"t": 1_700_000_000_000, "T": 1_700_086_400_000, "s": "BTC", "o": "100", "h": "110", "l": "90", "c": "105.0", "v": "1", "n": 1},
        # Missing "c" key — should be skipped.
        {"t": 1_700_086_400_000, "T": 1_700_172_800_000, "s": "BTC"},
    ]
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        candles = await hl_client.candle_snapshot("BTC", "1d", 0, 9_999_999_999_999)
    assert len(candles) == 1
    assert candles[0].close == pytest.approx(105.0)
    await hl_client.aclose()


# ---------------------------------------------------------------------------
# 3. candle_snapshot: request body shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_candle_snapshot_request_body(hl_client):
    """Verify the exact POST body sent to HL."""
    import json as _json

    captured: list[dict] = []

    async def side_effect(request: httpx.Request) -> httpx.Response:
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json=[])

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        await hl_client.candle_snapshot("ETH", "1d", 1_000_000, 2_000_000)

    assert len(captured) == 1
    body = captured[0]
    assert body["type"] == "candleSnapshot"
    req = body["req"]
    assert req["coin"] == "ETH"
    assert req["interval"] == "1d"
    assert req["startTime"] == 1_000_000
    assert req["endTime"] == 2_000_000
    await hl_client.aclose()


# ---------------------------------------------------------------------------
# 4. get_daily_candles: maps to (close_ms, close) and trims to `days`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_daily_candles_returns_tuples_ascending():
    """HLExchange.get_daily_candles returns [(close_ms, close), ...] ascending."""
    # Mock the underlying HLClient.candle_snapshot.
    mock_client = MagicMock()
    mock_client.candle_snapshot = AsyncMock(return_value=[
        HLCandle(coin="BTC", open_ms=1_699_913_600_000, close_ms=1_700_000_000_000, close=29000.0),
        HLCandle(coin="BTC", open_ms=1_700_000_000_000, close_ms=1_700_086_400_000, close=30000.5),
        HLCandle(coin="BTC", open_ms=1_700_086_400_000, close_ms=1_700_172_800_000, close=30800.0),
    ])
    mock_client.aclose = AsyncMock()

    exchange = HLExchange.__new__(HLExchange)
    exchange._hl_client = mock_client

    result = await exchange.get_daily_candles("BTC", days=10)

    assert isinstance(result, list)
    assert all(isinstance(r, tuple) and len(r) == 2 for r in result)
    # Ascending order.
    close_ms_list = [r[0] for r in result]
    assert close_ms_list == sorted(close_ms_list)
    assert result[0] == (1_700_000_000_000, pytest.approx(29000.0))
    assert result[-1] == (1_700_172_800_000, pytest.approx(30800.0))


@pytest.mark.asyncio
async def test_get_daily_candles_trims_to_days():
    """get_daily_candles returns at most `days` candles (most recent)."""
    candles = [
        HLCandle(coin="ETH", open_ms=i * 86_400_000, close_ms=(i + 1) * 86_400_000, close=float(i))
        for i in range(10)
    ]
    mock_client = MagicMock()
    mock_client.candle_snapshot = AsyncMock(return_value=candles)
    mock_client.aclose = AsyncMock()

    exchange = HLExchange.__new__(HLExchange)
    exchange._hl_client = mock_client

    result = await exchange.get_daily_candles("ETH", days=5)
    assert len(result) == 5
    # Must be the LAST 5 candles.
    assert result[0][1] == pytest.approx(5.0)
    assert result[-1][1] == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_get_daily_candles_empty_response():
    """If HL returns no candles, get_daily_candles returns []."""
    mock_client = MagicMock()
    mock_client.candle_snapshot = AsyncMock(return_value=[])
    mock_client.aclose = AsyncMock()

    exchange = HLExchange.__new__(HLExchange)
    exchange._hl_client = mock_client

    result = await exchange.get_daily_candles("BTC", days=30)
    assert result == []
