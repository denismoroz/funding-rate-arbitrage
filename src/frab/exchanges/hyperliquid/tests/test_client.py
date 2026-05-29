"""Unit tests for HLClient (transport + typed wire layer)."""
from __future__ import annotations

import pytest
import httpx
import respx

import frab.exchanges.hyperliquid.client as hl_client_mod
from frab.exchanges.hyperliquid.client import HLClient, HLTransferError
from frab.exchanges.hyperliquid.wire import (
    HLFundingDelta,
    HLFundingRecord,
    HLL2Snapshot,
    HLOrderResponse,
    HLOrderStatus,
    HLPerpMarketSpec,
    HLPerpState,
    HLSpotMeta,
    HLSpotState,
    HLUserFill,
)
from tenacity import wait_none

BASE_URL = "https://api.hyperliquid.xyz"
INFO_URL = f"{BASE_URL}/info"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SENTINEL = object()


@pytest.fixture
def make_client(mocker):
    """Factory that returns an HLClient with respx-compatible httpx client + SDK mocks.

    Pass exchange=None to get a client without an exchange handle (auth guard testing).
    By default, exchange and info are MagicMocks.
    """
    def _make(*, exchange=_SENTINEL, info_obj=None):
        http = httpx.AsyncClient(base_url=BASE_URL)
        info = info_obj if info_obj is not None else mocker.MagicMock()
        if exchange is _SENTINEL:
            ex = mocker.MagicMock()
        else:
            ex = exchange  # None is valid here to test auth guards
        return HLClient(client=http, info=info, exchange=ex)
    return _make


# ---------------------------------------------------------------------------
# 1. test_info_request_happy_path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_request_happy_path(make_client):
    client = make_client()
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json={"hello": "world"})
        result = await client.info_request({"type": "test"})
    assert result == {"hello": "world"}
    await client.aclose()


# ---------------------------------------------------------------------------
# 2. test_info_request_retries_on_5xx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_request_retries_on_5xx(mocker, make_client):
    mocker.patch.object(hl_client_mod, "_WAIT", wait_none())
    client = make_client()
    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        result = await client.info_request({"type": "x"})

    assert result == {"ok": True}
    assert call_count == 3
    await client.aclose()


# ---------------------------------------------------------------------------
# 3. test_info_request_retries_on_connect_error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_request_retries_on_connect_error(mocker, make_client):
    mocker.patch.object(hl_client_mod, "_WAIT", wait_none())
    client = make_client()
    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"data": 42})

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        result = await client.info_request({"type": "x"})

    assert result == {"data": 42}
    assert call_count == 3
    await client.aclose()


# ---------------------------------------------------------------------------
# 4. test_all_mids_returns_dict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_mids_returns_dict(make_client):
    client = make_client()
    payload = {"BTC": "30000.5", "ETH": "2000.0"}
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        result = await client.all_mids()
    assert isinstance(result, dict)
    assert result["BTC"] == pytest.approx(30000.5)
    assert result["ETH"] == pytest.approx(2000.0)
    await client.aclose()


# ---------------------------------------------------------------------------
# 5. test_l2_book_parses_bid_ask
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l2_book_parses_bid_ask(make_client):
    client = make_client()
    book = {
        "coin": "BTC",
        "time": 1_700_000_000_000,
        "levels": [
            [{"px": "29999.0", "sz": "1.5", "n": 3}],
            [{"px": "30001.0", "sz": "2.0", "n": 2}],
        ],
    }
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=book)
        snap = await client.l2_book("BTC")

    assert isinstance(snap, HLL2Snapshot)
    assert snap.bid == pytest.approx(29999.0)
    assert snap.ask == pytest.approx(30001.0)
    assert snap.ts_ms == 1_700_000_000_000
    await client.aclose()


# ---------------------------------------------------------------------------
# 6. test_l2_book_falls_back_to_zero_when_empty_levels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l2_book_falls_back_to_zero_when_empty_levels(make_client):
    """When levels are empty (no bids/asks), bid/ask are 0.0 — caller uses mark."""
    client = make_client()
    book = {"coin": "BTC", "time": 1234567890, "levels": [[], []]}
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=book)
        snap = await client.l2_book("BTC")

    assert snap.bid == pytest.approx(0.0)
    assert snap.ask == pytest.approx(0.0)
    assert snap.ts_ms == 1234567890
    await client.aclose()


