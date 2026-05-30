"""ExitEvaluator — decides when to begin closing OPEN FarbPositions."""
from __future__ import annotations

from datetime import datetime

from frab.domain import FarbPosition, FarbState
from frab.engine.two_phase_signals import TwoPhaseDecision, decide_two_phase, update_consec_negative
from frab.repo.farb_repo import FarbRepo, StateConflict
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.evaluators.signal import SignalComputer, _HOURS_PER_YEAR


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class ExitEvaluator:
    """Evaluates open FarbPositions and transitions them to CLOSING_SHORT when appropriate."""

    def __init__(
        self,
        *,
        strategy_id: int,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
        signal_computer: SignalComputer,
    ) -> None:
        self._strategy_id = strategy_id
        self._farb_repo = farb_repo
        self._params = params
        self._signal_computer = signal_computer

    async def evaluate(self, *, now_ms: int) -> None:
        """For each OPEN FarbPosition: check if we should begin closing."""
        open_fps = await self._farb_repo.list_open(self._strategy_id)
        for fp in open_fps:
            await self.evaluate_one(fp, now_ms=now_ms)

    async def evaluate_one(self, fp: FarbPosition, *, now_ms: int) -> None:
        signal = await self._signal_computer.compute(fp.coin)

        sd = fp.state_data
        opened_at_ms: int = sd.get("opened_at_ms", _dt_to_ms(fp.opened_at))
        hours_held = (now_ms - opened_at_ms) / 3_600_000

        pos_min_hold = sd.get("position_min_hold_hours", self._params.base_min_hold_hours)
        gross_funding = sd.get("gross_funding_so_far", 0.0)
        total_fees = sd.get("total_fees_paid", 0.0)
        consec_neg = sd.get("consec_negative_hours", 0)

        # Compute current hourly income quote (position_size × signal / hours_per_year)
        if signal is not None and signal > 0:
            current_hourly_income = self._params.position_size_usdc * signal / _HOURS_PER_YEAR
        else:
            current_hourly_income = 0.0

        # Update consec_negative counter in state_data
        new_consec_neg = update_consec_negative(
            prev_consec_negative=consec_neg,
            smoothed_signal_annual=signal,
        )

        decision = decide_two_phase(
            in_position=True,
            smoothed_signal_annual=signal,
            entry_threshold=self._params.entry_threshold_apr,
            hours_in_position=int(hours_held),
            position_min_hold_hours=pos_min_hold,
            gross_funding_so_far=gross_funding,
            total_fees_paid=total_fees,
            consec_negative_hours=new_consec_neg,
            current_hourly_income_quote=current_hourly_income,
            phase1_negative_patience=self._params.phase1_negative_patience,
            phase1_breakeven_cap_hours=self._params.phase1_breakeven_cap_hours,
            phase2_exit_threshold=self._params.phase2_exit_threshold,
        )

        # Always persist updated counters
        updated_sd = {
            **sd,
            "consec_negative_hours": new_consec_neg,
        }

        if decision != TwoPhaseDecision.NONE:
            try:
                await self._farb_repo.transition(
                    fp.id,
                    from_state=FarbState.OPEN,
                    to_state=FarbState.CLOSING_SHORT,
                    state_data={**updated_sd, "exit_signal_apr": signal, "exit_decision": decision.value},
                )
                _pkg.logger.info(
                    "exit triggered farb_position_id=%s decision=%s signal=%.4f",
                    fp.id,
                    decision.value,
                    signal if signal is not None else float("nan"),
                )
            except StateConflict:
                _pkg.logger.debug(
                    "state_conflict on exit transition farb_position_id=%s — skipping",
                    fp.id,
                )
        else:
            # Checkpoint updated counters without changing state
            await self._farb_repo.update_state_data(fp.id, updated_sd)
