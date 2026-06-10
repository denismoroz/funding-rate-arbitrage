"""Pure, stateless decision helpers for TwoPhaseDynamic (two-phase exit + dynamic min_hold)."""
from __future__ import annotations
from enum import StrEnum

PERIODS_PER_YEAR = 8760


class TwoPhaseDecision(StrEnum):
    NONE = "NONE"
    OPEN = "OPEN"
    CLOSE_PRE_BE_NEG = "CLOSE_PRE_BE_NEG"          # sustained negative rate (pre-breakeven)
    CLOSE_PRE_BE_CAP = "CLOSE_PRE_BE_CAP"          # current rate too low to break-even (pre-breakeven)
    CLOSE_PRE_BE_NEGSTOP = "CLOSE_PRE_BE_NEGSTOP"  # decisively negative — cut, bypassing min_hold
    CLOSE_POST_BE = "CLOSE_POST_BE"                 # in profit, rate dropped below threshold


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


def decide_entry(
    *,
    smoothed_signal_annual: float | None,
    entry_threshold: float,
) -> TwoPhaseDecision:
    """Entry decision (not in a position). OPEN iff signal strictly above threshold, else NONE."""
    if smoothed_signal_annual is None:
        return TwoPhaseDecision.NONE
    if smoothed_signal_annual > entry_threshold:
        return TwoPhaseDecision.OPEN
    return TwoPhaseDecision.NONE


def decide_pre_breakeven(
    *,
    smoothed_signal_annual: float | None,
    hours_in_position: int,
    position_min_hold_hours: int,
    gross_funding_so_far: float,
    total_fees_paid: float,
    consec_negative_hours: int,
    current_hourly_income_quote: float,
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    neg_stop_threshold: float = -0.15,
    neg_stop_patience: int = 6,
) -> TwoPhaseDecision:
    """PRE_BREAKEVEN (Phase 1) exit. Returns NONE / CLOSE_PRE_BE_NEGSTOP / CLOSE_PRE_BE_NEG / CLOSE_PRE_BE_CAP.

    Order (identical to the old in_profit=False path):
      1. negative hard-stop (BYPASSES min_hold): signal is not None and signal < neg_stop_threshold
         and consec_negative_hours >= neg_stop_patience  -> CLOSE_PRE_BE_NEGSTOP
      2. min_hold lock: hours_in_position < position_min_hold_hours -> NONE
      3. consec_negative_hours > phase1_negative_patience -> CLOSE_PRE_BE_NEG
      4. current_hourly_income_quote > 0 and (total_fees_paid - gross_funding_so_far)/current_hourly_income_quote
         > phase1_breakeven_cap_hours -> CLOSE_PRE_BE_CAP
      5. else NONE
    """
    # Phase-1 negative hard-stop — BYPASSES min_hold.
    if (
        smoothed_signal_annual is not None
        and smoothed_signal_annual < neg_stop_threshold
        and consec_negative_hours >= neg_stop_patience
    ):
        return TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP

    # Check dynamic min_hold lock
    if hours_in_position < position_min_hold_hours:
        return TwoPhaseDecision.NONE

    # Phase 1 — trying to recoup fees
    if consec_negative_hours > phase1_negative_patience:
        return TwoPhaseDecision.CLOSE_PRE_BE_NEG
    if current_hourly_income_quote > 0:
        remaining_to_breakeven = total_fees_paid - gross_funding_so_far
        hours_to_breakeven = remaining_to_breakeven / current_hourly_income_quote
        if hours_to_breakeven > phase1_breakeven_cap_hours:
            return TwoPhaseDecision.CLOSE_PRE_BE_CAP
    return TwoPhaseDecision.NONE


def decide_post_breakeven(
    *,
    smoothed_signal_annual: float | None,
    hours_in_position: int,
    position_min_hold_hours: int,
    phase2_exit_threshold: float,
) -> TwoPhaseDecision:
    """POST_BREAKEVEN (Phase 2) exit. Returns NONE / CLOSE_POST_BE.

    Keep the min_hold lock for behavior parity with the old in_profit=True path:
      1. hours_in_position < position_min_hold_hours -> NONE
      2. signal is not None and signal < phase2_exit_threshold -> CLOSE_POST_BE
      3. else NONE
    """
    if hours_in_position < position_min_hold_hours:
        return TwoPhaseDecision.NONE
    if smoothed_signal_annual is not None and smoothed_signal_annual < phase2_exit_threshold:
        return TwoPhaseDecision.CLOSE_POST_BE
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
