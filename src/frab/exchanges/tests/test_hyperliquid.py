"""Tests for HLMarketData."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from tenacity import wait_none

import frab.exchanges.hyperliquid as hl_mod
from frab.exchanges.base import Leg, Side
from frab.exchanges.hyperliquid import HLMarketData, _ms_to_dt

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
    assert tick.ts == _ms_to_dt(1_700_000_000_000)
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
    assert ticks == sorted(ticks, key=lambda t: t.ts)
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
    assert quote.ts == _ms_to_dt(1_700_000_000_000)
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


# ---------------------------------------------------------------------------
# fetch_user_fills
# ---------------------------------------------------------------------------

def _fill_record(
    coin: str,
    time_ms: int,
    side: str,
    sz: str,
    px: str,
    fee: str,
    fee_token: str,
    oid: int,
    tid: int,
) -> dict:
    return {
        "coin": coin,
        "time": time_ms,
        "side": side,
        "sz": sz,
        "px": px,
        "fee": fee,
        "feeToken": fee_token,
        "oid": oid,
        "tid": tid,
    }


@pytest.mark.asyncio
async def test_fetch_user_fills_parses_and_normalizes(mocker):
    """3-fill response: spot BUY (UBTC/USDC), perp SELL (ETH), perp BUY (BTC)."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    # ts values: fill2 is earliest to verify sort order
    t1_ms = 1_700_000_002_000  # spot BUY
    t2_ms = 1_700_000_000_000  # perp SELL — earliest
    t3_ms = 1_700_000_001_000  # perp BUY

    fills_response = [
        _fill_record("UBTC/USDC", t1_ms, "B", "0.001", "80000.0", "0.00001", "UBTC", 1001, 2001),
        _fill_record("ETH", t2_ms, "A", "0.5", "3000.0", "0.75", "USDC", 1002, 2002),
        _fill_record("BTC", t3_ms, "B", "0.01", "79000.0", "1.58", "USDC", 1003, 2003),
    ]

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=fills_response)
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_fills("0xABCD", since_ms=0)

    assert len(result) == 3

    # Verify sort order: ascending by ts
    assert result[0].ts == _ms_to_dt(t2_ms)  # ETH perp SELL
    assert result[1].ts == _ms_to_dt(t3_ms)  # BTC perp BUY
    assert result[2].ts == _ms_to_dt(t1_ms)  # UBTC/USDC spot BUY

    # Spot BUY (UBTC/USDC → BTC)
    spot_fill = result[2]
    assert spot_fill.coin == "BTC"
    assert spot_fill.leg == Leg.SPOT
    assert spot_fill.side == Side.BUY
    assert spot_fill.qty == pytest.approx(0.001)
    assert spot_fill.price == pytest.approx(80000.0)
    assert spot_fill.fee == pytest.approx(0.00001)
    assert spot_fill.fee_token == "UBTC"
    assert spot_fill.hl_oid == 1001
    assert spot_fill.hl_tid == 2001

    # Perp SELL (ETH)
    perp_sell = result[0]
    assert perp_sell.coin == "ETH"
    assert perp_sell.leg == Leg.PERP
    assert perp_sell.side == Side.SELL
    assert perp_sell.qty == pytest.approx(0.5)
    assert perp_sell.fee_token == "USDC"

    # Perp BUY (BTC)
    perp_buy = result[1]
    assert perp_buy.coin == "BTC"
    assert perp_buy.leg == Leg.PERP
    assert perp_buy.side == Side.BUY


@pytest.mark.asyncio
async def test_fetch_user_fills_retries_on_5xx(mocker):
    """500 on first attempt, 200 on second — retry should kick in."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    fill = _fill_record("BTC", 1_700_000_000_000, "B", "0.01", "50000", "0.5", "USDC", 1, 1)
    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=[fill])

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_fills("0xABCD", since_ms=0)

    assert call_count == 2
    assert len(result) == 1


@pytest.mark.asyncio
async def test_fetch_user_fills_empty_returns_empty_list(mocker):
    """Empty array response → empty list, no error."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[])
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_fills("0xABCD", since_ms=0)

    assert result == []


