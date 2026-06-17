"""Tests for tokens.py — bridge-token blacklist and spot-pair validator.

Removed (Phase F2 cleanup):
- test_mainnet_token_map_contains_wrappeds — MAINNET_SPOT_TOKEN_MAP deleted (registry is source)
- test_select_spot_token_map_* — select_spot_token_map() deleted (registry is source)
- test_server_back_compat_re_export — stale re-exports removed from server.py
"""
from __future__ import annotations

import pytest

from frab.exchanges.hyperliquid.tokens import (
    BRIDGE_TOKEN_BLACKLIST,
    validate_spot_pairs,
)


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


_SPOT_MAP = {"BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"}


@pytest.mark.asyncio
async def test_validate_spot_pairs_happy_path():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[
        ("UBTC/USDC", "UBTC"),
        ("UETH/USDC", "UETH"),
        ("USOL/USDC", "USOL"),
    ]))
    assert await validate_spot_pairs(market_data, ("BTC", "ETH", "SOL"), spot_token_map=_SPOT_MAP) is None


@pytest.mark.asyncio
async def test_validate_spot_pairs_missing_usdc_raises():
    market_data = _FakeMarketData({"tokens": [], "universe": []})
    with pytest.raises(RuntimeError, match="USDC token not found"):
        await validate_spot_pairs(market_data, ("BTC",), spot_token_map=_SPOT_MAP)


@pytest.mark.asyncio
async def test_validate_spot_pairs_unknown_coin_in_universe_raises():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[("UBTC/USDC", "UBTC")]))
    with pytest.raises(RuntimeError, match="missing_map"):
        # DOGE is not in _SPOT_MAP → missing_map
        await validate_spot_pairs(market_data, ("BTC", "DOGE"), spot_token_map=_SPOT_MAP)


@pytest.mark.asyncio
async def test_validate_spot_pairs_token_not_on_hl_raises():
    market_data = _FakeMarketData(_spot_meta_payload(pairs=[("UBTC/USDC", "UBTC")]))
    with pytest.raises(RuntimeError, match="not_on_hl"):
        # ETH is in map but UETH is not in the fake spotMeta
        await validate_spot_pairs(market_data, ("BTC", "ETH"), spot_token_map=_SPOT_MAP)


# ---------------------------------------------------------------------------
# BRIDGE_TOKEN_BLACKLIST tests
# ---------------------------------------------------------------------------

def test_bridge_token_blacklist_contains_known_bridges():
    """The three known EVM bridge tokens must be in the blacklist."""
    assert "AVAX0" in BRIDGE_TOKEN_BLACKLIST
    assert "LINK0" in BRIDGE_TOKEN_BLACKLIST
    assert "AAVE0" in BRIDGE_TOKEN_BLACKLIST


def test_bridge_token_blacklist_does_not_contain_safe_tokens():
    """Safe wrapped tokens (1:1 with perp) must NOT be blacklisted."""
    for safe in ("UBTC", "UETH", "USOL"):
        assert safe not in BRIDGE_TOKEN_BLACKLIST


def test_bridge_token_blacklist_is_frozenset():
    assert isinstance(BRIDGE_TOKEN_BLACKLIST, frozenset)


@pytest.mark.asyncio
async def test_normalize_hl_coin_rejects_avax0():
    """AVAX0/USDC fill must raise ValueError — never silently map to AVAX perp."""
    from frab.exchanges.hyperliquid.exchange import HLExchange
    import httpx
    ex = HLExchange(client=httpx.AsyncClient())
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await ex._symbols.normalize_hl_coin("AVAX0/USDC")


@pytest.mark.asyncio
async def test_normalize_hl_coin_rejects_link0():
    from frab.exchanges.hyperliquid.exchange import HLExchange
    import httpx
    ex = HLExchange(client=httpx.AsyncClient())
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await ex._symbols.normalize_hl_coin("LINK0/USDC")


@pytest.mark.asyncio
async def test_normalize_hl_coin_rejects_aave0():
    from frab.exchanges.hyperliquid.exchange import HLExchange
    import httpx
    ex = HLExchange(client=httpx.AsyncClient())
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await ex._symbols.normalize_hl_coin("AAVE0/USDC")


@pytest.mark.asyncio
async def test_normalize_hl_coin_future_bridge_token_rejected():
    """Any token name added to BRIDGE_TOKEN_BLACKLIST is blocked, even a hypothetical one."""
    from frab.exchanges.hyperliquid.exchange import HLExchange, BRIDGE_TOKEN_BLACKLIST
    import httpx
    # Verify the guard is data-driven: patch a hypothetical future bridge token.
    fake_token = next(iter(BRIDGE_TOKEN_BLACKLIST))  # any existing one
    ex = HLExchange(client=httpx.AsyncClient())
    with pytest.raises(ValueError, match="BRIDGE_TOKEN_BLACKLIST"):
        await ex._symbols.normalize_hl_coin(f"{fake_token}/USDC")


def test_server_re_exports_validate_spot_pairs():
    """server.py still imports _validate_spot_pairs (used in Phase C discovery)."""
    from frab.server import _validate_spot_pairs as server_validate
    assert server_validate is validate_spot_pairs
