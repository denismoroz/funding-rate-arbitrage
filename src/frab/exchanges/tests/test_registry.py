"""Tests for frab.exchanges.registry."""
from __future__ import annotations

import pytest

import frab.exchanges.registry as reg_mod
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.exchanges.protocol import Exchange
from frab.exchanges.registry import available, get_exchange, make_market_data, register


# ---------------------------------------------------------------------------
# 1. Default registry has hyperliquid
# ---------------------------------------------------------------------------

def test_default_registry_has_hyperliquid():
    assert "hyperliquid" in available()


# ---------------------------------------------------------------------------
# 2. get_exchange returns HLExchange
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_exchange_returns_hl_instance():
    adapter = get_exchange("hyperliquid")
    try:
        assert isinstance(adapter, HLExchange)
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# 3. make_market_data backward compat alias
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_make_market_data_returns_hl_instance():
    adapter = make_market_data("hyperliquid")
    try:
        assert isinstance(adapter, HLExchange)
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# 4. kwargs propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kwargs_propagated_to_factory():
    custom_url = "https://custom.test/info"
    adapter = get_exchange("hyperliquid", api_url=custom_url)
    try:
        assert adapter._api_url == custom_url
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# 5. Unknown name raises KeyError
# ---------------------------------------------------------------------------

def test_unknown_name_raises_key_error():
    with pytest.raises(KeyError, match="unknown exchange") as exc_info:
        get_exchange("nope")
    assert "nope" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 6. register adds new adapter
# ---------------------------------------------------------------------------

class _DummyAdapter:
    name = "dummy"

    async def get_quote(self, coin): ...
    async def get_funding_rate(self, coin): ...
    async def get_meta(self): ...
    async def open_position(self, req): ...
    async def close_position(self, pos): ...
    async def get_open_positions(self): ...
    async def get_accrued_funding(self, pos): ...
    async def get_wallet(self, coin, kind): ...
    async def transfer(self, coin, amount, from_wallet, to_wallet): ...


def test_register_adds_new_adapter(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {})
    register("dummy", _DummyAdapter)
    assert "dummy" in available()
    assert isinstance(get_exchange("dummy"), _DummyAdapter)


# ---------------------------------------------------------------------------
# 7. register overwrites silently
# ---------------------------------------------------------------------------

def test_register_overwrites_silently(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {"hyperliquid": HLExchange})
    register("hyperliquid", _DummyAdapter)
    adapter = get_exchange("hyperliquid")
    assert isinstance(adapter, _DummyAdapter)


# ---------------------------------------------------------------------------
# 8. available() returns sorted
# ---------------------------------------------------------------------------

def test_available_returns_sorted(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {}, clear=True)
    for name in ["zebra", "alpha", "mango"]:
        register(name, _DummyAdapter)
    result = available()
    assert result == sorted(result)
    assert result == ["alpha", "mango", "zebra"]


# ---------------------------------------------------------------------------
# 9. HLExchange satisfies Exchange Protocol
# ---------------------------------------------------------------------------

def test_hl_exchange_satisfies_protocol():
    from unittest.mock import MagicMock
    info = MagicMock()
    ex = HLExchange(info=info)
    assert isinstance(ex, Exchange)
