"""XsmomContext + State ABC for the XSMOM state machine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import XsmomPosition, XsmomState
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.xsmom_repo import XsmomRepo
from frab.settings import Settings
from frab.strategy.xsmom.params import XsmomParams

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class XsmomContext:
    """Shared dependencies for all XSMOM State handlers."""
    exchange: Exchange
    xsmom_repo: XsmomRepo
    params: XsmomParams
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    event_bus: EventBus | None = None


class State(ABC):
    """A single phase in the XSMOM state machine.

    Each subclass declares the XsmomState it handles via the class-level
    ``state`` attribute, and accepts an XsmomContext in its constructor.

    Each State owns its full transition end-to-end: side-effect work
    (exchange/repo calls), state_data updates, the xsmom_repo.transition or
    mark_failed call, and event publishing. Returns the new XsmomState on
    success, or None if the position entered a terminal/resting state.
    """

    state: ClassVar[XsmomState]

    def __init__(self, ctx: XsmomContext) -> None:
        self._ctx = ctx

    @abstractmethod
    async def execute(self, fp: XsmomPosition) -> XsmomState | None: ...