# ---------------------------------------------------------------------------
# 7. test_perp_meta_parses_sz_decimals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perp_meta_parses_sz_decimals(make_client):
    client = make_client()
    payload = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
    ]}
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        specs = await client.perp_meta()

    assert len(specs) == 2
    assert isinstance(specs[0], HLPerpMarketSpec)
    assert specs[0].name == "BTC"
    assert specs[0].sz_decimals == 5
    assert specs[1].name == "ETH"
    assert specs[1].sz_decimals == 4
    await client.aclose()


# ---------------------------------------------------------------------------
# 8. test_spot_meta_resolves_pair_names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_meta_resolves_pair_names(make_client):
    client = make_client()
    payload = {
        "tokens": [
            {"index": 0, "name": "USDC"},
            {"index": 1, "name": "UBTC"},
        ],
        "universe": [
            {"index": 142, "name": "@142", "tokens": [1, 0]},
        ],
    }
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        meta = await client.spot_meta()

    assert isinstance(meta, HLSpotMeta)
    assert 0 in meta.tokens
    assert 1 in meta.tokens
    assert meta.tokens[1] == "UBTC"
    assert len(meta.pairs) == 1
    assert meta.pairs[0].index == 142
    assert meta.pairs[0].name == "UBTC/USDC"
    await client.aclose()


# ---------------------------------------------------------------------------
# 9. test_spot_meta_keeps_explicit_pair_name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_meta_keeps_explicit_pair_name(make_client):
    """Universe entry with '/' in name is used as-is."""
    client = make_client()
    payload = {
        "tokens": [
            {"index": 0, "name": "USDC"},
            {"index": 1, "name": "UBTC"},
        ],
        "universe": [
            {"index": 142, "name": "UBTC/USDC", "tokens": [1, 0]},
        ],
    }
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        meta = await client.spot_meta()

    assert meta.pairs[0].name == "UBTC/USDC"
    await client.aclose()


# ---------------------------------------------------------------------------
# 10. test_funding_history_paginates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funding_history_paginates(mocker, make_client):
    mocker.patch.object(hl_client_mod, "_WAIT", wait_none())
    client = make_client()

    def _make_record(ts_ms: int) -> dict:
        return {"coin": "BTC", "time": ts_ms, "fundingRate": "0.0001", "premium": "0.0005"}

    page1 = [_make_record(i) for i in range(500)]
    page2 = [_make_record(500 + i) for i in range(200)]
    call_count = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=page1)
        elif call_count == 2:
            return httpx.Response(200, json=page2)
        else:
            pytest.fail("unexpected third call")

    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").mock(side_effect=side_effect)
        records = await client.funding_history("BTC", since_ms=0)

    assert len(records) == 700
    assert records == sorted(records, key=lambda r: r.ts_ms)
    assert isinstance(records[0], HLFundingRecord)
    await client.aclose()


# ---------------------------------------------------------------------------
# 11. test_user_funding_parses_deltas
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_funding_parses_deltas(make_client):
    client = make_client()
    payload = [
        {"time": 1_000_000, "delta": {"coin": "BTC", "usdc": "1.5", "type": "funding"}},
        {"time": 2_000_000, "delta": {"coin": "ETH", "usdc": "0.5", "type": "funding"}},
    ]
    async with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/info").respond(200, json=payload)
        deltas = await client.user_funding("0xabc", since_ms=0)

    assert len(deltas) == 2
    assert isinstance(deltas[0], HLFundingDelta)
    assert deltas[0].coin == "BTC"
    assert deltas[0].ts_ms == 1_000_000
    assert deltas[0].amount_usdc == pytest.approx(1.5)
    assert deltas[1].coin == "ETH"
    await client.aclose()


