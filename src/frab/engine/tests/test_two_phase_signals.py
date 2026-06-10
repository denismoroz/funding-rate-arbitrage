"""Unit tests for TwoPhaseDecision logic."""
from __future__ import annotations
import pytest
from frab.engine.two_phase_signals import (
    TwoPhaseDecision,
    compute_position_min_hold,
    decide_entry,
    decide_pre_breakeven,
    decide_post_breakeven,
    update_consec_negative,
)


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
# decide_entry — Group 1: Entry decisions
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
def test_decide_entry(
    smoothed: float | None,
    entry_threshold: float,
    expected: TwoPhaseDecision,
) -> None:
    result = decide_entry(
        smoothed_signal_annual=smoothed,
        entry_threshold=entry_threshold,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_pre_breakeven helpers
# ---------------------------------------------------------------------------

_BASE_PRE: dict = dict(
    smoothed_signal_annual=0.05,
    hours_in_position=750,
    position_min_hold_hours=720,
    gross_funding_so_far=2.0,
    total_fees_paid=4.2,
    consec_negative_hours=0,
    current_hourly_income_quote=0.001,
    phase1_negative_patience=72,
    phase1_breakeven_cap_hours=720,
)


def _pre(**overrides) -> TwoPhaseDecision:
    params = {**_BASE_PRE, **overrides}
    return decide_pre_breakeven(**params)


# ---------------------------------------------------------------------------
# decide_pre_breakeven — Group 2 PRE: Locked by dynamic min_hold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gross, fees, consec_neg, income, smoothed",
    [
        # consec would trigger phase1_neg if unlocked
        (2.0, 4.2, 100, 0.001, 0.05),
        # income would trigger phase1_cap if unlocked
        (2.0, 4.2,   0, 0.0001, 0.05),
    ],
)
def test_pre_locked_by_min_hold(
    gross: float,
    fees: float,
    consec_neg: int,
    income: float,
    smoothed: float,
) -> None:
    result = decide_pre_breakeven(
        smoothed_signal_annual=smoothed,
        hours_in_position=100,       # < position_min_hold_hours=720
        position_min_hold_hours=720,
        gross_funding_so_far=gross,
        total_fees_paid=fees,
        consec_negative_hours=consec_neg,
        current_hourly_income_quote=income,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_pre_breakeven — Group 3: No exit
# ---------------------------------------------------------------------------


def test_pre_no_exit() -> None:
    """Within patience, breakeven reachable — stay."""
    result = _pre(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,
        hours_in_position=750,
        position_min_hold_hours=720,
        consec_negative_hours=20,          # < patience=72
        current_hourly_income_quote=0.01,  # remaining=2.2, htb=220 < cap=720
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_pre_breakeven — Group 4: CLOSE_PRE_BE_NEG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "consec_neg, patience, expected",
    [
        (73, 72, TwoPhaseDecision.CLOSE_PRE_BE_NEG),  # strict > patience
        (72, 72, TwoPhaseDecision.NONE),              # boundary — equality NOT trigger
    ],
)
def test_pre_neg(
    consec_neg: int,
    patience: int,
    expected: TwoPhaseDecision,
) -> None:
    result = _pre(
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
# decide_pre_breakeven — Group 5: CLOSE_PRE_BE_CAP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "income, gross, fees, expected",
    [
        # remaining=2.0, htb=2000 > cap=720 → CLOSE_PRE_BE_CAP
        (0.001, 2.2, 4.2, TwoPhaseDecision.CLOSE_PRE_BE_CAP),
        # remaining=2.0, htb=400 < cap=720 → NONE
        (0.005, 2.2, 4.2, TwoPhaseDecision.NONE),
        # income=0, consec within patience → NONE (cap branch needs income>0)
        (0.0,   2.2, 4.2, TwoPhaseDecision.NONE),
    ],
)
def test_pre_cap(
    income: float,
    gross: float,
    fees: float,
    expected: TwoPhaseDecision,
) -> None:
    result = _pre(
        gross_funding_so_far=gross,
        total_fees_paid=fees,
        current_hourly_income_quote=income,
        consec_negative_hours=20,   # well within patience=72
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_pre_breakeven — Group 7: PRE outcomes (cap/neg priority)
# ---------------------------------------------------------------------------


def test_pre_cap_fires_when_cap_condition_met() -> None:
    """PRE: signal=-0.12 (above neg_stop), consec within patience, cap exceeded → CLOSE_PRE_BE_CAP."""
    # remaining=2.2, htb=2200 > 720 → CLOSE_PRE_BE_CAP (not CLOSE_PRE_BE_NEG)
    result = _pre(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,
        smoothed_signal_annual=-0.12,
        consec_negative_hours=20,   # within patience → no CLOSE_PRE_BE_NEG
        phase1_negative_patience=72,
        current_hourly_income_quote=0.001,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_CAP


def test_pre_neg_fires_over_cap() -> None:
    """PRE: consec > patience → CLOSE_PRE_BE_NEG (checked before cap)."""
    # smoothed=-0.12 kept above neg_stop_threshold (-0.15) to isolate CLOSE_PRE_BE_NEG.
    result = _pre(
        gross_funding_so_far=2.0,
        total_fees_paid=4.2,
        smoothed_signal_annual=-0.12,
        consec_negative_hours=80,   # > patience=72
        phase1_negative_patience=72,
        current_hourly_income_quote=0.001,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEG


# ---------------------------------------------------------------------------
# decide_pre_breakeven — Group 8: Phase-1 negative hard-stop (bypasses min_hold)
# ---------------------------------------------------------------------------


def test_pre_negstop_fires_while_locked_by_min_hold() -> None:
    """The whole point: a decisively-negative Phase-1 position is cut even though
    hours_in_position < position_min_hold_hours (the min_hold lock is bypassed).

    Mirrors the live SOL #26 case: locked at 150/720h, signal ≈ -0.22, consec 27."""
    result = _pre(
        gross_funding_so_far=-0.02,
        total_fees_paid=0.037,
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
def test_pre_negstop_threshold_and_patience(
    smoothed: float,
    consec: int,
    expected: TwoPhaseDecision,
) -> None:
    result = _pre(
        gross_funding_so_far=-0.02,
        total_fees_paid=0.037,
        smoothed_signal_annual=smoothed,
        consec_negative_hours=consec,
        hours_in_position=150,            # < min_hold → locked unless negstop bypasses
        position_min_hold_hours=720,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
    )
    assert result == expected


def test_pre_negstop_none_signal_does_not_fire() -> None:
    """No signal data → hard-stop must not fire; falls through to min_hold lock."""
    result = _pre(
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
# decide_pre_breakeven — Group 9 PRE: explicit PRE tests
# ---------------------------------------------------------------------------


def test_pre_with_negstop_bypasses_min_hold() -> None:
    """PRE_BREAKEVEN + decisively negative → CLOSE_PRE_BE_NEGSTOP, bypassing min_hold."""
    result = decide_pre_breakeven(
        smoothed_signal_annual=-0.22,
        hours_in_position=150,
        position_min_hold_hours=720,
        gross_funding_so_far=5.0,
        total_fees_paid=4.2,
        consec_negative_hours=27,
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        neg_stop_threshold=-0.15,
        neg_stop_patience=6,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP


def test_pre_neg_fires_regardless_of_gross_fees_ratio() -> None:
    """PRE function is Phase 1 by definition; consec > patience → CLOSE_PRE_BE_NEG
    even when gross > fees (phase is encoded in the function choice, not gross/fees)."""
    result = decide_pre_breakeven(
        smoothed_signal_annual=-0.12,
        hours_in_position=750,
        position_min_hold_hours=720,
        gross_funding_so_far=5.0,   # gross > fees — irrelevant to PRE logic
        total_fees_paid=4.2,
        consec_negative_hours=80,   # > patience=72
        current_hourly_income_quote=0.001,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )
    assert result == TwoPhaseDecision.CLOSE_PRE_BE_NEG


# ---------------------------------------------------------------------------
# decide_post_breakeven helpers
# ---------------------------------------------------------------------------

_BASE_POST: dict = dict(
    smoothed_signal_annual=0.05,
    hours_in_position=750,
    position_min_hold_hours=720,
    phase2_exit_threshold=-0.10,
)


def _post(**overrides) -> TwoPhaseDecision:
    params = {**_BASE_POST, **overrides}
    return decide_post_breakeven(**params)


# ---------------------------------------------------------------------------
# decide_post_breakeven — Group 2 POST: Locked by dynamic min_hold
# ---------------------------------------------------------------------------


def test_post_locked_by_min_hold() -> None:
    """POST: smoothed below phase2 threshold — would be CLOSE_POST_BE if unlocked."""
    result = decide_post_breakeven(
        smoothed_signal_annual=-1.0,
        hours_in_position=100,       # < position_min_hold_hours=720
        position_min_hold_hours=720,
        phase2_exit_threshold=-0.10,
    )
    assert result == TwoPhaseDecision.NONE


# ---------------------------------------------------------------------------
# decide_post_breakeven — Group 6: Phase 2 threshold
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
def test_post_phase2_threshold(
    smoothed: float,
    p2_threshold: float,
    expected: TwoPhaseDecision,
) -> None:
    result = _post(
        smoothed_signal_annual=smoothed,
        phase2_exit_threshold=p2_threshold,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide_post_breakeven — Group 8 POST: negstop does not apply
# ---------------------------------------------------------------------------


def test_post_deep_negative_signal_gives_close_post_be() -> None:
    """POST_BREAKEVEN position: negstop is Phase-1 only, so a deep-negative signal
    exits via CLOSE_POST_BE (not a hard-stop variant)."""
    result = _post(
        smoothed_signal_annual=-0.50,     # well below both thresholds
        phase2_exit_threshold=-0.10,
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE


# ---------------------------------------------------------------------------
# decide_post_breakeven — Group 9 POST: explicit POST tests
# ---------------------------------------------------------------------------


def test_post_negative_signal_gives_close_post_be_not_negstop() -> None:
    """POST_BREAKEVEN: deep negative signal → CLOSE_POST_BE, never a negstop variant."""
    result = decide_post_breakeven(
        smoothed_signal_annual=-0.50,
        hours_in_position=750,
        position_min_hold_hours=720,
        phase2_exit_threshold=-0.10,
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE


def test_post_fires_regardless_of_gross_fees_ratio() -> None:
    """POST function is Phase 2 by definition; signal below threshold → CLOSE_POST_BE
    even when gross < fees (phase is encoded in the function choice, not gross/fees)."""
    result = decide_post_breakeven(
        smoothed_signal_annual=-0.15,
        hours_in_position=750,
        position_min_hold_hours=720,
        phase2_exit_threshold=-0.10,
    )
    assert result == TwoPhaseDecision.CLOSE_POST_BE
