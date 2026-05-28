from dataclasses import dataclass
from datetime import datetime

from .enums import FarbState


@dataclass(frozen=True)
class FarbPosition:
    id: int | None
    strategy_id: int
    coin: str                       # the arb's base coin (e.g. "BTC")
    state: FarbState
    state_data: dict                # strategy-specific JSON blob (e.g. min_hold_hours, phase, consec_negative)
    spot_position_id: int | None
    perp_position_id: int | None
    margin_position_id: int | None
    opened_at: datetime
    closed_at: datetime | None
