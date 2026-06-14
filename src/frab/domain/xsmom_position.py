from dataclasses import dataclass
from datetime import datetime

from .enums import Side, XsmomState, XSMOM_ACTIVE_STATES


@dataclass(frozen=True)
class XsmomPosition:
    id: int | None
    strategy_id: int
    coin: str                        # the position's base coin (e.g. "BTC")
    side: Side                       # LONG or SHORT
    state: XsmomState
    state_data: dict                 # strategy-specific JSON blob
    perp_position_id: int | None
    collateral_position_id: int | None
    target_qty: float | None
    opened_at: datetime
    closed_at: datetime | None

    @property
    def is_active(self) -> bool:
        """True iff the position is in an actively-holding state (OPENED)."""
        return self.state in XSMOM_ACTIVE_STATES
