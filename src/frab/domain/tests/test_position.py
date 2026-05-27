from __future__ import annotations

import pytest
from datetime import datetime, timezone

from frab.domain.exchange import Exchange
from frab.domain.position import ClosedPosition, Position


_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
_EX = Exchange.HYPERLIQUID


def _make_position(**kwargs) -> Position:
    defaults = dict(
        exchange=_EX,
        coin="BTC",
        spot_qty=0.01,
        perp_qty=0.01,
        notional_usd=500.0,
        margin_reserve_usd=100.0,
        entry_spot_price=50_000.0,
        entry_perp_price=50_050.0,
        opened_at=_NOW,
    )
    defaults.update(kwargs)
    return Position(**defaults)


def test_default_state_is_empty_dict():
    pos = _make_position()
    assert pos.state == {}


def test_state_not_shared_across_instances():
    p1 = _make_position()
    p2 = _make_position()
    assert p1.state is not p2.state


def test_frozen_raises_on_assignment():
    pos = _make_position()
    with pytest.raises((AttributeError, TypeError)):
        pos.coin = "ETH"  # type: ignore[misc]


def test_explicit_state():
    pos = _make_position(state={"min_hold": 3})
    assert pos.state == {"min_hold": 3}


def test_default_fees_and_funding():
    pos = _make_position()
    assert pos.fees_paid == 0.0
    assert pos.funding_collected == 0.0


def test_equality_same_fields():
    p1 = _make_position()
    p2 = _make_position()
    assert p1 == p2


def test_inequality_different_coin():
    p1 = _make_position(coin="BTC")
    p2 = _make_position(coin="ETH")
    assert p1 != p2


def test_closed_position_fields():
    cp = ClosedPosition(
        exchange=_EX,
        coin="BTC",
        closed_at=_NOW,
        realized_pnl=42.5,
        fees_paid_total=1.5,
        funding_collected_total=3.0,
        released_margin_usd=100.0,
    )
    assert cp.realized_pnl == 42.5
    assert cp.released_margin_usd == 100.0


def test_closed_position_frozen():
    cp = ClosedPosition(
        exchange=_EX,
        coin="ETH",
        closed_at=_NOW,
        realized_pnl=0.0,
        fees_paid_total=0.0,
        funding_collected_total=0.0,
        released_margin_usd=50.0,
    )
    with pytest.raises((AttributeError, TypeError)):
        cp.coin = "BTC"  # type: ignore[misc]
