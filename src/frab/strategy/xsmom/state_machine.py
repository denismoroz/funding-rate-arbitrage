"""StateMachine — routes an XsmomPosition to its registered State handler."""
from __future__ import annotations

from frab.domain import XsmomPosition, XsmomState
from frab.strategy.xsmom.states._base import State


class StateMachine:
    def __init__(self, handlers: dict[XsmomState, State]) -> None:
        self._handlers = handlers

    async def step(self, fp: XsmomPosition) -> XsmomState | None:
        """Execute the handler for fp.state.

        Returns the new XsmomState the handler transitioned to, or None if no
        handler is registered (terminal/resting — caller treats as no-op) or
        if the handler terminated the position (CLOSED/FAILED).
        """
        handler = self._handlers.get(fp.state)
        if handler is None:
            return None
        return await handler.execute(fp)
