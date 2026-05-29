"""StateMachine — routes a FarbPosition to its registered State handler."""
from __future__ import annotations

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.states.base import State


class StateMachine:
    def __init__(self, handlers: dict[FarbState, State]) -> None:
        self._handlers = handlers

    async def step(self, fp: FarbPosition) -> FarbState | None:
        """Execute the handler for fp.state. Returns the new FarbState the
        handler transitioned to, or None if no handler is registered (which
        means terminal/steady — caller should treat as no-op) or if the
        handler terminated the FP (FAILED)."""
        handler = self._handlers.get(fp.state)
        if handler is None:
            return None
        return await handler.execute(fp)
