from __future__ import annotations

from typing import Callable

from frab.exchanges.base import MarketDataSource
from frab.exchanges.hyperliquid import HLMarketData

_REGISTRY: dict[str, Callable[..., MarketDataSource]] = {
    "hyperliquid": HLMarketData,
}


def register(name: str, factory: Callable[..., MarketDataSource]) -> None:
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def make_market_data(name: str, **kwargs) -> MarketDataSource:
    if name not in _REGISTRY:
        raise KeyError(f"unknown exchange: {name!r}. available: {available()}")
    return _REGISTRY[name](**kwargs)
