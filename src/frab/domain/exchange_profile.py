from __future__ import annotations

from dataclasses import dataclass

from frab.domain.exchange import Exchange


@dataclass(frozen=True, slots=True)
class ExchangeProfile:
    """Per-exchange static facts used for annualization and fee math."""

    exchange: Exchange
    funding_interval_hours: float
    periods_per_year: float
    default_spot_taker_bps: float
    default_perp_taker_bps: float

    @property
    def fee_round_trip_annual_pct(self) -> float:
        """Annualized cost of one open+close round-trip in percent."""
        rt_bps = 2 * (self.default_spot_taker_bps + self.default_perp_taker_bps)
        return rt_bps / 1e4 * 100 * self.periods_per_year / 8760
