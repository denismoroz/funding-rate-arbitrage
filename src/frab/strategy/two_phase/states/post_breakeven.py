"""PostBreakevenHandler — hourly evaluator for FarbPositions in POST_BREAKEVEN state."""
from __future__ import annotations

from datetime import datetime

from frab.domain import FarbPosition, FarbState
from frab.engine.two_phase_signals import TwoPhaseDecision, decide_post_breakeven, update_consec_negative
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.strategy.two_phase.close_gate import CloseGate
from frab.settings import Settings
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.evaluators.signal import SignalComputer


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class PostBreakevenHandler:
    """Hourly handler for FarbPositions in POST_BREAKEVEN state.

    On each evaluation:
    1. Call decide_post_breakeven(). Only CLOSE_POST_BE can fire
       (signal < phase2_exit_threshold AND funding handed back from its peak
       >= phase2_giveback_fee_mult × total_fees_paid).
    2. If decision fires → transition POST_BREAKEVEN → CLOSING_SHORT.
    3. If NONE → checkpoint updated counters.
    4. NEVER transitions back to PRE_BREAKEVEN, even if gross_funding dips below
       total_fees_paid (the latch is one-way).
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

    async def evaluate_one(self, fp: FarbPosition, *, now_ms: int) -> None:
        signal = await self._signal_computer.compute(fp.coin)

        sd = fp.state_data
        opened_at_ms: int = sd.get("opened_at_ms", _dt_to_ms(fp.opened_at))
        hours_held = (now_ms - opened_at_ms) / 3_600_000

        pos_min_hold = sd.get("position_min_hold_hours", self._params.base_min_hold_hours)
        consec_neg = sd.get("consec_negative_hours", 0)
        gross_funding = sd.get("gross_funding_so_far", 0.0)
        total_fees = sd.get("total_fees_paid", 0.0)
        # High-water mark of cumulative funding. Positions that latched before
        # this key existed start their peak at the current value.
        peak_funding = max(sd.get("peak_gross_funding", gross_funding), gross_funding)

        # Update consec_negative counter
        new_consec_neg = update_consec_negative(
            prev_consec_negative=consec_neg,
            smoothed_signal_annual=signal,
        )

        updated_sd = {
            **sd,
            "consec_negative_hours": new_consec_neg,
            "peak_gross_funding": peak_funding,
        }

        # ── EXIT EVALUATION (Phase 2 only) ───────────────────────────────────
        # The latch is one-way: POST_BREAKEVEN never reverts to PRE.
        decision = decide_post_breakeven(
            smoothed_signal_annual=signal,
            hours_in_position=int(hours_held),
            position_min_hold_hours=pos_min_hold,
            phase2_exit_threshold=self._params.phase2_exit_threshold,
            funding_giveback=peak_funding - gross_funding,
            total_fees_paid=total_fees,
            giveback_fee_mult=self._params.phase2_giveback_fee_mult,
        )

        if decision != TwoPhaseDecision.NONE:
            exit_sd = updated_sd
            if self._close_gate is not None:
                # Close nothing unless the whole position can be closed within HL's limits.
                admitted = await self._close_gate.admit(
                    fp, decision=decision.value, state_data=updated_sd, now_ms=now_ms,
                )
                if not admitted.ok:
                    return
                exit_sd = admitted.state_data
            try:
                await self._farb_repo.transition(
                    fp.id,
                    from_state=FarbState.POST_BREAKEVEN,
                    to_state=FarbState.CLOSING_SHORT,
                    state_data={
                        **exit_sd,
                        "exit_signal_apr": signal,
                        "exit_decision": decision.value,
                    },
                )
                _pkg.logger.info(
                    "exit triggered (POST) farb_position_id=%s decision=%s signal=%.4f",
                    fp.id,
                    decision.value,
                    signal if signal is not None else float("nan"),
                )
            except StateConflict:
                _pkg.logger.debug(
                    "state_conflict on POST→CLOSING_SHORT farb_position_id=%s — skipping",
                    fp.id,
                )
        else:
            if self._close_gate is not None:
                # No longer needs to exit: lift a close block left by an earlier hour.
                updated_sd = await self._close_gate.release(
                    fp, state_data=updated_sd, now_ms=now_ms,
                )
            # Checkpoint updated counters without changing state
            await self._farb_repo.update_state_data(fp.id, updated_sd)
