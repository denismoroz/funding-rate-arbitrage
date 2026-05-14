"""Pure, stateless signal-computation helpers for Strategy A."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

PERIODS_PER_YEAR = 8760  # hourly funding, matches research/engine.py


class Decision(StrEnum):
    NONE = "NONE"
    OPEN = "OPEN"
    CLOSE = "CLOSE"


def annualize_rate(hourly_rate: float) -> float:
    """Return hourly_rate scaled to an annual rate."""
    return hourly_rate * PERIODS_PER_YEAR


def rolling_mean(values: Sequence[float], window: int) -> float | None:
    """Return the strict mean of the last `window` values, or None if insufficient data."""
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def decide(
    *,
    in_position: bool,
    smoothed_signal: float | None,
    current_annual_rate: float,
    hours_in_position: int,
    entry_threshold: float,
    exit_threshold: float,
    min_hold_hours: int,
) -> Decision:
    """Return a trading Decision based on current position state and signal values.

    Entry uses the smoothed signal; exit uses the current raw annualized rate.
    This asymmetry is intentional and mirrors research/concurrency_cap.py:88-134.
    """
    if in_position:
        if hours_in_position >= min_hold_hours and current_annual_rate < exit_threshold:
            return Decision.CLOSE
        return Decision.NONE
    else:
        if smoothed_signal is None:
            return Decision.NONE
        if smoothed_signal > entry_threshold:
            return Decision.OPEN
        return Decision.NONE
