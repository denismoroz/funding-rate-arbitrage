"""Regression: the HL SDK (Info/Exchange) must be constructed WITH a timeout.

The SDK's API defaults to timeout=None (infinite wait). A dead socket during a
connection-reset storm then hangs the to_thread call forever and freezes the
awaiting EngineLoop (xsmom froze for ~5h on 2026-06-17 this way). HLExchange must
always pass an explicit sdk_timeout_s into Info and Exchange.
"""

from __future__ import annotations

import httpx

from frab.exchanges.hyperliquid.exchange import HLExchange

# Throwaway key (well-known hardhat account #0) — used only to exercise the
# Exchange-construction branch offline; never funded.
_TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_info_gets_default_sdk_timeout():
    ex = HLExchange(client=httpx.AsyncClient())
    assert ex._info.timeout == 30.0


def test_info_gets_custom_sdk_timeout():
    ex = HLExchange(client=httpx.AsyncClient(), sdk_timeout_s=12.5)
    assert ex._info.timeout == 12.5


def test_exchange_gets_sdk_timeout():
    ex = HLExchange(
        client=httpx.AsyncClient(),
        private_key=_TEST_KEY,
        account_address=_TEST_ADDR,
        sdk_timeout_s=17.0,
    )
    assert ex._exchange is not None
    assert ex._exchange.timeout == 17.0
