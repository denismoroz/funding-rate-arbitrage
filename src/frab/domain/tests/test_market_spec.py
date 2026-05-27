from __future__ import annotations

import pytest

from frab.domain.market_spec import MarketSpec


def test_required_fields():
    spec = MarketSpec(
        coin="BTC",
        has_spot=True,
        has_perp=True,
        max_leverage=50,
        maint_ratio=0.03,
        min_size=0.001,
        tick_size=0.5,
    )
    assert spec.coin == "BTC"
    assert spec.max_leverage == 50
    assert spec.maint_ratio == 0.03


def test_optional_fee_fields_default_none():
    spec = MarketSpec(
        coin="ETH",
        has_spot=True,
        has_perp=True,
        max_leverage=25,
        maint_ratio=0.05,
        min_size=0.01,
        tick_size=0.01,
    )
    assert spec.spot_taker_bps is None
    assert spec.perp_taker_bps is None


def test_optional_fee_fields_set():
    spec = MarketSpec(
        coin="SOL",
        has_spot=True,
        has_perp=True,
        max_leverage=20,
        maint_ratio=0.05,
        min_size=0.1,
        tick_size=0.01,
        spot_taker_bps=5.0,
        perp_taker_bps=2.5,
    )
    assert spec.spot_taker_bps == 5.0
    assert spec.perp_taker_bps == 2.5


def test_frozen():
    spec = MarketSpec(
        coin="BTC",
        has_spot=True,
        has_perp=True,
        max_leverage=50,
        maint_ratio=0.03,
        min_size=0.001,
        tick_size=0.5,
    )
    with pytest.raises((AttributeError, TypeError)):
        spec.coin = "ETH"  # type: ignore[misc]