# ---------------------------------------------------------------------------
# 12. test_user_state_parses_perp_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_state_parses_perp_state(mocker, make_client):
    fake_info = mocker.MagicMock()
    fake_raw = {
        "marginSummary": {"accountValue": "1234.56"},
        "assetPositions": [
            {"position": {
                "coin": "BTC",
                "szi": "0.1",
                "unrealizedPnl": "50.0",
                "cumFunding": {"sinceOpen": "-3.5"},
            }}
        ],
    }
    fake_info.user_state.return_value = fake_raw
    client = make_client(info_obj=fake_info)

    state = await client.user_state("0xaddr")

    assert isinstance(state, HLPerpState)
    assert state.account_value == pytest.approx(1234.56)
    assert len(state.asset_positions) == 1
    ap = state.asset_positions[0]
    assert ap.coin == "BTC"
    assert ap.szi == pytest.approx(0.1)
    assert ap.unrealized_pnl == pytest.approx(50.0)
    assert ap.cum_funding_since_open == pytest.approx(-3.5)
    # New fields default when absent
    assert ap.margin_used == pytest.approx(0.0)
    assert ap.position_value == pytest.approx(0.0)
    assert ap.leverage_value is None
    await client.aclose()


# ---------------------------------------------------------------------------
# 12b. test_user_state_parses_extended_position_fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_state_parses_extended_position_fields(mocker, make_client):
    fake_info = mocker.MagicMock()
    fake_raw = {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [
            {"position": {
                "coin": "BTC",
                "szi": "-0.5",
                "unrealizedPnl": "10.0",
                "cumFunding": {"sinceOpen": "-1.0"},
                "marginUsed": "200.5",
                "positionValue": "30000.0",
                "leverage": {"type": "cross", "value": 3},
            }}
        ],
    }
    fake_info.user_state.return_value = fake_raw
    client = make_client(info_obj=fake_info)

    state = await client.user_state("0xaddr")

    ap = state.asset_positions[0]
    assert ap.margin_used == pytest.approx(200.5)
    assert ap.position_value == pytest.approx(30000.0)
    assert ap.leverage_value == 3
    await client.aclose()


# ---------------------------------------------------------------------------
# 12c. test_user_state_extended_fields_malformed_inputs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_state_extended_fields_malformed_inputs(mocker, make_client):
    """Malformed / missing nested leverage keys → safe defaults."""
    fake_info = mocker.MagicMock()

    # leverage present but value key missing
    fake_raw_no_value = {
        "marginSummary": {"accountValue": "100.0"},
        "assetPositions": [
            {"position": {
                "coin": "ETH",
                "szi": "1.0",
                "unrealizedPnl": "0.0",
                "cumFunding": {"sinceOpen": "0.0"},
                "leverage": {"type": "cross"},
            }}
        ],
    }
    fake_info.user_state.return_value = fake_raw_no_value
    client = make_client(info_obj=fake_info)
    state = await client.user_state("0xaddr")
    assert state.asset_positions[0].leverage_value is None
    await client.aclose()

    # leverage value not int-able
    fake_raw_bad_value = {
        "marginSummary": {"accountValue": "100.0"},
        "assetPositions": [
            {"position": {
                "coin": "ETH",
                "szi": "1.0",
                "unrealizedPnl": "0.0",
                "cumFunding": {"sinceOpen": "0.0"},
                "leverage": {"type": "cross", "value": "abc"},
            }}
        ],
    }
    fake_info.user_state.return_value = fake_raw_bad_value
    client2 = make_client(info_obj=fake_info)
    state2 = await client2.user_state("0xaddr")
    assert state2.asset_positions[0].leverage_value is None
    await client2.aclose()


