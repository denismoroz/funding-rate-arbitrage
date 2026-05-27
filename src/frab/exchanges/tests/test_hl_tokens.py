"""Tests for _hl_tokens — token map and spot-pair validator (F2.3)."""
from __future__ import annotations

import pytest

from frab.exchanges._hl_tokens import (
    MAINNET_SPOT_TOKEN_MAP,
    select_spot_token_map,
    validate_spot_pairs,
)


def test_mainnet_token_map_contains_wrappeds():
    assert MAINNET_SPOT_TOKEN_MAP == {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"}


def test_select_spot_token_map_mainnet():
    assert select_spot_token_map("mainnet") == MAINNET_SPOT_TOKEN_MAP


def test_select_spot_token_map_testnet_empty():
    assert select_spot_token_map("testnet") == {}


def test_select_spot_token_map_unknown_network_empty():
    assert select_spot_token_map("nonsense") == {}


def _spot_meta_payload(*, pairs: list[tuple[str, str]] = ()) -> dict:
    """Build a fake HL spotMeta payload.

    Each pair tuple = (pair_name, base_token_name). USDC is always present
    as index 0; bases get successive indices.
    """
    tokens = [{"index": 0, "name": "USDC"}]
    universe = []
    for i, (pair_name, base) in enumerate(pairs, start=1):
        tokens.append({"index": i, "name": base})
        universe.append({"name": pair_name, "tokens": [i, 0]})
    return {"tokens": tokens, "universe": universe}


class _FakeMarketData:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def _post(self, _req: dict) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_validate_spot_pairs_happy_path():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[
        ("UBTC/USDC", "UBTC"),
        ("UETH/USDC", "UETH"),
        ("USOL/USDC", "USOL"),
    ]))
    assert await validate_spot_pairs(market_data, ("BTC", "ETH", "SOL")) is None


@pytest.mark.asyncio
async def test_validate_spot_pairs_missing_usdc_raises():
    market_data = _FakeMarketData({"tokens": [], "universe": []})
    with pytest.raises(RuntimeError, match="USDC token not found"):
        await validate_spot_pairs(market_data, ("BTC",))


@pytest.mark.asyncio
async def test_validate_spot_pairs_unknown_coin_in_universe_raises():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[("UBTC/USDC", "UBTC")]))
    with pytest.raises(RuntimeError, match="missing_map"):
        await validate_spot_pairs(market_data, ("BTC", "DOGE"))


@pytest.mark.asyncio
async def test_validate_spot_pairs_token_not_on_hl_raises():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[("UBTC/USDC", "UBTC")]))
    with pytest.raises(RuntimeError, match="not_on_hl"):
        await validate_spot_pairs(market_data, ("BTC", "ETH"))


def test_server_back_compat_re_export():
    """server.py re-exports the new names so cli.py and existing tests
    can keep importing from frab.server until F2.6/F2.8 cleans up."""
    from frab.server import (
        MAINNET_SPOT_TOKEN_MAP as ServerMap,
        _select_spot_token_map as server_select,
        _validate_spot_pairs as server_validate,
    )
    assert ServerMap is MAINNET_SPOT_TOKEN_MAP
    assert server_select is select_spot_token_map
    assert server_validate is validate_spot_pairs