@pytest.mark.asyncio
async def test_fetch_user_fills_unknown_spot_coin_fallback(mocker):
    """Spot coin with no known mapping falls back to the part before the slash."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    fill = _fill_record("PURR/USDC", 1_700_000_000_000, "B", "100.0", "1.0", "0.1", "USDC", 1, 1)

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[fill])
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_fills("0xABCD", since_ms=0)

    assert len(result) == 1
    assert result[0].coin == "PURR"
    assert result[0].leg == Leg.SPOT


@pytest.mark.asyncio
async def test_fetch_user_fills_resolves_at_index_format(mocker):
    """HL also returns spot fills as '@<idx>' — must resolve via spotMeta."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    spot_meta = {
        "universe": [
            {"index": 142, "name": "UBTC/USDC", "tokens": [142, 0], "isCanonical": True},
            {"index": 0, "name": "PURR/USDC", "tokens": [1, 0], "isCanonical": True},
        ],
        "tokens": [],
    }
    fill = _fill_record("@142", 1_700_000_000_000, "B", "0.00015", "76800.0", "0.00000011", "UBTC", 1, 1)

    async def routed(request):
        body = json.loads(request.content)
        if body.get("type") == "spotMeta":
            return httpx.Response(200, json=spot_meta)
        if body.get("type") == "userFillsByTime":
            return httpx.Response(200, json=[fill])
        return httpx.Response(400)

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=routed)
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_fills("0xABCD", since_ms=0)

    assert len(result) == 1
    assert result[0].coin == "BTC"
    assert result[0].leg == Leg.SPOT


# ---------------------------------------------------------------------------
# fetch_user_funding
# ---------------------------------------------------------------------------


def _user_funding_record(coin: str, time_ms: int, usdc: str, szi: str, rate: str, hash_: str = "0xH") -> dict:
    return {
        "time": time_ms,
        "hash": hash_,
        "delta": {
            "type": "funding",
            "coin": coin,
            "usdc": usdc,
            "szi": szi,
            "fundingRate": rate,
            "nSamples": None,
        },
    }


@pytest.mark.asyncio
async def test_fetch_user_funding_parses_and_sorts(mocker):
    """3 funding payments — positive and negative; sorted ascending; signs preserved."""
    mocker.patch.object(hl_mod, "_WAIT", wait_none())

    t1 = 1_700_000_002_000  # latest
    t2 = 1_700_000_000_000  # earliest
    t3 = 1_700_000_001_000  # middle

    payments_response = [
        _user_funding_record("BTC", t1, "0.000150", "-0.00015", "0.0000125"),
        _user_funding_record("BTC", t2, "0.000140", "-0.00014", "0.0000125"),
        _user_funding_record("BTC", t3, "-0.000050", "0.00015", "-0.0000045"),  # we paid
    ]

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payments_response)
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_funding("0xABCD", since_ms=0)

    assert len(result) == 3
    # Ascending by ts
    assert result[0].ts == _ms_to_dt(t2)
    assert result[1].ts == _ms_to_dt(t3)
    assert result[2].ts == _ms_to_dt(t1)
    # Sign preservation
    assert result[1].usdc == pytest.approx(-0.000050)
    assert result[2].usdc == pytest.approx(0.000150)


@pytest.mark.asyncio
async def test_fetch_user_funding_retries_on_5xx(mocker):
    mocker.patch.object(hl_mod, "_WAIT", wait_none())
    payments_response = [_user_funding_record("BTC", 1_700_000_000_000, "0.0001", "-0.00015", "0.0000125")]

    async with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/info")
        route.side_effect = [
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=payments_response),
        ]
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_funding("0xABCD", since_ms=0)

    assert len(result) == 1
    assert route.call_count == 2  # one retry


@pytest.mark.asyncio
async def test_fetch_user_funding_empty_returns_empty_list(mocker):
    mocker.patch.object(hl_mod, "_WAIT", wait_none())
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=[])
        client = httpx.AsyncClient(base_url=BASE_URL)
        md = HLMarketData(api_url=INFO_URL, client=client)
        result = await md.fetch_user_funding("0xABCD", since_ms=0)

    assert result == []
