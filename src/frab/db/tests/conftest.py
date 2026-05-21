from datetime import UTC, datetime

import pytest

from frab.db.models import Exchange, Position, Strategy

_DEFAULT_OPENED_AT = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def make_exchange():
    def _make(**kwargs):
        defaults = dict(name="HL", funding_interval_h=8, spot_taker_bps=7.0, perp_taker_bps=2.5)
        defaults.update(kwargs)
        return Exchange(**defaults)
    return _make


@pytest.fixture
def make_strategy():
    def _make(**kwargs):
        defaults = dict(name="strategy_a", version="v1", params_json={"k": 3})
        defaults.update(kwargs)
        return Strategy(**defaults)
    return _make


@pytest.fixture
def make_position():
    def _make(strategy_id: int, market_id: int, **kwargs):
        defaults = dict(
            strategy_id=strategy_id, market_id=market_id, mode="live", status="open",
            opened_at=_DEFAULT_OPENED_AT, spot_units=0.1, perp_units=0.1,
            entry_spot_price=30000.0, entry_perp_price=30010.0,
        )
        defaults.update(kwargs)
        return Position(**defaults)
    return _make
