"""Unit tests for HLSymbols — coin normalization, sz_decimals cache, qty rounding."""
from __future__ import annotations

import logging

import pytest
from unittest.mock import AsyncMock

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import (
    HLPerpMarketSpec,
    HLSpotMeta,
    HLSpotPair,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.perp_meta = AsyncMock(return_value=[
        HLPerpMarketSpec(name="BTC", sz_decimals=5),
    ])
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={142: "UBTC"},
        pairs=[HLSpotPair(index=142, name="UBTC/USDC")],
    ))
    return client


@pytest.fixture()
def symbols(mock_client):
    return HLSymbols(
        client=mock_client,
        spot_token_map={"BTC": "UBTC"},
        spot_quote_token="USDC",
    )


# ---------------------------------------------------------------------------
# 1. make_spot_name — no token map
# ---------------------------------------------------------------------------

def test_make_spot_name_default_no_token_map(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map=None)
    assert sym.make_spot_name("BTC") == "BTC/USDC"


# ---------------------------------------------------------------------------
# 2. make_spot_name — with token map
# ---------------------------------------------------------------------------

def test_make_spot_name_with_token_map(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    assert sym.make_spot_name("BTC") == "UBTC/USDC"


# ---------------------------------------------------------------------------
# 3. make_spot_name — custom quote token
# ---------------------------------------------------------------------------

def test_make_spot_name_custom_quote_token(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={}, spot_quote_token="USD")
    assert sym.make_spot_name("BTC") == "BTC/USD"


# ---------------------------------------------------------------------------
# 4. normalize_spot_coin — inverse of token map
# ---------------------------------------------------------------------------

def test_normalize_spot_coin_inverse_of_token_map(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    assert sym.normalize_spot_coin("UBTC") == "BTC"


# ---------------------------------------------------------------------------
# 5. normalize_spot_coin — unknown passes through
# ---------------------------------------------------------------------------

def test_normalize_spot_coin_unknown_passes_through(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={})
    assert sym.normalize_spot_coin("FOO") == "FOO"


# ---------------------------------------------------------------------------
# 6. spot_token_inverse property
# ---------------------------------------------------------------------------

def test_spot_token_inverse_property(mock_client):
    """spot_token_inverse returns the registry-derived dict passed at construction."""
    registry_inverse = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL"}
    sym = HLSymbols(client=mock_client, spot_token_inverse=registry_inverse)
    inv = sym.spot_token_inverse
    assert inv["UBTC"] == "BTC"
    assert inv["UETH"] == "ETH"
    assert inv["USOL"] == "SOL"


def test_spot_token_inverse_property_default_empty(mock_client):
    """Without a registry-derived inverse, spot_token_inverse is empty (no module const fallback)."""
    sym = HLSymbols(client=mock_client)
    assert sym.spot_token_inverse == {}


# ---------------------------------------------------------------------------
# 7. sz_decimals — loads from client on first call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sz_decimals_loads_from_client_on_first_call(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    result = await sym.sz_decimals("BTC")
    assert result == 5
    assert mock_client.perp_meta.call_count == 1


# ---------------------------------------------------------------------------
# 8. sz_decimals — uses cache on second call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sz_decimals_uses_cache_on_second_call(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    await sym.sz_decimals("BTC")
    await sym.sz_decimals("BTC")
    assert mock_client.perp_meta.call_count == 1


# ---------------------------------------------------------------------------
# 9. sz_decimals — unknown coin raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sz_decimals_unknown_coin_raises_value_error(mock_client):
    sym = HLSymbols(client=mock_client)
    with pytest.raises(ValueError, match="unknown coin"):
        await sym.sz_decimals("DOGE")


# ---------------------------------------------------------------------------
# 10. round_qty — floors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_qty_floors(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    result = await sym.round_qty("BTC", 0.123456789)
    assert result == pytest.approx(0.12345)


# ---------------------------------------------------------------------------
# 11. round_qty_to_nearest — ROUND_HALF_UP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_qty_to_nearest_half_up(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    # 0.123456 at sz_decimals=5: 5th decimal is 5, 6th is 6 → rounds up to 0.12346
    result = await sym.round_qty_to_nearest("BTC", 0.123456)
    assert result == pytest.approx(0.12346)


# ---------------------------------------------------------------------------
# 12. round_qty — below step returns zero
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_qty_below_step_returns_zero(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_map={"BTC": "UBTC"})
    result = await sym.round_qty("BTC", 0.000001)
    assert result == 0.0


# ---------------------------------------------------------------------------
# 13. resolve_spot_pair — returns name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spot_pair_returns_name(mock_client):
    sym = HLSymbols(client=mock_client)
    result = await sym.resolve_spot_pair(142)
    assert result == "UBTC/USDC"


# ---------------------------------------------------------------------------
# 14. resolve_spot_pair — unknown returns None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spot_pair_unknown_returns_none(mock_client):
    sym = HLSymbols(client=mock_client)
    result = await sym.resolve_spot_pair(999)
    assert result is None


# ---------------------------------------------------------------------------
# 15. resolve_spot_pair — skips pairs without slash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spot_pair_skips_pairs_without_slash(mock_client, mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={},
        pairs=[HLSpotPair(index=10, name="FOO")],  # no slash → not cached
    ))
    sym = HLSymbols(client=client)
    result = await sym.resolve_spot_pair(10)
    assert result is None