# ---------------------------------------------------------------------------
# 13. test_spot_user_state_returns_all_balances
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_user_state_returns_all_balances(mocker, make_client):
    """Client returns ALL balances including zero-balance ones; caller filters."""
    fake_info = mocker.MagicMock()
    fake_info.spot_user_state.return_value = {
        "balances": [
            {"coin": "USDC", "total": "500.0", "hold": "100.0"},
            {"coin": "UBTC", "total": "0.0", "hold": "0.0"},
            {"coin": "UETH", "total": "1.5", "hold": "0.0"},
        ]
    }
    client = make_client(info_obj=fake_info)

    state = await client.spot_user_state("0xaddr")

    assert isinstance(state, HLSpotState)
    assert len(state.balances) == 3
    coins = {b.coin for b in state.balances}
    assert coins == {"USDC", "UBTC", "UETH"}
    usdc = next(b for b in state.balances if b.coin == "USDC")
    assert usdc.total == pytest.approx(500.0)
    assert usdc.hold == pytest.approx(100.0)
    await client.aclose()


# ---------------------------------------------------------------------------
# 14. test_user_fills_by_time_passes_end_ms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_fills_by_time_passes_end_ms(mocker, make_client):
    """When end_ms provided, SDK is called with (addr, since, end)."""
    fake_info = mocker.MagicMock()
    fake_info.user_fills_by_time.return_value = []
    client = make_client(info_obj=fake_info)

    await client.user_fills_by_time("0xaddr", 1000, 2000)
    fake_info.user_fills_by_time.assert_called_once()
    args = fake_info.user_fills_by_time.call_args[0]
    assert args == ("0xaddr", 1000, 2000)
    await client.aclose()


@pytest.mark.asyncio
async def test_user_fills_by_time_omits_end_ms_when_none(mocker, make_client):
    """When end_ms is None, SDK is called with only (addr, since)."""
    fake_info = mocker.MagicMock()
    fake_info.user_fills_by_time.return_value = []
    client = make_client(info_obj=fake_info)

    await client.user_fills_by_time("0xaddr", 1000)
    fake_info.user_fills_by_time.assert_called_once()
    args = fake_info.user_fills_by_time.call_args[0]
    assert args == ("0xaddr", 1000)
    await client.aclose()


# ---------------------------------------------------------------------------
# 15. test_market_open_filled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_filled(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.5", "avgPx": "100.0", "oid": 42, "fee": "0.05"}}
        ]}}
    }
    client = make_client()
    client._exchange = fake_exchange

    resp = await client.market_open("BTC", True, 0.5, 0.01)

    assert isinstance(resp, HLOrderResponse)
    assert len(resp.statuses) == 1
    s0 = resp.first
    assert s0.filled is not None
    assert s0.filled.qty == pytest.approx(0.5)
    assert s0.filled.price == pytest.approx(100.0)
    assert s0.filled.oid == 42
    assert s0.filled.fee_usdc == pytest.approx(0.05)
    await client.aclose()


# ---------------------------------------------------------------------------
# 16. test_market_open_filled_without_fee
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_filled_without_fee(mocker, make_client):
    """When 'fee' key is absent, fee_usdc is None."""
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.5", "avgPx": "100.0", "oid": 42}}
        ]}}
    }
    client = make_client()
    client._exchange = fake_exchange

    resp = await client.market_open("BTC", True, 0.5, 0.01)

    assert resp.first.filled is not None
    assert resp.first.filled.fee_usdc is None
    await client.aclose()


# ---------------------------------------------------------------------------
# 17. test_market_open_error_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_error_status(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}
    }
    client = make_client()
    client._exchange = fake_exchange

    resp = await client.market_open("BTC", True, 0.5, 0.01)

    assert resp.first.filled is None
    assert resp.first.error == "insufficient margin"
    await client.aclose()


# ---------------------------------------------------------------------------
# 18. test_market_open_resting_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_resting_status(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 99}}]}}
    }
    client = make_client()
    client._exchange = fake_exchange

    resp = await client.market_open("BTC", True, 0.5, 0.01)

    assert resp.first.resting_oid == 99
    await client.aclose()


# ---------------------------------------------------------------------------
# 19. test_market_open_rejected_raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_rejected_raises(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {
        "status": "err",
        "response": "insufficient balance"
    }
    client = make_client()
    client._exchange = fake_exchange

    with pytest.raises(RuntimeError):
        await client.market_open("BTC", True, 0.5, 0.01)
    await client.aclose()


