"""Tests for strategies/base.py — dataclasses and abstract Strategy."""
from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError

from frab.exchanges.base import Fill, FundingTick, Leg, Quote, Side
from frab.strategies.base import EquitySnapshot, SignalEvent, Strategy, TickReport

T0 = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Helper: concrete subclass for testing
# ---------------------------------------------------------------------------

class _ConcreteStrategy(Strategy):
    name = "test_strategy"
    version = "v0"

    async def on_minute_tick(self, now_ms: int, quotes: dict[str, Quote]) -> None:
        return None

    async def on_hour_tick(self, now_ms: int, funding: dict[str, FundingTick]) -> TickReport:
        return TickReport(ts_ms=now_ms, signals=(), fills=(), opened=(), closed=())

    def compute_equity(self, now_ms: int) -> EquitySnapshot:
        return EquitySnapshot(
            ts_ms=now_ms,
            total_equity=0.0,
            cash=0.0,
            spot_value=0.0,
            perp_unrealized=0.0,
            perp_realized_cum=0.0,
            funding_cum=0.0,
            fees_cum=0.0,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_signal_event_frozen():
    ev = SignalEvent(coin="BTC", ts_ms=T0, signal_value=1.0, regime_pass=True, action="NONE")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        ev.coin = "X"  # type: ignore[misc]


def test_tick_report_defaults():
    report = TickReport(
        ts_ms=T0,
        signals=(SignalEvent(coin="BTC", ts_ms=T0, signal_value=0.5, regime_pass=True, action="OPEN"),),
        fills=(),
        opened=("BTC",),
        closed=(),
    )
    assert isinstance(report.signals, tuple)
    assert isinstance(report.fills, tuple)
    assert isinstance(report.opened, tuple)
    assert isinstance(report.closed, tuple)
    assert report.ts_ms == T0


def test_equity_snapshot_fields():
    snap = EquitySnapshot(
        ts_ms=T0,
        total_equity=10_000.0,
        cash=5_000.0,
        spot_value=3_000.0,
        perp_unrealized=2_000.0,
        perp_realized_cum=100.0,
        funding_cum=50.0,
        fees_cum=10.0,
    )
    assert snap.ts_ms == T0
    assert snap.total_equity == pytest.approx(10_000.0)
    assert snap.cash == pytest.approx(5_000.0)
    assert snap.spot_value == pytest.approx(3_000.0)
    assert snap.perp_unrealized == pytest.approx(2_000.0)
    assert snap.perp_realized_cum == pytest.approx(100.0)
    assert snap.funding_cum == pytest.approx(50.0)
    assert snap.fees_cum == pytest.approx(10.0)


def test_strategy_abstract():
    # Direct instantiation of abstract class raises TypeError
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]

    # Concrete subclass can be instantiated
    strat = _ConcreteStrategy()
    assert strat.name == "test_strategy"
    assert strat.version == "v0"