# ---------------------------------------------------------------------------
# 16. resolve_spot_pair — caches spot_meta
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spot_pair_caches_spot_meta(mock_client):
    sym = HLSymbols(client=mock_client)
    await sym.resolve_spot_pair(142)
    await sym.resolve_spot_pair(142)
    assert mock_client.spot_meta.call_count == 1


# ---------------------------------------------------------------------------
# 17. normalize_hl_coin — perp ticker passthrough
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_perp_ticker_passthrough(mock_client):
    sym = HLSymbols(client=mock_client)
    coin, leg = await sym.normalize_hl_coin("BTC")
    assert coin == "BTC"
    assert leg == "perp"


# ---------------------------------------------------------------------------
# 18. normalize_hl_coin — @idx resolves to spot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_at_idx_resolves_to_spot(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_inverse={"UBTC": "BTC"})
    coin, leg = await sym.normalize_hl_coin("@142")
    assert coin == "BTC"
    assert leg == "spot"


# ---------------------------------------------------------------------------
# 19. normalize_hl_coin — pair symbol resolves to spot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_pair_symbol_resolves_to_spot(mock_client):
    sym = HLSymbols(client=mock_client, spot_token_inverse={"UBTC": "BTC"})
    coin, leg = await sym.normalize_hl_coin("UBTC/USDC")
    assert coin == "BTC"
    assert leg == "spot"


# ---------------------------------------------------------------------------
# 20. normalize_hl_coin — unknown @idx falls back to perp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_at_unknown_idx_falls_back_to_perp(mock_client):
    sym = HLSymbols(client=mock_client)
    coin, leg = await sym.normalize_hl_coin("@999")
    assert coin == "@999"
    assert leg == "perp"


# ---------------------------------------------------------------------------
# 21. normalize_hl_coin — non-numeric @ falls back to perp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_at_non_numeric_falls_back_to_perp(mock_client):
    sym = HLSymbols(client=mock_client)
    coin, leg = await sym.normalize_hl_coin("@FOO")
    assert coin == "@FOO"
    assert leg == "perp"


# ---------------------------------------------------------------------------
# 22. normalize_hl_coin — blacklisted pair symbol raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_pair_blacklisted_raises_value_error(mock_client):
    sym = HLSymbols(client=mock_client)
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await sym.normalize_hl_coin("AVAX0/USDC")


# ---------------------------------------------------------------------------
# 23. normalize_hl_coin — @idx resolves to blacklisted token raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_at_idx_resolves_blacklisted_raises_value_error(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={},
        pairs=[HLSpotPair(index=300, name="AVAX0/USDC")],
    ))
    sym = HLSymbols(client=client)
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await sym.normalize_hl_coin("@300")


