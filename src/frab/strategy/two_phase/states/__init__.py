"""Public facade of the two-phase state-machine states package.

Exports:
- State, StrategyContext — the ABC and DI context (defined in _base.py).
- STATE_CLASSES         — ordered list of all concrete State classes; the
                          strategy uses this to wire its state machine in one line.

Runtime invariant: every entry of STATE_CLASSES must declare its `state`
ClassVar. Enforced at import time so a misconfigured state fails fast.
"""
from __future__ import annotations

from frab.strategy.two_phase.states._base import State, StrategyContext
from frab.strategy.two_phase.states.check_margin import CheckMarginState
from frab.strategy.two_phase.states.closing_long import ClosingLongState
from frab.strategy.two_phase.states.closing_short import ClosingShortState
from frab.strategy.two_phase.states.opening_long import OpeningLongState
from frab.strategy.two_phase.states.opening_margin import OpeningMarginState
from frab.strategy.two_phase.states.opening_short import OpeningShortState
from frab.strategy.two_phase.states.releasing_margin import ReleasingMarginState


STATE_CLASSES: list[type[State]] = [
    CheckMarginState,
    OpeningMarginState,
    OpeningLongState,
    OpeningShortState,
    ClosingShortState,
    ClosingLongState,
    ReleasingMarginState,
]

# Runtime guard: every subclass must declare which FarbState it handles.
for _cls in STATE_CLASSES:
    assert hasattr(_cls, "state"), f"{_cls.__name__} missing required `state` class attribute"


__all__ = ["State", "StrategyContext", "STATE_CLASSES"]
