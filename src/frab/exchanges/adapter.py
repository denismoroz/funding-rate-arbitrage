"""ExchangeAdapter Protocol — universal per-exchange trading interface.

Strategy talks to this and ONLY this. All exchange quirks (separate
wallets, transfer choreography, spot-first ordering, token name mapping,
szDecimals rounding) live inside concrete implementations.

Reads are always safe in dry-run.
Writes (open_position, close_position, adjust_margin) MUST be wrapped by
DryRunAdapterGuard when dry_run=True (see exchanges/dry_run.py).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from frab.domain.exchange import Exchange
from frab.domain.exchange_profile import ExchangeProfile
from frab.domain.market_spec import MarketSpec
from frab.domain.position import ClosedPosition, Position
from frab.domain.wallet import WalletInfo
from frab.exchanges.base import FundingPayment, FundingTick, Quote, UserFill


@runtime_checkable
class ExchangeAdapter(Protocol):
    exchange: Exchange

    # reads (safe in dry-run)
    async def get_exchange_profile(self) -> ExchangeProfile: ...  # pragma: no cover
    async def get_wallet(self) -> WalletInfo: ...  # pragma: no cover
    async def get_open_positions(self) -> list[Position]: ...  # pragma: no cover
    async def get_market_specs(self) -> dict[str, MarketSpec]: ...  # pragma: no cover

    async def fetch_quote(self, coin: str) -> Quote: ...  # pragma: no cover
    async def fetch_funding(self, coin: str) -> FundingTick: ...  # pragma: no cover
    async def fetch_funding_history(
        self, coin: str, since_ms: int,
    ) -> list[FundingTick]: ...  # pragma: no cover

    async def fetch_user_fills(self, since_ms: int) -> list[UserFill]: ...  # pragma: no cover
    async def fetch_user_funding(
        self, since_ms: int,
    ) -> list[FundingPayment]: ...  # pragma: no cover

    async def startup_validate(self, coins: tuple[str, ...]) -> None: ...  # pragma: no cover

    # writes (MUTATING — DryRunAdapterGuard wraps in dry-run mode)
    async def open_position(
        self,
        coin: str,
        *,
        notional_usd: float,
        margin_reserve_usd: float,
        client_ref: str | None = None,
    ) -> Position: ...  # pragma: no cover

    async def close_position(self, coin: str) -> ClosedPosition: ...  # pragma: no cover

    async def adjust_margin(self, coin: str, delta_usd: float) -> None: ...  # pragma: no cover

    # lifecycle
    async def close(self) -> None: ...  # pragma: no cover
