"""Tests for src/frab/engine/signals.py."""

from __future__ import annotations

import pytest

from frab.engine.signals import Decision, annualize_rate, decide, rolling_mean


# ---------------------------------------------------------------------------
# annualize_rate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hourly_rate, expected",
    [
        (0.0, 0.0),
        (0.0001, 0.876),
        (-0.0002, -1.752),
        (1e-6, 0.00876),
    ],
)
def test_annualize_rate(hourly_rate: float, expected: float) -> None:
    assert annualize_rate(hourly_rate) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# rolling_mean — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, window, expected",
    [
        ([1.0, 2.0, 3.0], 3, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 3, 3.0),
        ([5.0], 1, 5.0),
        ([1.0, 2.0, 3.0, 4.0, 5.0], 5, 3.0),
    ],
)
def test_rolling_mean_happy(values: list[float], window: int, expected: float) -> None:
    assert rolling_mean(values, window) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# rolling_mean — insufficient data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, window",
    [
        ([], 1),
        ([1.0], 2),
        ([1.0, 2.0], 3),
    ],
)
def test_rolling_mean_insufficient(values: list[float], window: int) -> None:
    assert rolling_mean(values, window) is None


# ---------------------------------------------------------------------------
# rolling_mean — invalid window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", [0, -1])
def test_rolling_mean_invalid_window(window: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        rolling_mean([1.0, 2.0, 3.0], window)


# ---------------------------------------------------------------------------
# rolling_mean — last-N semantics
# ---------------------------------------------------------------------------


def test_rolling_mean_last_n_semantics() -> None:
    """Only the last 3 values (1, 2, 3) should be averaged, not the leading 10/20/30."""
    result = rolling_mean([10.0, 20.0, 30.0, 1.0, 2.0, 3.0], 3)
    assert result == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# decide — helper
# ---------------------------------------------------------------------------

_BASE_PARAMS: dict = dict(
    in_position=False,
    smoothed_signal=0.5,
    current_annual_rate=0.0,
    hours_in_position=0,
    entry_threshold=0.30,
    exit_threshold=-0.15,
    min_hold_hours=120,
)


def _decide(**overrides) -> Decision:
    params = {**_BASE_PARAMS, **overrides}
    return decide(**params)


# ---------------------------------------------------------------------------
# decide — entry branch (in_position=False)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "smoothed, entry_threshold, expected",
    [
        (None, 0.30, Decision.NONE),
        (0.5, 0.30, Decision.OPEN),
        (0.30, 0.30, Decision.NONE),   # strict greater-than
        (0.0, 0.30, Decision.NONE),
        (-0.5, 0.30, Decision.NONE),
    ],
)
def test_decide_entry_branch(
    smoothed: float | None, entry_threshold: float, expected: Decision
) -> None:
    result = _decide(
        in_position=False,
        smoothed_signal=smoothed,
        entry_threshold=entry_threshold,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide — exit branch (in_position=True)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hours, current, exit_threshold, min_hold, expected",
    [
        (120, -0.20, -0.15, 120, Decision.CLOSE),   # boundary: hours >= min_hold
        (121, -0.16, -0.15, 120, Decision.CLOSE),
        (119, -0.50, -0.15, 120, Decision.NONE),    # under min_hold
        (200, -0.10, -0.15, 120, Decision.NONE),    # rate not below exit_threshold
        (200, -0.15, -0.15, 120, Decision.NONE),    # strict less-than
        (200, 0.50, -0.15, 120, Decision.NONE),     # positive funding
    ],
)
def test_decide_exit_branch(
    hours: int,
    current: float,
    exit_threshold: float,
    min_hold: int,
    expected: Decision,
) -> None:
    result = _decide(
        in_position=True,
        hours_in_position=hours,
        current_annual_rate=current,
        exit_threshold=exit_threshold,
        min_hold_hours=min_hold,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# decide — smoothed_signal ignored when in_position
# ---------------------------------------------------------------------------


def test_decide_smoothed_ignored_when_in_position() -> None:
    """None smoothed_signal must not block an exit decision."""
    result = _decide(
        in_position=True,
        smoothed_signal=None,
        hours_in_position=200,
        current_annual_rate=-0.30,
        exit_threshold=-0.15,
        min_hold_hours=120,
    )
    assert result == Decision.CLOSE
