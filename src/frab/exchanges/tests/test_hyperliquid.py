"""Tests for HLMarketData."""
from __future__ import annotations

import httpx
import pytest
import respx
from tenacity import wait_none

import frab.exchanges.hyperliquid as hl_mod
from frab.exchanges.hyperliquid import HLMarketData

BASE_URL = "https://api.hyperliquid.xyz"
INFO_URL = f"{BASE_URL}/info"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _funding_record(ts_ms: int = 1_000_000, rate: str = "0.0001", premium: str = "0.001") -> dict:
    return {"coin": "BTC", "time": ts_ms, "fundingRate": rate, "premium": premium}


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL)


# ---------------------------------------------------------------------------
# fetch_funding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_funding_happy_path():
    record = _funding_record(ts_ms=1_700_000_000_000, rate="0.0001", premium="0.0005")
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[record])
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        tick = await md.fetch_funding("BTC")

    assert tick.coin == "BTC"
    assert tick.ts_ms == 1_700_000_000_000
    assert tick.rate == pytest.approx(0.0001)
    assert tick.premium == pytest.approx(0.0005)
    assert tick.annualized_pct == pytest.approx(0.0001 * 8760 * 100)


@pytest.mark.asyncio
async def test_fetch_funding_empty_raises():
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[])
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        with pytest.raises(ValueError, match="no recent funding"):
            await md.fetch_funding("BTC")


@pytest.mark.asyncio
async def test_fetch_funding_parses_string_fields():
    record = _funding_record(rate="0.0001", premium="0.0002")
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[record])
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        tick = await md.fetch_funding("BTC")

    assert isinstance(tick.rate, float)
    assert tick.rate == pytest.approx(0.0001)
    assert isinstance(tick.premium, float)


# ---------------------------------------------------------------------------
# fetch_funding_history — pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_funding_history_paginates(mocker):
    page1 = [_funding_record(ts_ms=i) for i in range(500)]
    page2 = [_funding_record(ts_ms=500 + i) for i in range(200)]
    last_ts_page1 = page1[-1]["time"]  # 499

    call_bodies: list[dict] = []

    async with respx.mock(base_url=BASE_URL) as mock:
        call_count = 0

        async def side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            body = __import__("json").loads(request.content)
            call_bodies.append(body)
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=page1)
            elif call_count == 2:
                return httpx.Response(200, json=page2)
            else:
                pytest.fail("unexpected third call")

        mock.post("/info").mock(side_effect=side_effect)
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        ticks = await md.fetch_funding_history("BTC", since_ms=0)

    assert len(ticks) == 700
    assert ticks == sorted(ticks, key=lambda t: t.ts_ms)
    assert call_bodies[1]["startTime"] == last_ts_page1 + 1


# ---------------------------------------------------------------------------
# fetch_quote
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_quote_combines_endpoints():
    mids = {"BTC": "30000.5", "ETH": "2000.0"}
    book = {
        "coin": "BTC",
        "time": 1_700_000_000_000,
        "levels": [
            [{"px": "29999.0", "sz": "1.5", "n": 3}],
            [{"px": "30001.0", "sz": "2.0", "n": 2}],
        ],
    }

    calls: list[str] = []

    async def side_effect(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        t = body["type"]
        calls.append(t)
        if t == "allMids":
            return httpx.Response(200, json=mids)
        elif t == "l2Book":
            return httpx.Response(200, json=book)
        pytest.fail(f"unexpected type: {t}")

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        quote = await md.fetch_quote("BTC")

    assert quote.coin == "BTC"
    assert quote.ts_ms == 1_700_000_000_000
    assert quote.bid == pytest.approx(29999.0)
    assert quote.ask == pytest.approx(30001.0)
    assert quote.mark == pytest.approx(30000.5)
    assert quote.spot is None
    assert "allMids" in calls
    assert "l2Book" in calls


# ---------------------------------------------------------------------------
# fetch_meta
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_meta_parses_universe():
    universe = {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "LOWDEC", "szDecimals": 3, "maxLeverage": 20},
        ]
    }
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=universe)
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        specs = await md.fetch_meta()

    assert len(specs) == 2
    btc = specs[0]
    assert btc.coin == "BTC"
    assert btc.has_spot is False
    assert btc.has_perp is True
    assert btc.min_size == pytest.approx(10 ** -5)
    assert btc.tick_size == pytest.approx(10 ** -(6 - 5))  # 0.1

    low = specs[1]
    assert low.min_size == pytest.approx(10 ** -3)
    assert low.tick_size == pytest.approx(10 ** -(6 - 3))  # 0.001


# ---------------------------------------------------------------------------
# Retry on 5xx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_5xx(mocker):
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    record = _funding_record()
    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[record])

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        tick = await md.fetch_funding("BTC")

    assert tick.coin == "BTC"
    assert call_count == 3


# ---------------------------------------------------------------------------
# No retry on 4xx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_retry_on_4xx(mocker):
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400)

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        client = _make_client()
        md = HLMarketData(api_url=INFO_URL, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await md.fetch_funding("BTC")

    assert call_count == 1


# ---------------------------------------------------------------------------
# Owned client lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owned_client_closed_on_exit(mocker):
    record = _funding_record()
    async with respx.mock(base_url=BASE_URL):
        respx.post(INFO_URL).respond(200, json=[record])
        md = HLMarketData(api_url=INFO_URL)
        spy = mocker.spy(md._client, "aclose")
        async with md:
            pass
        spy.assert_called_once()
        assert md._client.is_closed


# ---------------------------------------------------------------------------
# Injected client not closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_injected_client_not_closed():
    ext_client = httpx.AsyncClient(base_url=BASE_URL)
    md = HLMarketData(api_url=INFO_URL, client=ext_client)
    await md.aclose()
    assert not ext_client.is_closed
    await ext_client.aclose()
