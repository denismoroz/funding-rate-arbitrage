"""State ABC for the two-phase FarbPosition state machine."""
from __future__ import annotations

from abc import ABC, abstractmethod

from frab.domain import FarbPosition, FarbState


class State(ABC):
    """A single phase in the two-phase state machine.

    Each State owns its full transition end-to-end: side-effect work
    (exchange/repo calls), state_data updates, the farb_repo.transition or
    mark_failed call, and event publishing. Returns the new FarbState on
    success, or None if the FP entered a terminal failure state."""

    @abstractmethod
    async def execute(self, fp: FarbPosition) -> FarbState | None: ...
