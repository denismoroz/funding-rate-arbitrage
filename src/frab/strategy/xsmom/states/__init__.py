"""Public facade of the XSMOM state-machine states package.

Exports:
- State, XsmomContext  — the ABC and DI context (defined in _base.py).
- STATE_CLASSES        — ordered tuple of concrete State classes with side-effects
                         to drive. The orchestrator uses this to wire the state
                         machine in one line.

Design choices:
- Only NEW and CLOSE are registered here. OPENED is a resting/non-transient
  state (no per-tick side-effects) and CLOSED/FAILED are terminal — none of
  them need a handler. This mirrors how FRAB registers only transient states
  in STATE_CLASSES and treats PRE/POST_BREAKEVEN as non-transient.
- No opened.py or closed.py handler files are created; their absence is the
  deliberate signal that they are not transient.

Runtime invariant: every entry of STATE_CLASSES must declare its ``state``
ClassVar. Enforced at import time so a misconfiguration fails fast.
"""
from __future__ import annotations

from frab.strategy.xsmom.states._base import State, XsmomContext
from frab.strategy.xsmom.states.new import NewState
from frab.strategy.xsmom.states.close import CloseState


STATE_CLASSES: tuple[type[State], ...] = (
    NewState,
    CloseState,
)

# Runtime guard: every subclass must declare which XsmomState it handles.
for _cls in STATE_CLASSES:
    assert hasattr(_cls, "state"), f"{_cls.__name__} missing required `state` class attribute"


__all__ = ["State", "XsmomContext", "STATE_CLASSES"]
