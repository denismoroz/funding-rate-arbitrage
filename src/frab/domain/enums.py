from enum import Enum


class XsmomState(str, Enum):
    NEW = "new"
    OPENED = "opened"
    CLOSE = "close"
    CLOSED = "closed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (XsmomState.CLOSED, XsmomState.FAILED)


# States in which an XsmomPosition is actively holding an open position.
XSMOM_ACTIVE_STATES: frozenset[XsmomState] = frozenset({XsmomState.OPENED})


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
    OPENING_LONG = "opening_long"
    OPENING_SHORT = "opening_short"
    PRE_BREAKEVEN = "pre_breakeven"
    POST_BREAKEVEN = "post_breakeven"
    CLOSING_SHORT = "closing_short"
    CLOSING_LONG = "closing_long"
    RELEASING_MARGIN = "releasing_margin"
    CLOSED = "closed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (FarbState.CLOSED, FarbState.FAILED)


# States in which a FarbPosition is actively holding an open arb leg.
ACTIVE_STATES: frozenset[FarbState] = frozenset({
    FarbState.PRE_BREAKEVEN,
    FarbState.POST_BREAKEVEN,
})
