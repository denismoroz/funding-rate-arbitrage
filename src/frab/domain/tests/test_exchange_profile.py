from __future__ import annotations

import pytest

from frab.domain.exchange import Exchange
from frab.domain.exchange_profile import ExchangeProfile


_HL_PROFILE = ExchangeProfile(
    exchange=Exchange.HYPERLIQUID,
    funding_interval_hours=1.0,
    periods_per_year=24 * 365,
    default_spot_taker_bps=7.0,
    default_perp_taker_bps=3.5,
)


def test_fee_round_trip_annual_pct_hl_typical():
    # rt_bps = 2*(7+3.5) = 21; pct = 21/1e4 * 100 * (24*365)/8760 = 0.21
    result = _HL_PROFILE.fee_round_trip_annual_pct
    assert abs(result - 0.21) < 1e-9


def test_fee_round_trip_annual_pct_8h_interval():
    profile = ExchangeProfile(
        exchange=Exchange.HYPERLIQUID,
        funding_interval_hours=8.0,
        periods_per_year=24 * 365 / 8,
        default_spot_taker_bps=2.0,
        default_perp_taker_bps=1.0,
    )
    # rt_bps = 2*(2+1) = 6; periods_per_year = 1095; pct = 6/1e4*100*(1095/8760)
    expected = 6 / 1e4 * 100 * (1095 / 8760)
    assert abs(profile.fee_round_trip_annual_pct - expected) < 1e-9


def test_profile_frozen():
    with pytest.raises((AttributeError, TypeError)):
        _HL_PROFILE.funding_interval_hours = 2.0  # type: ignore[misc]


def test_profile_fields():
    assert _HL_PROFILE.exchange is Exchange.HYPERLIQUID
    assert _HL_PROFILE.periods_per_year == 24 * 365
    assert _HL_PROFILE.default_spot_taker_bps == 7.0
    assert _HL_PROFILE.default_perp_taker_bps == 3.5
