"""ExitEvaluator — hourly dispatch to per-phase handlers for active FarbPositions."""
from __future__ import annotations

from frab.domain import FarbState
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.strategy.two_phase.close_gate import PENDING_FORCED_KEY, CloseGate
from frab.settings import Settings
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.states.pre_breakeven import PreBreakevenHandler
from frab.strategy.two_phase.states.post_breakeven import PostBreakevenHandler


class ExitEvaluator:
    """Evaluates active FarbPositions (PRE/POST_BREAKEVEN) and transitions them
    to CLOSING_SHORT when appropriate.

    Dispatches each position to the correct per-phase handler:
      - PRE_BREAKEVEN  → PreBreakevenHandler (latch check first, then Phase-1 exit logic)
      - POST_BREAKEVEN → PostBreakevenHandler (Phase-2 exit logic, one-way latch)
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
        signal_computer: SignalComputer,
        settings: Settings,
        close_gate: CloseGate | None = None,
    ) -> None:
        self._close_gate = close_gate
        self._strategy_id = strategy_id
        self._farb_repo = farb_repo
        self._params = params
        self._signal_computer = signal_computer
        self._settings = settings

        # Construct per-phase handlers with the same deps
        handler_kwargs = dict(
            strategy_id=strategy_id,
            farb_repo=farb_repo,
            params=params,
            signal_computer=signal_computer,
            settings=settings,
            close_gate=close_gate,
        )
        self._pre_handler = PreBreakevenHandler(**handler_kwargs)
        self._post_handler = PostBreakevenHandler(**handler_kwargs)

    async def evaluate(self, *, now_ms: int) -> None:
        """For each active (PRE/POST_BREAKEVEN) FarbPosition: dispatch to the correct handler."""
        active_fps = await self._farb_repo.list_active(self._strategy_id)
        for fp in active_fps:
            await self.evaluate_one(fp, now_ms=now_ms)

    async def evaluate_one(self, fp, *, now_ms: int) -> None:
        """Dispatch a single FarbPosition to the correct phase handler."""
        if fp.state in (FarbState.PRE_BREAKEVEN, FarbState.POST_BREAKEVEN) and await self._retry_forced_exit(
            fp, now_ms=now_ms,
        ):
            return
        if fp.state == FarbState.PRE_BREAKEVEN:
            await self._pre_handler.evaluate_one(fp, now_ms=now_ms)
        elif fp.state == FarbState.POST_BREAKEVEN:
            await self._post_handler.evaluate_one(fp, now_ms=now_ms)
        else:
            _pkg.logger.warning(
                "evaluate_one called with non-active state=%s farb_position_id=%s — skipping",
                fp.state.value,
                fp.id,
            )

    async def _retry_forced_exit(self, fp, *, now_ms: int) -> bool:
        """Repeat an exit the owner or the margin watchdog asked for and the close check
        refused. Returns True when the position was handled here (closing started, or
        still blocked): the strategy's own exit logic then waits for the next hour."""
        pending = (fp.state_data or {}).get(PENDING_FORCED_KEY)
        if not pending or self._close_gate is None:
            return False
        decision = pending.get("decision", "forced")
        admitted = await self._close_gate.admit(
            fp, decision=decision, state_data=fp.state_data, now_ms=now_ms, forced=True,
        )
        if not admitted.ok:
            return True
        try:
            await self._farb_repo.transition(
                fp.id,
                from_state=fp.state,
                to_state=FarbState.CLOSING_SHORT,
                state_data={
                    **admitted.state_data,
                    "exit_decision": decision,
                    "exit_requested_at_ms": pending.get("requested_at_ms", now_ms),
                },
            )
            _pkg.logger.info(
                "forced exit proceeds after a close block farb_position_id=%s decision=%s",
                fp.id, decision,
            )
        except StateConflict:
            _pkg.logger.debug(
                "state_conflict on forced exit farb_position_id=%s — skipping", fp.id,
            )
        return True
