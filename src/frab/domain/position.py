from dataclasses import dataclass
from datetime import datetime

from .enums import Instrument, PositionStatus, Side


@dataclass(frozen=True)
class Position:
    id: int | None
    exchange_name: str              # NOT the Exchange object — that's a Step 4 concern
    coin: str                       # canonical: "BTC", "USDC", "USDT", etc.
    instrument: Instrument
    side: Side
    qty: float
    entry_price: float              # 1.0 for COLLATERAL
    opened_at: datetime
    closed_at: datetime | None
    status: PositionStatus
    farb_position_id: int | None
