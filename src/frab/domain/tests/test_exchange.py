from __future__ import annotations

import pytest

from frab.domain.exchange import Exchange


def test_hyperliquid_value():
    assert Exchange.HYPERLIQUID == "hyperliquid"


def test_hyperliquid_is_str():
    assert isinstance(Exchange.HYPERLIQUID, str)


def test_string_equality():
    assert Exchange.HYPERLIQUID == Exchange("hyperliquid")


def test_from_string():
    ex = Exchange("hyperliquid")
    assert ex is Exchange.HYPERLIQUID


def test_unknown_value_raises():
    with pytest.raises(ValueError):
        Exchange("drift")


def test_name_attribute():
    assert Exchange.HYPERLIQUID.name == "HYPERLIQUID"
