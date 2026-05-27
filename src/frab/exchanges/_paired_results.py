"""Result DTOs for paired (spot+perp) open/close operations.

Exchange-agnostic. Extracted from atomic.py so that the ExchangeAdapter
Protocol can reference them without pulling in HL-specific code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from frab.exchanges.base import FillReport


@dataclass(frozen=True, slots=True)
class PairedOpenResult:
    status: Literal["ok", "failed"]
    perp_fill: FillReport | None
    spot_fill: FillReport | None
    perp_attempts: int
    spot_attempts: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairedCloseResult:
    status: Literal["ok", "failed"]
    perp_fill: FillReport | None
    spot_fill: FillReport | None
    perp_attempts: int
    spot_attempts: int
    errors: tuple[str, ...]
