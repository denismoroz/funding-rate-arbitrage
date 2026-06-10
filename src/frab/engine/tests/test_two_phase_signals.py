"""Unit tests for TwoPhaseDecision logic."""
from __future__ import annotations
import pytest
from frab.engine.two_phase_signals import (
    TwoPhaseDecision,
    compute_position_min_hold,
    decide_two_phase,
    update_consec_negative,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_EXIT_PARAMS: dict = dict(
    in_position=True,
    smoothed_signal_annual=0.05,
    entry_threshold=0.10,
    hours_in_position=750,
    position_min_hold_hours=720,
    gross_funding_so_far=2.0,
    total_fees_paid=4.2,
    consec_negative_hours=0,
    current_hourly_income_quote=0.001,
    phase1_negative_patience=72,
    phase1_breakeven_cap_hours=720,
    phase2_exit_threshold=-0.10,
)


def _decide(**overrides) -> TwoPhaseDecision:
    params = {**_BASE_EXIT_PARAMS, **overrides}
    return decide_two_phase(**params)


# ---------------------------------------------------------------------------
# compute_position_min_hold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_signal, safety_mult, base, cap, expected",
    [
        # typical C-config: 5 × 183.96 = 919.8 → capped at 720
        (0.10,  5.0, 24, 720, 720),
        # 5 × 122.64 = 613.2 → int = 613
        (0.15,  5.0, 24, 720, 613),
        # 5 × 61.32 = 306.6 → int = 306
        (0.30,  5.0, 24, 720, 306),
        # 5 × 367.92 = 1839.6 → capped at 720
        (0.05,  5.0, 24, 720, 720),
        # 5 × 36.792 = 183.96 → int = 183
        (0.50,  5.0, 24, 720, 183),
        # 5 × 18.396 = 91.98 → int = 91
        (1.00,  5.0, 24, 720,  91),
        # 5 × 3.6792 = 18.396 < base=24 → 24
        (5.00,  5.0, 24, 720,  24),
        # rate=0 → cap
        (0.0,   5.0, 24, 720, 720),
        # rate<0 → cap
        (-0.10, 5.0, 24, 720, 720),
        # 3 × 183.96 = 551.88 → int = 551
        (0.10,  3.0, 24, 720, 551),
        # 10 × 183.96 = 1839.6 → capped at 720
        (0.10, 10.0, 24, 720, 720),
    ],
)
def test_compute_position_min_hold(
    entry_signal: float,
    safety_mult: float,
    base: int,
    cap: int,
    expected: int,
) -> None:
    result = compute_position_min_hold(
        entry_signal_annual=entry_signal,
        safety_mult=safety_mult,
        base_min_hold_hours=base,
        cap_min_hold_hours=cap,
    )
    assert result == expected


def test_compute_position_min_hold_custom_fee() -> None:
    """VIP fee tier (half of retail) produces half the min_hold (before clamping)."""
    # fee=9.198, entry=0.15, mult=5.0: 5 × (9.198/0.15) = 5 × 61.32 = 306.6 → 306
    result = compute_position_min_hold(
        entry_signal_annual=0.15,
        safety_mult=5.0,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        fee_round_trip_annual=9.198,
    )
    assert result == 306


