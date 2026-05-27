"""Tests for frab.exchanges.registry."""
from __future__ import annotations

import pytest

import frab.exchanges.registry as reg_mod
from frab.exchanges.hyperliquid.reader import HLExchangeReader
from frab.exchanges.registry import available, make_market_data, register


# ---------------------------------------------------------------------------
# 1. Default registry has hyperliquid
# ---------------------------------------------------------------------------

def test_default_registry_has_hyperliquid():
    assert "hyperliquid" in available()


# ---------------------------------------------------------------------------
# 2. make_market_data returns HLExchangeReader
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_make_market_data_returns_hl_instance():
    adapter = make_market_data("hyperliquid")
    try:
        assert isinstance(adapter, HLExchangeReader)
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# 3. kwargs propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kwargs_propagated_to_factory():
    custom_url = "https://custom.test/info"
    adapter = make_market_data("hyperliquid", api_url=custom_url)
    try:
        assert adapter._api_url == custom_url
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# 4. Unknown name raises KeyError
# ---------------------------------------------------------------------------

def test_unknown_name_raises_key_error():
    with pytest.raises(KeyError, match="unknown exchange") as exc_info:
        make_market_data("nope")
    assert "nope" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. register adds new adapter
# ---------------------------------------------------------------------------

class _DummyAdapter:
    name = "dummy"

    async def fetch_funding(self, coin): ...  # pragma: no cover
    async def fetch_funding_history(self, coin, since_ms): ...  # pragma: no cover
    async def fetch_quote(self, coin): ...  # pragma: no cover
    async def fetch_meta(self): ...  # pragma: no cover


def test_register_adds_new_adapter(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {})
    register("dummy", _DummyAdapter)
    assert "dummy" in available()
    assert isinstance(make_market_data("dummy"), _DummyAdapter)


# ---------------------------------------------------------------------------
# 6. register overwrites silently
# ---------------------------------------------------------------------------

class _AltAdapter:
    name = "hyperliquid"

    async def fetch_funding(self, coin): ...  # pragma: no cover
    async def fetch_funding_history(self, coin, since_ms): ...  # pragma: no cover
    async def fetch_quote(self, coin): ...  # pragma: no cover
    async def fetch_meta(self): ...  # pragma: no cover


def test_register_overwrites_silently(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {"hyperliquid": HLExchangeReader})
    register("hyperliquid", _AltAdapter)
    adapter = make_market_data("hyperliquid")
    assert isinstance(adapter, _AltAdapter)


# ---------------------------------------------------------------------------
# 7. available() returns sorted
# ---------------------------------------------------------------------------

def test_available_returns_sorted(mocker):
    mocker.patch.dict(reg_mod._REGISTRY, {}, clear=True)
    for name in ["zebra", "alpha", "mango"]:
        register(name, _DummyAdapter)
    result = available()
    assert result == sorted(result)
    assert result == ["alpha", "mango", "zebra"]
