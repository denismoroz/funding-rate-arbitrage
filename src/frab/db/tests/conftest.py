from datetime import UTC, datetime

import pytest

from frab.db.models import Exchange, Strategy

_DEFAULT_OPENED_AT = datetime(2024, 1, 1, tzinfo=UTC)
_DEFAULT_OPENED_AT_MS = int(_DEFAULT_OPENED_AT.timestamp() * 1000)


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