# ---------------------------------------------------------------------------
# update_consec_negative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prev, signal, expected",
    [
        (0,  0.10,  0),   # positive resets from 0
        (5,  0.10,  0),   # positive resets from 5
        (0, -0.05,  1),   # first negative
        (5, -0.05,  6),   # increment
        (5,  0.0,   0),   # zero is NOT negative (strict <0)
        (5,  None,  5),   # no data, hold counter unchanged
    ],
)
def test_update_consec_negative(
    prev: int,
    signal: float | None,
    expected: int,
) -> None:
    result = update_consec_negative(
        prev_consec_negative=prev,
        smoothed_signal_annual=signal,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_two_phase — Group 1: Entry decisions (in_position=False)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "smoothed, entry_threshold, expected",
    [
        (None,   0.10, TwoPhaseDecision.NONE),   # no signal
        (0.05,   0.10, TwoPhaseDecision.NONE),   # below threshold
        (0.10,   0.10, TwoPhaseDecision.NONE),   # equal — strict >, not triggered
        (0.15,   0.10, TwoPhaseDecision.OPEN),   # above threshold
        (-0.05,  0.10, TwoPhaseDecision.NONE),   # negative
    ],
)
def test_decide_entry_branch(
    smoothed: float | None,
    entry_threshold: float,
    expected: TwoPhaseDecision,
) -> None:
    result = decide_two_phase(
        in_position=False,
        smoothed_signal_annual=smoothed,
        entry_threshold=entry_threshold,
        hours_in_position=0,
        position_min_hold_hours=720,
        gross_funding_so_far=0.0,
        total_fees_paid=0.0,
        consec_negative_hours=0,
        current_hourly_income_quote=0.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_two_phase — Group 2: Locked by dynamic min_hold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gross, fees, consec_neg, income, smoothed, in_profit",
    [
        # PRE, consec would trigger phase1_neg if unlocked
        (2.0, 4.2, 100, 0.001, 0.05, False),
        # POST, smoothed below phase2 threshold — would be CLOSE_POST_BE if unlocked
        (5.0, 4.2,   0, 0.001, -1.0, True),
        # PRE, income would trigger phase1_cap if unlocked
        (2.0, 4.2,   0, 0.0001, 0.05, False),
    ],
)
def test_decide_locked_by_min_hold(
    gross: float,
    fees: float,
    consec_neg: int,
    income: float,
    smoothed: float,
    in_profit: bool,
) -> None:
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=smoothed,
        entry_threshold=0.10,
        hours_in_position=100,       # < position_min_hold_hours=720
        position_min_hold_hours=720,
        gross_funding_so_far=gross,
        total_fees_paid=fees,
        consec_negative_hours=consec_neg,
        current_hourly_income_quote=income,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        in_profit=in_profit,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_two_phase — Group 3: Phase 1 (not in_profit), no exit
# ---------------------------------------------------------------------------


