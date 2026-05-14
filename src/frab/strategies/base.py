"""Abstract Strategy + tick-report DTOs."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from frab.exchanges.base import FillReport, FundingTick, Quote


@dataclass(frozen=True, slots=True)
class SignalEvent:
    coin: str
    ts_ms: int
    signal_value: float | None
    regime_pass: bool
    action: str  # one of "NONE", "OPEN", "CLOSE"


@dataclass(frozen=True, slots=True)
class TickReport:
    ts_ms: int
    signals: tuple[SignalEvent, ...]
    fills: tuple[FillReport, ...]
    opened: tuple[str, ...]
    closed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    ts_ms: int
    total_equity: float
    cash: float
    spot_value: float
    perp_unrealized: float
    perp_realized_cum: float
    funding_cum: float
    fees_cum: float


class Strategy(ABC):
    name: str
    version: str

    @abstractmethod
    async def on_minute_tick(self, now_ms: int, quotes: dict[str, Quote]) -> None:
        ...  # pragma: no cover

    @abstractmethod
    async def on_hour_tick(self, now_ms: int, funding: dict[str, FundingTick]) -> TickReport:
        ...  # pragma: no cover

    @abstractmethod
    def compute_equity(self, now_ms: int) -> EquitySnapshot:
        ...  # pragma: no cover
