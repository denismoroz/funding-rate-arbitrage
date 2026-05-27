from __future__ import annotations

import pytest

from frab.domain.exchange import Exchange
from frab.domain.wallet import WalletInfo


_EX = Exchange.HYPERLIQUID


def test_wallet_fields():
    w = WalletInfo(
        exchange=_EX,
        available_usdc=1000.0,
        reserved_usdc=200.0,
        total_value_usd=1250.0,
    )
    assert w.available_usdc == 1000.0
    assert w.reserved_usdc == 200.0
    assert w.total_value_usd == 1250.0
    assert w.exchange is _EX


def test_wallet_frozen():
    w = WalletInfo(exchange=_EX, available_usdc=100.0, reserved_usdc=0.0, total_value_usd=100.0)
    with pytest.raises((AttributeError, TypeError)):
        w.available_usdc = 999.0  # type: ignore[misc]


def test_wallet_equality():
    w1 = WalletInfo(exchange=_EX, available_usdc=500.0, reserved_usdc=50.0, total_value_usd=600.0)
    w2 = WalletInfo(exchange=_EX, available_usdc=500.0, reserved_usdc=50.0, total_value_usd=600.0)
    assert w1 == w2


def test_wallet_inequality_different_available():
    w1 = WalletInfo(exchange=_EX, available_usdc=100.0, reserved_usdc=0.0, total_value_usd=100.0)
    w2 = WalletInfo(exchange=_EX, available_usdc=200.0, reserved_usdc=0.0, total_value_usd=200.0)
    assert w1 != w2
