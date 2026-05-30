"""StrategyContext + State ABC for two-phase state machine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.params import TwoPhaseParams


@dataclass(frozen=True)
class StrategyContext:
    """Shared dependencies for all State handlers."""
    exchange: Exchange
    farb_repo: FarbRepo
    params: TwoPhaseParams
    session_factory: async_sessionmaker[AsyncSession]
    event_bus: EventBus | None = None


class State(ABC):
    """A single phase in the two-phase state machine. Each subclass declares
    the FarbState it handles via the class-level `state` attribute, and
    accepts a StrategyContext in its constructor.

    Each State owns its full transition end-to-end: side-effect work
    (exchange/repo calls), state_data updates, the farb_repo.transition or
    mark_failed call, and event publishing. Returns the new FarbState on
    success, or None if the FP entered a terminal failure state."""

    state: ClassVar[FarbState]

    def __init__(self, ctx: StrategyContext) -> None:
        self._ctx = ctx

    @abstractmethod
    async def execute(self, fp: FarbPosition) -> FarbState | None: ...
