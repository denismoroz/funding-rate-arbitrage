"""Abstract Strategy + tick-report DTOs."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from frab.exchanges.base import FillReport, FundingTick, Quote


@dataclass(frozen=True, slots=True)
class SignalEvent:
    coin: str
    ts: datetime
    signal_value: float | None
    regime_pass: bool
    action: str  # one of "NONE", "OPEN", "CLOSE"


@dataclass(frozen=True, slots=True)
class FailedOpen:
    coin: str
    ts: datetime
    perp_fill: FillReport | None   # None if perp leg never filled
    spot_fill: FillReport | None   # None if spot leg never filled
    error: str                     # short summary, typically repr() of the last underlying exception


@dataclass(frozen=True, slots=True)
class TickReport:
    ts: datetime
    signals: tuple[SignalEvent, ...]
    fills: tuple[FillReport, ...]
    opened: tuple[str, ...]
    closed: tuple[str, ...]
    # Per-position funding accrued this tick (coin → delta in quote currency).
    # Only includes positions that were already open at the start of this
    # hour-tick (not coins opened by Step 5 of the same tick).
    funding_accrued: tuple[tuple[str, float], ...] = ()
    # For TwoPhaseDynamic — persisted per-position state:
    opened_min_holds: tuple[tuple[str, int], ...] = ()      # (coin, position_min_hold_hours) for each newly opened position
    consec_negative_updates: tuple[tuple[str, int], ...] = ()  # (coin, new_consec_negative_hours) for each in-position coin
    # Failed paired-open attempts (perp leg failed before any fill, or spot leg
    # failed after perp filled).  Written as FAILED positions in the DB; NOT
    # added to the in-memory open-positions cache.
    failed_opens: tuple[FailedOpen, ...] = ()


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    ts: datetime
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
    async def on_minute_tick(self, now: datetime, quotes: dict[str, Quote]) -> None:
        ...  # pragma: no cover

    @abstractmethod
    async def on_hour_tick(self, now: datetime, funding: dict[str, FundingTick]) -> TickReport:
        ...  # pragma: no cover

    @abstractmethod
    def compute_equity(self, now: datetime) -> EquitySnapshot:
        ...  # pragma: no cover

    def set_fees_cum(self, value: float) -> None:
        """Replace the running fees counter with the DB-authoritative total."""
        self._fees_cum = value  # type: ignore[attr-defined]

    def set_funding_cum(self, value: float) -> None:
        """Replace the running funding counter with the DB-authoritative total."""
        self._funding_cum = value  # type: ignore[attr-defined]
