"""Pure, stateless decision helpers for TwoPhaseDynamic (two-phase exit + dynamic min_hold)."""
from __future__ import annotations
from enum import StrEnum

PERIODS_PER_YEAR = 8760


class TwoPhaseDecision(StrEnum):
    NONE = "NONE"
    OPEN = "OPEN"
    CLOSE_PHASE1_NEG = "CLOSE_PHASE1_NEG"      # sustained negative rate
    CLOSE_PHASE1_CAP = "CLOSE_PHASE1_CAP"      # current rate too low to break-even
    CLOSE_PHASE2 = "CLOSE_PHASE2"              # in profit, rate dropped below threshold


def compute_position_min_hold(
    *,
    entry_signal_annual: float,
    safety_mult: float,
    base_min_hold_hours: int,
    cap_min_hold_hours: int,
    fee_round_trip_annual: float = 18.396,  # 0.0021 × 8760, HL retail (PERP+SPOT)×2 sides
) -> int:
    """Calculate position_min_hold based on entry signal.

    Formula: min_hold = min(cap, max(base, safety_mult × (fee_round_trip_annual / entry_rate)))
    When entry_signal_annual <= 0: returns cap_min_hold_hours.

    fee_round_trip_annual: annual-cost-equivalent of one full open+close cycle's fees.
    Default is 0.0021 × 8760 = 18.396 (HL retail: PERP_TAKER + SPOT_TAKER each leg, ×2 sides).

    For other exchanges / fee tiers, pass explicit value:
      HL VIP: pass 0.5 × 18.396
      Drift maker: pass 0.4 × 18.396
    """
    if entry_signal_annual > 0:
        breakeven_h = fee_round_trip_annual / entry_signal_annual
        return int(min(cap_min_hold_hours, max(base_min_hold_hours, safety_mult * breakeven_h)))
    else:
        return cap_min_hold_hours


def decide_two_phase(
    *,
    in_position: bool,
    # entry signal
    smoothed_signal_annual: float | None,
    entry_threshold: float,
    # position state (only meaningful if in_position)
    hours_in_position: int,
    position_min_hold_hours: int,
    gross_funding_so_far: float,
    total_fees_paid: float,
    consec_negative_hours: int,
    current_hourly_income_quote: float,    # POSITION_SIZE × smoothed_signal × P / 8760 (this hour)
    # exit params
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    phase2_exit_threshold: float,
    neg_overrides_min_hold: bool = False,
) -> TwoPhaseDecision:
    """Return decision based on position state + signal.

    Logic mirrors research/two_phase_dynamic.py simulate_two_phase_dynamic (138-202).

    Entry (not in_position):
        smoothed_signal_annual is None → NONE
        smoothed_signal_annual > entry_threshold → OPEN
        else → NONE

    Exit (in_position):
        in_profit = gross_funding_so_far >= total_fees_paid
        neg_overrides_min_hold and not in_profit and
            consec_negative_hours > phase1_negative_patience → CLOSE_PHASE1_NEG
            (emergency exit — bypasses the dynamic min_hold lock)
        hours_in_position < position_min_hold_hours → NONE (locked by dynamic min_hold)
        Phase 1 (not in_profit):
            consec_negative_hours > phase1_negative_patience → CLOSE_PHASE1_NEG
            current_hourly_income > 0 and hours_to_breakeven > phase1_breakeven_cap_hours → CLOSE_PHASE1_CAP
            otherwise → NONE
        Phase 2 (in_profit):
            smoothed_signal_annual < phase2_exit_threshold → CLOSE_PHASE2
            otherwise → NONE
    """
    if not in_position:
        if smoothed_signal_annual is None:
            return TwoPhaseDecision.NONE
        if smoothed_signal_annual > entry_threshold:
            return TwoPhaseDecision.OPEN
        return TwoPhaseDecision.NONE

    in_profit = gross_funding_so_far >= total_fees_paid

    # Emergency phase-1 exit: sustained-negative funding overrides the dynamic
    # min_hold lock. min_hold exists to give funding time to recoup entry fees;
    # once funding is sustainedly negative the thesis is broken — holding only
    # accrues more loss, so cut early instead of waiting out min_hold.
    if (
        neg_overrides_min_hold
        and not in_profit
        and consec_negative_hours > phase1_negative_patience
    ):
        return TwoPhaseDecision.CLOSE_PHASE1_NEG

    # in_position — check dynamic min_hold lock
    if hours_in_position < position_min_hold_hours:
        return TwoPhaseDecision.NONE

    if not in_profit:
        # Phase 1 — trying to recoup fees
        if consec_negative_hours > phase1_negative_patience:
            return TwoPhaseDecision.CLOSE_PHASE1_NEG
        if current_hourly_income_quote > 0:
            remaining_to_breakeven = total_fees_paid - gross_funding_so_far
            hours_to_breakeven = remaining_to_breakeven / current_hourly_income_quote
            if hours_to_breakeven > phase1_breakeven_cap_hours:
                return TwoPhaseDecision.CLOSE_PHASE1_CAP
        return TwoPhaseDecision.NONE
    else:
        # Phase 2 — already in profit, watch exit threshold
        if smoothed_signal_annual is not None and smoothed_signal_annual < phase2_exit_threshold:
            return TwoPhaseDecision.CLOSE_PHASE2
        return TwoPhaseDecision.NONE


def update_consec_negative(
    *,
    prev_consec_negative: int,
    smoothed_signal_annual: float | None,
) -> int:
    """Increment if signal is strictly negative, reset to 0 otherwise.

    If signal is None — return prev unchanged (no data this tick, don't reset).
    """
    if smoothed_signal_annual is None:
        return prev_consec_negative
    if smoothed_signal_annual < 0:
        return prev_consec_negative + 1
    return 0
