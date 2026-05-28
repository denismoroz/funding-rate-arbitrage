"""Tests for domain dataclasses and enums — dumb data, no business logic."""
from datetime import UTC, datetime

import pytest

from frab.domain import FarbPosition, FarbState, Instrument, Position, PositionStatus, Side


# ---------------------------------------------------------------------------
# Enum value stability
# ---------------------------------------------------------------------------

def test_instrument_values():
    assert Instrument.SPOT == "spot"
    assert Instrument.PERP == "perp"
    assert Instrument.COLLATERAL == "collateral"


def test_side_values():
    assert Side.LONG == "long"
    assert Side.SHORT == "short"
    assert Side.NONE == "none"


def test_position_status_values():
    assert PositionStatus.OPEN == "open"
    assert PositionStatus.CLOSED == "closed"


def test_farb_state_values():
    assert FarbState.CHECK_MARGIN == "check_margin"
    assert FarbState.OPENING_MARGIN == "opening_margin"
    assert FarbState.MARGIN_RESERVED == "margin_reserved"
    assert FarbState.OPENING_LONG == "opening_long"
    assert FarbState.LONG_OPENED == "long_opened"
    assert FarbState.OPENING_SHORT == "opening_short"
    assert FarbState.OPEN == "open"
    assert FarbState.CLOSING_SHORT == "closing_short"
    assert FarbState.SHORT_CLOSED == "short_closed"
    assert FarbState.CLOSING_LONG == "closing_long"
    assert FarbState.LONG_CLOSED == "long_closed"
    assert FarbState.RELEASING_MARGIN == "releasing_margin"
    assert FarbState.CLOSED == "closed"
    assert FarbState.FAILED == "failed"


def test_enums_are_str_subclass():
    assert isinstance(Instrument.SPOT, str)
    assert isinstance(Side.LONG, str)
    assert isinstance(PositionStatus.OPEN, str)
    assert isinstance(FarbState.OPEN, str)


# ---------------------------------------------------------------------------
# Dataclass construction + frozen invariant
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_position_construction():
    pos = Position(
        id=1,
        exchange_name="hyperliquid",
        coin="BTC",
        instrument=Instrument.SPOT,
        side=Side.LONG,
        qty=0.1,
        entry_price=50000.0,
        opened_at=_NOW,
        closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=42,
    )
    assert pos.id == 1
    assert pos.exchange_name == "hyperliquid"
    assert pos.coin == "BTC"
    assert pos.instrument == Instrument.SPOT
    assert pos.side == Side.LONG
    assert pos.qty == 0.1
    assert pos.entry_price == 50000.0
    assert pos.opened_at == _NOW
    assert pos.closed_at is None
    assert pos.status == PositionStatus.OPEN
    assert pos.farb_position_id == 42


def test_position_is_frozen():
    pos = Position(
        id=None,
        exchange_name="hl",
        coin="ETH",
        instrument=Instrument.PERP,
        side=Side.SHORT,
        qty=1.0,
        entry_price=3000.0,
        opened_at=_NOW,
        closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass of AttributeError
        pos.qty = 2.0  # type: ignore[misc]


def test_position_collateral():
    pos = Position(
        id=None,
        exchange_name="hl",
        coin="USDC",
        instrument=Instrument.COLLATERAL,
        side=Side.NONE,
        qty=1000.0,
        entry_price=1.0,
        opened_at=_NOW,
        closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=5,
    )
    assert pos.instrument == Instrument.COLLATERAL
    assert pos.side == Side.NONE
    assert pos.entry_price == 1.0


def test_farb_position_construction():
    fp = FarbPosition(
        id=10,
        strategy_id=1,
        coin="BTC",
        state=FarbState.OPEN,
        state_data={"min_hold_hours": 12, "phase": "hold"},
        spot_position_id=100,
        perp_position_id=101,
        margin_position_id=102,
        opened_at=_NOW,
        closed_at=None,
    )
    assert fp.id == 10
    assert fp.strategy_id == 1
    assert fp.coin == "BTC"
    assert fp.state == FarbState.OPEN
    assert fp.state_data["min_hold_hours"] == 12
    assert fp.spot_position_id == 100
    assert fp.perp_position_id == 101
    assert fp.margin_position_id == 102
    assert fp.opened_at == _NOW
    assert fp.closed_at is None


def test_farb_position_is_frozen():
    fp = FarbPosition(
        id=None,
        strategy_id=1,
        coin="SOL",
        state=FarbState.CHECK_MARGIN,
        state_data={},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=_NOW,
        closed_at=None,
    )
    with pytest.raises(Exception):
        fp.state = FarbState.OPEN  # type: ignore[misc]


def test_farb_position_nullable_ids():
    fp = FarbPosition(
        id=None,
        strategy_id=2,
        coin="ETH",
        state=FarbState.FAILED,
        state_data={},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=_NOW,
        closed_at=_NOW,
    )
    assert fp.id is None
    assert fp.spot_position_id is None
    assert fp.perp_position_id is None
    assert fp.margin_position_id is None
    assert fp.closed_at == _NOW
