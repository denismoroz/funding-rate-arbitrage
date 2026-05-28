from __future__ import annotations

from typing import Callable

from frab.exchanges.base import ExchangeDataSource
from frab.exchanges.hyperliquid.exchange import HLExchange

_REGISTRY: dict[str, Callable[..., ExchangeDataSource]] = {
    "hyperliquid": HLExchange,
}


def register(name: str, factory: Callable[..., ExchangeDataSource]) -> None:
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def make_market_data(name: str, **kwargs) -> ExchangeDataSource:
    if name not in _REGISTRY:
        raise KeyError(f"unknown exchange: {name!r}. available: {available()}")
    return _REGISTRY[name](**kwargs)