# ---------------------------------------------------------------------------
# 20. test_market_open_malformed_shape_raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_malformed_shape_raises(mocker, make_client):
    """Missing 'response' key → RuntimeError."""
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_open.return_value = {"status": "ok"}
    client = make_client()
    client._exchange = fake_exchange

    with pytest.raises(RuntimeError):
        await client.market_open("BTC", True, 0.5, 0.01)
    await client.aclose()


# ---------------------------------------------------------------------------
# 21. test_market_open_requires_exchange
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_open_requires_exchange(make_client):
    """HLClient(exchange=None) → market_open raises RuntimeError."""
    client = make_client(exchange=None)  # None → no exchange handle

    with pytest.raises(RuntimeError, match="require `exchange`"):
        await client.market_open("BTC", True, 0.5, 0.01)
    await client.aclose()


# ---------------------------------------------------------------------------
# 22. test_market_close_parses_filled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_close_parses_filled(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.market_close.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.3", "avgPx": "95000.0", "oid": 77, "fee": "0.02"}}
        ]}}
    }
    client = make_client()
    client._exchange = fake_exchange

    resp = await client.market_close("BTC", 0.01)

    assert resp.first.filled is not None
    assert resp.first.filled.qty == pytest.approx(0.3)
    assert resp.first.filled.price == pytest.approx(95000.0)
    assert resp.first.filled.oid == 77
    assert resp.first.filled.fee_usdc == pytest.approx(0.02)
    await client.aclose()


# ---------------------------------------------------------------------------
# 23. test_update_leverage_calls_sdk_with_cross_true
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_leverage_calls_sdk_with_cross_true(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.update_leverage.return_value = {"status": "ok"}
    client = make_client()
    client._exchange = fake_exchange

    await client.update_leverage("BTC", 5)

    fake_exchange.update_leverage.assert_called_once()
    args = fake_exchange.update_leverage.call_args[0]
    assert args[0] == 5       # int(leverage)
    assert args[1] == "BTC"   # coin
    assert args[2] is True    # cross-margin=True
    await client.aclose()


# ---------------------------------------------------------------------------
# 24. test_update_leverage_rejected_raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_leverage_rejected_raises(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.update_leverage.return_value = {"status": "err"}
    client = make_client()
    client._exchange = fake_exchange

    with pytest.raises(RuntimeError):
        await client.update_leverage("BTC", 5)
    await client.aclose()


# ---------------------------------------------------------------------------
# 25. test_usd_class_transfer_to_perp_true
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usd_class_transfer_to_perp_true(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.usd_class_transfer.return_value = {"status": "ok"}
    client = make_client()
    client._exchange = fake_exchange

    await client.usd_class_transfer(100.0, True)

    fake_exchange.usd_class_transfer.assert_called_once()
    args = fake_exchange.usd_class_transfer.call_args[0]
    assert args[0] == pytest.approx(100.0)
    assert args[1] is True
    await client.aclose()


# ---------------------------------------------------------------------------
# 26. test_usd_class_transfer_rejected_raises_hltransfererror
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usd_class_transfer_rejected_raises_hltransfererror(mocker, make_client):
    fake_exchange = mocker.MagicMock()
    fake_exchange.usd_class_transfer.return_value = {"status": "err", "response": "nope"}
    client = make_client()
    client._exchange = fake_exchange

    with pytest.raises(HLTransferError):
        await client.usd_class_transfer(50.0, False)
    await client.aclose()


# ---------------------------------------------------------------------------
# 27. test_aclose_closes_owned_client_only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aclose_closes_owned_client_only(mocker):
    """When httpx client is injected, aclose does NOT close it.
    When HLClient creates its own, aclose DOES close it.
    """
    # Injected: should NOT be closed
    ext_http = httpx.AsyncClient(base_url=BASE_URL)
    client_injected = HLClient(client=ext_http)
    await client_injected.aclose()
    assert not ext_http.is_closed
    await ext_http.aclose()  # cleanup

    # Owned: should be closed
    client_owned = HLClient(api_url=INFO_URL)
    owned_http = client_owned._http
    await client_owned.aclose()
    assert owned_http.is_closed
