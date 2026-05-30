"""Unit tests for SignalComputer — fully mocked DB, no fixtures needed."""
from __future__ import annotations

import pytest

from frab.strategy.two_phase.evaluators.signal import SignalComputer


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_computer(session_factory, *, exchange_name: str = "HL", window: int = 3) -> SignalComputer:
    return SignalComputer(
        exchange_name=exchange_name,
        session_factory=session_factory,
        signal_window_hours=window,
    )


def _mock_session(mocker, *, exc_row=None, rates=None):
    """Build a fake async session_factory that yields a mock session.

    exc_row  -- object with .id and .funding_interval_h, or None
    rates    -- list of floats (will be returned from rates_result.all())
    """
    # session.execute(stmt) returns a result object
    # For exchange lookup: result.scalar_one_or_none() -> exc_row
    # For rates lookup:    result.all()               -> [(r,) ...]

    session = mocker.AsyncMock()

    exc_result = mocker.MagicMock()
    exc_result.scalar_one_or_none.return_value = exc_row

    if rates is not None:
        rates_result = mocker.MagicMock()
        rates_result.all.return_value = [(r,) for r in rates]
        session.execute.side_effect = [exc_result, rates_result]
    else:
        session.execute.return_value = exc_result

    # session_factory is used as an async context manager via session_scope
    # We need to patch session_scope itself to yield our mock session.
    return session


# ─── compute() tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_returns_none_when_exchange_not_found(mocker):
    """compute() returns None when the exchange row is missing."""
    session = _mock_session(mocker, exc_row=None)
    sf = mocker.MagicMock()  # session_factory (unused; we patch session_scope)
    mocker.patch(
        "frab.strategy.two_phase.evaluators.signal.session_scope",
        return_value=_async_cm(session),
    )

    computer = _make_computer(sf)
    result = await computer.compute("BTC")
    assert result is None


@pytest.mark.asyncio
async def test_compute_returns_none_when_insufficient_rates(mocker):
    """compute() returns None when fewer than signal_window_hours rates exist."""
    exc_row = mocker.MagicMock()
    exc_row.id = 1
    exc_row.funding_interval_h = 1

    # Only 2 rates but window=3
    session = _mock_session(mocker, exc_row=exc_row, rates=[0.001, 0.002])
    sf = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.evaluators.signal.session_scope",
        return_value=_async_cm(session),
    )

    computer = _make_computer(sf, window=3)
    result = await computer.compute("BTC")
    assert result is None


@pytest.mark.asyncio
async def test_compute_returns_annualized_mean_when_full_window(mocker):
    """compute() returns mean(rates) * intervals_per_year when window is full."""
    exc_row = mocker.MagicMock()
    exc_row.id = 1
    exc_row.funding_interval_h = 1  # intervals_per_year = 8760 // 1 = 8760

    rates = [0.001, 0.002, 0.003]  # window=3; mean = 0.002
    session = _mock_session(mocker, exc_row=exc_row, rates=rates)
    sf = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.evaluators.signal.session_scope",
        return_value=_async_cm(session),
    )

    computer = _make_computer(sf, window=3)
    result = await computer.compute("BTC")

    expected = (sum(rates) / len(rates)) * 8760
    assert result == pytest.approx(expected)


# ─── latest_funding_rate() tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_latest_funding_rate_returns_none_when_exchange_not_found(mocker):
    """latest_funding_rate() returns None when exchange row is missing."""
    session = mocker.AsyncMock()

    exc_result = mocker.MagicMock()
    exc_result.scalar_one_or_none.return_value = None
    session.execute.return_value = exc_result

    sf = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.evaluators.signal.session_scope",
        return_value=_async_cm(session),
    )

    computer = _make_computer(sf)
    result = await computer.latest_funding_rate("BTC")
    assert result is None


@pytest.mark.asyncio
async def test_latest_funding_rate_returns_rate_when_row_exists(mocker):
    """latest_funding_rate() returns the rate value when a row is found."""
    exc_row = mocker.MagicMock()
    exc_row.id = 1
    exc_row.funding_interval_h = 1

    session = mocker.AsyncMock()

    exc_result = mocker.MagicMock()
    exc_result.scalar_one_or_none.return_value = exc_row

    rate_result = mocker.MagicMock()
    rate_result.first.return_value = (0.00123,)

    session.execute.side_effect = [exc_result, rate_result]

    sf = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.evaluators.signal.session_scope",
        return_value=_async_cm(session),
    )

    computer = _make_computer(sf)
    result = await computer.latest_funding_rate("BTC")
    assert result == pytest.approx(0.00123)


# ─── Helper for async context manager ────────────────────────────────────────

class _async_cm:
    """Minimal async context manager that yields `obj`."""
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        pass
