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
    # Phase-1 negative hard-stop (bypasses min_hold). Defaults match prod config.
    neg_stop_threshold: float = -0.15,
    neg_stop_patience: int = 6,
    # Phase supplied by caller from fp.state (PRE_BREAKEVEN → in_profit=False,
    # POST_BREAKEVEN → in_profit=True).  When None the phase is reconstructed from
    # gross_funding_so_far >= total_fees_paid for backwards compatibility, but callers
    # SHOULD supply it explicitly so the persisted latch is respected.
    in_profit: bool | None = None,
) -> TwoPhaseDecision:
    """Return decision based on position state + signal.

    Design choice — ``in_profit: bool`` (not ``phase: FarbState``):
    The function already had an ``in_profit`` boolean branch; keeping the same
    type avoids importing FarbState here and keeps the pure-signal module free
    of domain-model coupling.  The caller converts FarbState → bool before
    calling: PRE_BREAKEVEN → False, POST_BREAKEVEN → True.

    The phase MUST be supplied by the caller from the persisted FarbState
    (fp.state), not recomputed here, so that a position latched into
    POST_BREAKEVEN stays there even if gross_funding_so_far dips below
    total_fees_paid again (hysteresis).

    gross_funding_so_far / total_fees_paid are still accepted because callers
    (W2 handler) need them for the LATCH check (deciding when to transition
    PRE_BREAKEVEN → POST_BREAKEVEN).  decide_two_phase no longer uses them to
    derive in_profit when in_profit is supplied.

    Logic mirrors research/two_phase_dynamic.py simulate_two_phase_dynamic (138-202),
    plus the Phase-1 negative hard-stop validated in research/two_phase_negstop.py.

    Entry (not in_position):
        smoothed_signal_annual is None → NONE
        smoothed_signal_annual > entry_threshold → OPEN
        else → NONE

    Exit (in_position):
        Phase is taken from the supplied ``in_profit`` parameter (or falls back to
        gross_funding_so_far >= total_fees_paid when in_profit is None).
        Phase-1 negative hard-stop (checked BEFORE min_hold lock — it bypasses it):
            not in_profit and smoothed_signal_annual < neg_stop_threshold
            and consec_negative_hours >= neg_stop_patience → CLOSE_PRE_BE_NEGSTOP
        hours_in_position < position_min_hold_hours → NONE (locked by dynamic min_hold)
        Phase 1 (not in_profit):
            consec_negative_hours > phase1_negative_patience → CLOSE_PRE_BE_NEG
            current_hourly_income > 0 and hours_to_breakeven > phase1_breakeven_cap_hours → CLOSE_PRE_BE_CAP
            otherwise → NONE
        Phase 2 (in_profit):
            smoothed_signal_annual < phase2_exit_threshold → CLOSE_POST_BE
            otherwise → NONE
    """
    if not in_position:
        if smoothed_signal_annual is None:
            return TwoPhaseDecision.NONE
        if smoothed_signal_annual > entry_threshold:
            return TwoPhaseDecision.OPEN
        return TwoPhaseDecision.NONE

    # Resolve phase: use caller-supplied value when available (persisted latch);
    # fall back to recomputation only when in_profit is not provided.
    if in_profit is None:
        in_profit = gross_funding_so_far >= total_fees_paid

    # Phase-1 negative hard-stop — BYPASSES min_hold. Only while still trying to
    # recoup fees (Phase 1): if the smoothed signal is decisively negative and has
    # been negative for >= neg_stop_patience hours, cut now rather than sit under
    # the min_hold lock bleeding funding. min_hold protects against fee churn on
    # mild/transient negativity, NOT against a decisive funding flip.
    if (
        not in_profit
        and smoothed_signal_annual is not None
        and smoothed_signal_annual < neg_stop_threshold
        and consec_negative_hours >= neg_stop_patience
    ):
        return TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP

    # in_position — check dynamic min_hold lock
    if hours_in_position < position_min_hold_hours:
        return TwoPhaseDecision.NONE

    if not in_profit:
        # Phase 1 — trying to recoup fees
        if consec_negative_hours > phase1_negative_patience:
            return TwoPhaseDecision.CLOSE_PRE_BE_NEG
        if current_hourly_income_quote > 0:
            remaining_to_breakeven = total_fees_paid - gross_funding_so_far
            hours_to_breakeven = remaining_to_breakeven / current_hourly_income_quote
            if hours_to_breakeven > phase1_breakeven_cap_hours:
                return TwoPhaseDecision.CLOSE_PRE_BE_CAP
        return TwoPhaseDecision.NONE
    else:
        # Phase 2 — already in profit, watch exit threshold
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
