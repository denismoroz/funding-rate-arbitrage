"""Tests for the partial unique index uq_positions_farb_instrument.

Enforces: at most one Position per (farb_position_id, instrument) where
farb_position_id IS NOT NULL.  Rows with farb_position_id=None are excluded
from the constraint (partial index).
"""
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from frab.db.models import Exchange, FarbPosition, Position, Strategy
from frab.db.session import session_scope
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side

_NOW_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_exchange(**kw) -> Exchange:
    defaults = dict(name="HL", funding_interval_h=8, spot_taker_bps=7.0, perp_taker_bps=2.5)
    defaults.update(kw)
    return Exchange(**defaults)


def _make_strategy(**kw) -> Strategy:
    defaults = dict(name="strategy_a", version="v1", params_json={"k": 3})
    defaults.update(kw)
    return Strategy(**defaults)


def _make_farb_position(strategy_id: int, **kw) -> FarbPosition:
    defaults = dict(
        strategy_id=strategy_id,
        coin="BTC",
        state=FarbState.PRE_BREAKEVEN,
        state_data={},
        opened_at=_NOW_MS,
    )
    defaults.update(kw)
    return FarbPosition(**defaults)


def _make_position(exchange_id: int, farb_position_id, instrument: Instrument, **kw) -> Position:
    defaults = dict(
        exchange_id=exchange_id,
        coin="BTC",
        instrument=instrument,
        side=Side.LONG,
        qty=0.1,
        entry_price=50000.0,
        opened_at=_NOW_MS,
        status=PositionStatus.OPEN,
        farb_position_id=farb_position_id,
    )
    defaults.update(kw)
    return Position(**defaults)


# ---------------------------------------------------------------------------
# Setup fixture: exchange + strategy + one FarbPosition
# ---------------------------------------------------------------------------

@pytest.fixture
async def setup(session_factory):
    """Yield (exchange_id, farb_position_id) after inserting base rows."""
    async with session_scope(session_factory) as s:
        exc = _make_exchange(name="UQ_Test_Exchange")
        s.add(exc)
        await s.flush()

        strat = _make_strategy()
        s.add(strat)
        await s.flush()

        fp = _make_farb_position(strategy_id=strat.id)
        s.add(fp)
        await s.flush()

        exc_id = exc.id
        fp_id = fp.id

    return exc_id, fp_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_duplicate_instrument_raises_integrity_error(setup, session_factory):
    """Inserting a second Position with the same (farb_position_id, instrument) must fail."""
    exc_id, fp_id = setup

    # First position: succeeds
    async with session_scope(session_factory) as s:
        s.add(_make_position(exc_id, fp_id, Instrument.SPOT))

    # Second position with same farb_position_id + instrument: must raise
    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as s:
            s.add(_make_position(exc_id, fp_id, Instrument.SPOT))


async def test_different_instrument_succeeds(setup, session_factory):
    """Inserting (farb_position_id, SPOT) then (farb_position_id, PERP) must succeed."""
    exc_id, fp_id = setup

    async with session_scope(session_factory) as s:
        s.add(_make_position(exc_id, fp_id, Instrument.SPOT))

    # Different instrument on the same FarbPosition: must NOT raise
    async with session_scope(session_factory) as s:
        s.add(_make_position(exc_id, fp_id, Instrument.PERP, side=Side.SHORT))


async def test_null_farb_position_id_not_constrained(setup, session_factory):
    """Two Positions with farb_position_id=None and same instrument must succeed
    (partial index excludes NULL)."""
    exc_id, _fp_id = setup

    async with session_scope(session_factory) as s:
        s.add(_make_position(exc_id, None, Instrument.SPOT))

    # Second NULL row with the same instrument: must NOT raise
    async with session_scope(session_factory) as s:
        s.add(_make_position(exc_id, None, Instrument.SPOT))
