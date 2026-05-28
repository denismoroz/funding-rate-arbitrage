"""Exchange registry: maps exchange names to factory callables.

Factory signature: factory(*, session_factory=None, **kwargs) -> Exchange
"""
from __future__ import annotations

from typing import Callable

from frab.exchanges.protocol import Exchange
from frab.exchanges.hyperliquid.exchange import HLExchange

_REGISTRY: dict[str, Callable[..., Exchange]] = {
    "hyperliquid": HLExchange,
}


def register(name: str, factory: Callable[..., Exchange]) -> None:
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_exchange(name: str, *, session_factory=None, **kwargs) -> Exchange:
    """Return an Exchange instance by name."""
    if name not in _REGISTRY:
        raise KeyError(f"unknown exchange: {name!r}. available: {available()}")
    if session_factory is not None:
        return _REGISTRY[name](session_factory=session_factory, **kwargs)
    return _REGISTRY[name](**kwargs)


# Backward-compatible alias
def make_market_data(name: str, **kwargs) -> Exchange:
    return get_exchange(name, **kwargs)