# ---------------------------------------------------------------------------
# 24. normalize_hl_coin — unknown wrapped passes through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_hl_coin_unknown_wrapped_passes_through(mock_client):
    sym = HLSymbols(client=mock_client)
    coin, leg = await sym.normalize_hl_coin("FOO/USDC")
    assert coin == "FOO"
    assert leg == "spot"


# ---------------------------------------------------------------------------
# 25. spot_mids_by_coin — happy path: @-prefixed entries resolved to canonical coins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_happy_path(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(return_value={
        "@1": 60000.0,
        "@2": 3000.0,
        "BTC": 60000.0,   # non-@ key, should be skipped
    })
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={1: "UBTC", 2: "UETH"},
        pairs=[
            HLSpotPair(index=1, name="UBTC/USDC"),
            HLSpotPair(index=2, name="UETH/USDC"),
        ],
    ))
    sym = HLSymbols(
        client=client,
        spot_token_map={"BTC": "UBTC", "ETH": "UETH"},
        spot_token_inverse={"UBTC": "BTC", "UETH": "ETH"},
    )
    result = await sym.spot_mids_by_coin()
    assert result == {"BTC": 60000.0, "ETH": 3000.0}


# ---------------------------------------------------------------------------
# 26. spot_mids_by_coin — pair with wrong quote token is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_wrong_quote_token_skipped(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(return_value={"@1": 60000.0})
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={1: "UBTC"},
        pairs=[HLSpotPair(index=1, name="UBTC/BTC")],  # wrong quote
    ))
    sym = HLSymbols(client=client)
    result = await sym.spot_mids_by_coin()
    assert result == {}


# ---------------------------------------------------------------------------
# 27. spot_mids_by_coin — wrapped token not in SPOT_TOKEN_INVERSE is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_unknown_wrapped_token_skipped(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(return_value={"@5": 1.0})
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={5: "FAKETOKEN"},
        pairs=[HLSpotPair(index=5, name="FAKETOKEN/USDC")],
    ))
    sym = HLSymbols(client=client)
    result = await sym.spot_mids_by_coin()
    assert result == {}


# ---------------------------------------------------------------------------
# 27b. spot_mids_by_coin — symbolic-name key (PURR/USDC) resolves directly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_symbolic_key_resolved(mocker):
    """HL exposes some early pairs (notably PURR/USDC at @0) under their
    symbolic name in all_mids. Verify these are picked up without an @-prefix."""
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(return_value={
        "PURR/USDC": 0.137,
        "PURR": 0.138,        # bare perp ticker — must be ignored
    })
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={0: "PURR"},
        pairs=[HLSpotPair(index=0, name="PURR/USDC")],
    ))
    sym = HLSymbols(client=client, spot_token_inverse={"PURR": "PURR"})
    result = await sym.spot_mids_by_coin()
    assert result == {"PURR": 0.137}


# ---------------------------------------------------------------------------
# 28. spot_mids_by_coin — resolve_spot_pair returning None or empty string skips entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_unresolved_pair_skipped(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(return_value={"@999": 100.0})
    client.spot_meta = AsyncMock(return_value=HLSpotMeta(
        tokens={},
        pairs=[],  # index 999 not present → resolve_spot_pair returns None
    ))
    sym = HLSymbols(client=client)
    result = await sym.spot_mids_by_coin()
    assert result == {}


# ---------------------------------------------------------------------------
# 29. spot_mids_by_coin — client.all_mids() raising logs warning and returns {}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_mids_by_coin_all_mids_raises_returns_empty(mocker, caplog):
    client = mocker.MagicMock(spec=HLClient)
    client.all_mids = AsyncMock(side_effect=RuntimeError("network error"))
    sym = HLSymbols(client=client)
    with caplog.at_level(logging.WARNING):
        result = await sym.spot_mids_by_coin()
    assert result == {}
    assert "allMids failed" in caplog.text