def test_decide_phase1_no_exit() -> None:
    """Not in profit, within patience, breakeven reachable — stay."""
    result = _decide(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,
        hours_in_position=750,
        position_min_hold_hours=720,
        consec_negative_hours=20,       # < patience=72
        current_hourly_income_quote=0.01,  # remaining=2.2, htb=220 < cap=720
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_two_phase — Group 4: Phase 1, CLOSE_PRE_BE_NEG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "consec_neg, patience, expected",
    [
        (73, 72, TwoPhaseDecision.CLOSE_PRE_BE_NEG),  # strict > patience
        (72, 72, TwoPhaseDecision.NONE),              # boundary — equality NOT trigger
    ],
)
def test_decide_phase1_neg(
    consec_neg: int,
    patience: int,
    expected: TwoPhaseDecision,
) -> None:
    result = _decide(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,
        consec_negative_hours=consec_neg,
        phase1_negative_patience=patience,
        # ensure breakeven cap won't fire first (income high enough)
        current_hourly_income_quote=1.0,
        phase1_breakeven_cap_hours=720,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_two_phase — Group 5: Phase 1, CLOSE_PRE_BE_CAP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "income, gross, fees, expected",
    [
        # remaining=2.0, htb=2000 > cap=720 → CLOSE_PRE_BE_CAP
        (0.001, 2.2, 4.2, TwoPhaseDecision.CLOSE_PRE_BE_CAP),
        # remaining=2.0, htb=400 < cap=720 → NONE
        (0.005, 2.2, 4.2, TwoPhaseDecision.NONE),
        # income=0 (or negative), consec within patience → NONE (cap branch needs income>0)
        (0.0,   2.2, 4.2, TwoPhaseDecision.NONE),
    ],
)
def test_decide_phase1_cap(
    income: float,
    gross: float,
    fees: float,
    expected: TwoPhaseDecision,
) -> None:
    result = _decide(
        gross_funding_so_far=gross,
        total_fees_paid=fees,
        current_hourly_income_quote=income,
        consec_negative_hours=20,   # well within patience=72
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_two_phase — Group 6: Phase 2 (in_profit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "smoothed, p2_threshold, expected",
    [
        # above threshold → stay
        (-0.05, -0.10, TwoPhaseDecision.NONE),
        # boundary — equality NOT trigger (strict <)
        (-0.10, -0.10, TwoPhaseDecision.NONE),
        # below threshold → close
        (-0.15, -0.10, TwoPhaseDecision.CLOSE_POST_BE),
        # well below → close
        (-1.00, -0.10, TwoPhaseDecision.CLOSE_POST_BE),
    ],
)
def test_decide_phase2(
    smoothed: float,
    p2_threshold: float,
    expected: TwoPhaseDecision,
) -> None:
    result = _decide(
        gross_funding_so_far=5.0,
        total_fees_paid=4.2,
        smoothed_signal_annual=smoothed,
        phase2_exit_threshold=p2_threshold,
        consec_negative_hours=0,
        current_hourly_income_quote=0.001,
        in_profit=True,   # POST_BREAKEVEN
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_two_phase — Group 7: Phase 1 priority over Phase 2
# ---------------------------------------------------------------------------


def test_decide_phase1_priority_over_phase2() -> None:
    """Not in profit → must stay in Phase 1, even if signal would trigger Phase 2."""
    # smoothed=-0.12 would trigger CLOSE_POST_BE if in_profit (< -0.10), but gross < fees.
    # Kept above neg_stop_threshold (-0.15) so the hard-stop does not pre-empt this case.
    result = _decide(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,        # not in profit
        smoothed_signal_annual=-0.12,
        consec_negative_hours=20,   # within patience → no CLOSE_PRE_BE_NEG
        phase1_negative_patience=72,
        current_hourly_income_quote=0.001,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
    )
    # remaining=2.2, htb=2200 > 720 → CLOSE_PRE_BE_CAP (NOT CLOSE_POST_BE)
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_CAP


def test_decide_phase1_priority_neg_trigger() -> None:
    """Not in profit + consec > patience → CLOSE_PRE_BE_NEG, not CLOSE_POST_BE."""
    # smoothed=-0.12 kept above neg_stop_threshold (-0.15) to isolate CLOSE_PRE_BE_NEG.
    result = _decide(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,        # not in profit
        smoothed_signal_annual=-0.12,
        consec_negative_hours=80,   # > patience=72
        phase1_negative_patience=72,
        current_hourly_income_quote=0.001,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEG


# ---------------------------------------------------------------------------
# decide_two_phase — Group 8: Phase-1 negative hard-stop (bypasses min_hold)
# ---------------------------------------------------------------------------


def test_negstop_fires_while_locked_by_min_hold() -> None:
    """The whole point: a decisively-negative Phase-1 position is cut even though
    hours_in_position < position_min_hold_hours (the min_hold lock is bypassed).

    Mirrors the live SOL #26 case: locked at 150/720h, signal ≈ -0.22, consec 27."""
    result = _decide(
        gross_funding_so_far=-0.02,
        total_fees_paid=0.037,            # not in profit (Phase 1)
        smoothed_signal_annual=-0.22,     # < neg_stop_threshold (-0.15)
        consec_negative_hours=27,         # >= neg_stop_patience (6)
        hours_in_position=150,
        position_min_hold_hours=720,      # still locked — must be bypassed
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP


@pytest.mark.parametrize(
    "smoothed, consec, expected",
    [
        # deep enough + patient enough → fire (bypasses lock)
        (-0.16, 6,  TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP),
        (-1.00, 6,  TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP),
        # boundary: == threshold is NOT < threshold → no fire (locked → NONE)
        (-0.15, 50, TwoPhaseDecision.NONE),
        # not deep enough (the live ETH case: -0.04, consec 12) → no fire (locked)
        (-0.04, 12, TwoPhaseDecision.NONE),
        # deep but patience not met (consec < 6) → no fire (locked)
        (-0.50, 5,  TwoPhaseDecision.NONE),
        # boundary: consec == patience (6) counts (>=) → fire
        (-0.50, 6,  TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP),
    ],
)
def test_negstop_threshold_and_patience(
    smoothed: float,
    consec: int,
    expected: TwoPhaseDecision,
) -> None:
    result = _decide(
        gross_funding_so_far=-0.02,
        total_fees_paid=0.037,            # Phase 1
        smoothed_signal_annual=smoothed,
        consec_negative_hours=consec,
        hours_in_position=150,            # < min_hold → locked unless negstop bypasses
        position_min_hold_hours=720,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
    )
    assert result == expected


def test_negstop_does_not_fire_in_phase2() -> None:
    """In profit (Phase 2): a deep-negative signal exits via CLOSE_POST_BE, never
    via the hard-stop (which is Phase-1 only)."""
    result = _decide(
        gross_funding_so_far=5.0,
        total_fees_paid=4.2,              # in profit (Phase 2)
        smoothed_signal_annual=-0.50,     # well below both thresholds
        consec_negative_hours=30,
        hours_in_position=750,
        position_min_hold_hours=720,
        phase2_exit_threshold=-0.10,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
        in_profit=True,                   # POST_BREAKEVEN
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE


def test_negstop_none_signal_does_not_fire() -> None:
    """No signal data → hard-stop must not fire; falls through to min_hold lock."""
    result = _decide(
        gross_funding_so_far=-0.02,
        total_fees_paid=0.037,
        smoothed_signal_annual=None,
        consec_negative_hours=30,
        hours_in_position=150,
        position_min_hold_hours=720,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_two_phase — Group 9: Explicit in_profit param (persisted phase latch)
# ---------------------------------------------------------------------------


def test_decide_explicit_in_profit_false_overrides_gross() -> None:
    """Caller passes in_profit=False (PRE_BREAKEVEN state) even though gross >= fees.
    The persisted phase must be respected — not recomputed from gross/fees."""
    # gross > fees would normally imply in_profit=True → CLOSE_POST_BE at -0.12 signal.
    # But caller supplies in_profit=False (position latched as PRE), so Phase-1 logic
    # applies: consec=80 > patience=72 → CLOSE_PRE_BE_NEG (not CLOSE_POST_BE).
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=-0.12,
        entry_threshold=0.10,
        hours_in_position=750,
        position_min_hold_hours=720,
        gross_funding_so_far=5.0,   # would imply in_profit if recomputed
        total_fees_paid=4.2,        # gross > fees — latch overrides this
        consec_negative_hours=80,
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        in_profit=False,            # explicit: caller says still pre-breakeven
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEG


def test_decide_explicit_in_profit_true_overrides_gross() -> None:
    """Caller passes in_profit=True (POST_BREAKEVEN state) even though gross < fees.
    The persisted latch keeps Phase-2 logic active."""
    # gross < fees would normally imply Phase 1.  But latch says POST_BREAKEVEN.
    # Signal -0.15 < phase2_exit_threshold -0.10 → CLOSE_POST_BE.
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=-0.15,
        entry_threshold=0.10,
        hours_in_position=750,
        position_min_hold_hours=720,
        gross_funding_so_far=2.0,   # would imply Phase 1 if recomputed
        total_fees_paid=4.2,        # gross < fees — latch overrides this
        consec_negative_hours=0,
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        in_profit=True,             # explicit: caller says post-breakeven (latched)
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE


def test_decide_default_in_profit_is_pre_breakeven() -> None:
    """When in_profit is not supplied it defaults to False (pre-breakeven) —
    NEVER recomputed from gross/fees. gross=5.0 >= fees=4.2 would imply Phase 2
    if it were recomputed, but the default keeps us in Phase 1: consec=80 >
    patience=72 → CLOSE_PRE_BE_NEG (not CLOSE_POST_BE)."""
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=-0.12,   # kept above neg_stop (-0.15) to isolate
        entry_threshold=0.10,
        hours_in_position=750,
        position_min_hold_hours=720,
        gross_funding_so_far=5.0,        # would imply Phase 2 if recomputed
        total_fees_paid=4.2,
        consec_negative_hours=80,        # > patience=72
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        # in_profit not supplied → defaults to False (pre-breakeven)
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEG


def test_decide_explicit_pre_with_negstop() -> None:
    """PRE_BREAKEVEN + decisively negative → CLOSE_PRE_BE_NEGSTOP, bypassing min_hold."""
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=-0.22,
        entry_threshold=0.10,
        hours_in_position=150,
        position_min_hold_hours=720,
        gross_funding_so_far=5.0,   # would be in_profit without explicit override
        total_fees_paid=4.2,
        consec_negative_hours=27,
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
        in_profit=False,            # explicit: PRE_BREAKEVEN
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP


def test_decide_explicit_post_with_negative_signal_does_not_negstop() -> None:
    """POST_BREAKEVEN position: negstop is Phase-1 only, so deep signal → CLOSE_POST_BE."""
    result = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=-0.50,
        entry_threshold=0.10,
        hours_in_position=750,
        position_min_hold_hours=720,
        gross_funding_so_far=2.0,   # would be Phase 1 without explicit override
        total_fees_paid=4.2,
        consec_negative_hours=30,
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
        in_profit=True,             # explicit: POST_BREAKEVEN
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE
