"""HLActionContext: shared dependencies for HL Actions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols


@dataclass(frozen=True)
class HLActionContext:
    """Shared dependencies for all HL Actions."""
    client: HLClient
    symbols: HLSymbols
    session_factory: async_sessionmaker[AsyncSession] | None
    exchange_name: str
    address: str | None
    clock_fn: Callable[[], datetime]
    slippage: float = 0.01
    partial_fill_tolerance: float = 0.01


class HLAction:
    """Marker base for HL Actions. Subclasses set `requires_session` if they need DB."""
    requires_session: ClassVar[bool] = False

    def __init__(self, ctx: HLActionContext) -> None:
        self._ctx = ctx
