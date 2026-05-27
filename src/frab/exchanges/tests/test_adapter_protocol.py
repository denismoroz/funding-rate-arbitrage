"""Verify ExchangeAdapter Protocol shape via runtime_checkable.

Concrete implementations (HyperliquidAdapter, DryRunAdapterGuard) land
in later F2 chunks. Here we only check that a class exposing the required
methods satisfies isinstance(..., ExchangeAdapter), and that a class
missing methods fails.
"""
from __future__ import annotations

from frab.domain.exchange import Exchange
from frab.domain.exchange_profile import ExchangeProfile
from frab.domain.market_spec import MarketSpec
from frab.domain.position import ClosedPosition, Position
from frab.domain.wallet import WalletInfo
from frab.exchanges.adapter import ExchangeAdapter
from frab.exchanges.base import FundingPayment, FundingTick, Quote, UserFill


class _CompleteAdapter:
    """Minimal stub satisfying the ExchangeAdapter Protocol."""

    exchange = Exchange.HYPERLIQUID

    async def get_exchange_profile(self) -> ExchangeProfile: ...
    async def get_wallet(self) -> WalletInfo: ...
    async def get_open_positions(self) -> list[Position]: ...
    async def get_market_specs(self) -> dict[str, MarketSpec]: ...
    async def fetch_quote(self, coin: str) -> Quote: ...
    async def fetch_funding(self, coin: str) -> FundingTick: ...
    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]: ...
    async def fetch_user_fills(self, since_ms: int) -> list[UserFill]: ...
    async def fetch_user_funding(self, since_ms: int) -> list[FundingPayment]: ...
    async def startup_validate(self, coins: tuple[str, ...]) -> None: ...
    async def open_position(
        self,
        coin: str,
        *,
        notional_usd: float,
        margin_reserve_usd: float,
        client_ref: str | None = None,
    ) -> Position: ...
    async def close_position(self, coin: str) -> ClosedPosition: ...
    async def adjust_margin(self, coin: str, delta_usd: float) -> None: ...
    async def close(self) -> None: ...


class _IncompleteAdapter:
    """Missing several required methods — must NOT satisfy the Protocol."""

    exchange = Exchange.HYPERLIQUID

    async def fetch_quote(self, coin: str) -> Quote: ...


def test_complete_adapter_satisfies_protocol():
    assert isinstance(_CompleteAdapter(), ExchangeAdapter)


def test_incomplete_adapter_does_not_satisfy_protocol():
    assert not isinstance(_IncompleteAdapter(), ExchangeAdapter)


def test_protocol_lists_all_required_methods():
    """Sanity: enumerate the protocol's expected method names to catch
    accidental signature drift in adapter.py."""
    expected = {
        "get_exchange_profile",
        "get_wallet",
        "get_open_positions",
        "get_market_specs",
        "fetch_quote",
        "fetch_funding",
        "fetch_funding_history",
        "fetch_user_fills",
        "fetch_user_funding",
        "startup_validate",
        "open_position",
        "close_position",
        "adjust_margin",
        "close",
    }
    for name in expected:
        assert hasattr(ExchangeAdapter, name), f"Protocol missing {name}"


def test_paired_results_importable():
    """F2.1 also extracts PairedOpenResult/PairedCloseResult to _paired_results.
    Old import path through atomic.py must still work (back-compat)."""
    from frab.exchanges._paired_results import PairedCloseResult, PairedOpenResult
    from frab.exchanges.atomic import (
        PairedCloseResult as _CompatClose,
        PairedOpenResult as _CompatOpen,
    )
    assert PairedOpenResult is _CompatOpen
    assert PairedCloseResult is _CompatClose
