"""PreBreakevenHandler — hourly evaluator for FarbPositions in PRE_BREAKEVEN state."""
from __future__ import annotations

from datetime import datetime

from frab.domain import FarbPosition, FarbState
from frab.engine.two_phase_signals import TwoPhaseDecision, decide_two_phase, update_consec_negative
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.settings import Settings
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.evaluators.signal import SignalComputer, _HOURS_PER_YEAR


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class PreBreakevenHandler:
    """Hourly handler for FarbPositions in PRE_BREAKEVEN state.

    On each evaluation:
    1. LATCH CHECK: if gross_funding_so_far >= total_fees_paid → transition
       PRE_BREAKEVEN → POST_BREAKEVEN atomically and return (POST evaluates next hour).
    2. Otherwise call decide_two_phase(in_profit=False). If a close decision fires →
       transition PRE_BREAKEVEN → CLOSING_SHORT with exit markers written.
    3. If decision is NONE → checkpoint updated counters via update_state_data.

    This handler is NOT a subclass of the minute-tick State ABC; it is
    dispatched hourly by ExitEvaluator.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
        signal_computer: SignalComputer,
        settings: Settings,
    ) -> None:
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
        gross_funding = sd.get("gross_funding_so_far", 0.0)
        total_fees = sd.get("total_fees_paid", 0.0)
        consec_neg = sd.get("consec_negative_hours", 0)

        # Compute current hourly income quote
        if signal is not None and signal > 0:
            size_usdc = self._params.compute_size_for(fp.coin, self._settings)
            current_hourly_income = size_usdc * signal / _HOURS_PER_YEAR
        else:
            current_hourly_income = 0.0

        # Update consec_negative counter
        new_consec_neg = update_consec_negative(
            prev_consec_negative=consec_neg,
            smoothed_signal_annual=signal,
        )

        # Always include updated counters in any state_data write
        updated_sd = {
            **sd,
            "consec_negative_hours": new_consec_neg,
        }

        # ── LATCH CHECK ──────────────────────────────────────────────────────
        # If gross funding has reached break-even: latch PRE → POST atomically.
        # Do NOT also evaluate exit this tick — POST evaluates next hour.
        if gross_funding >= total_fees:
            try:
                await self._farb_repo.transition(
                    fp.id,
                    from_state=FarbState.PRE_BREAKEVEN,
                    to_state=FarbState.POST_BREAKEVEN,
                    state_data=updated_sd,
                )
                _pkg.logger.info(
                    "breakeven latch: PRE→POST farb_position_id=%s gross=%.6f fees=%.6f",
                    fp.id,
                    gross_funding,
                    total_fees,
                )
            except StateConflict:
                _pkg.logger.debug(
                    "state_conflict on PRE→POST latch farb_position_id=%s — skipping",
                    fp.id,
                )
            return

        # ── EXIT EVALUATION (Phase 1 only) ───────────────────────────────────
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
            neg_stop_threshold=self._params.neg_stop_threshold_apr,
            neg_stop_patience=self._params.neg_stop_patience_hours,
            in_profit=False,  # explicitly Phase 1
        )

        if decision != TwoPhaseDecision.NONE:
            try:
                await self._farb_repo.transition(
                    fp.id,
                    from_state=FarbState.PRE_BREAKEVEN,
                    to_state=FarbState.CLOSING_SHORT,
                    state_data={
                        **updated_sd,
                        "exit_signal_apr": signal,
                        "exit_decision": decision.value,
                    },
                )
                _pkg.logger.info(
                    "exit triggered (PRE) farb_position_id=%s decision=%s signal=%.4f",
                    fp.id,
                    decision.value,
                    signal if signal is not None else float("nan"),
                )
            except StateConflict:
                _pkg.logger.debug(
                    "state_conflict on PRE→CLOSING_SHORT farb_position_id=%s — skipping",
                    fp.id,
                )
        else:
            # Checkpoint updated counters without changing state
            await self._farb_repo.update_state_data(fp.id, updated_sd)
