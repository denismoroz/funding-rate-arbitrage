from enum import Enum


class Instrument(str, Enum):
    SPOT = "spot"
    PERP = "perp"
    COLLATERAL = "collateral"   # USDC reservation on perp wallet


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"               # for COLLATERAL


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class FarbState(str, Enum):
    CHECK_MARGIN = "check_margin"
    OPENING_MARGIN = "opening_margin"
    MARGIN_RESERVED = "margin_reserved"
    OPENING_LONG = "opening_long"
    LONG_OPENED = "long_opened"
    OPENING_SHORT = "opening_short"
    OPEN = "open"
    CLOSING_SHORT = "closing_short"
    SHORT_CLOSED = "short_closed"
    CLOSING_LONG = "closing_long"
    LONG_CLOSED = "long_closed"
    RELEASING_MARGIN = "releasing_margin"
    CLOSED = "closed"
    FAILED = "failed"
