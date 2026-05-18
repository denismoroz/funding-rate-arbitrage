from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.exchanges.base import FundingTick
from frab.engine.state import CoinState, MarketState

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _tick(coin: str = "BTC", offset_h: float = 0.0, rate: float = 0.0001) -> FundingTick:
    ts = _BASE + timedelta(hours=offset_h)
    return FundingTick(coin=coin, ts=ts, rate=rate, premium=None, annualized_pct=rate * 8760 * 100)


# ---------------------------------------------------------------------------
# CoinState tests
# ---------------------------------------------------------------------------


def test_initial_state_empty() -> None:
    state = CoinState("BTC", 12)
    assert state.coin == "BTC"
    assert state.window == 12
    assert state.samples == 0
    assert state.last_tick is None
    assert state.is_ready is False
    assert state.smoothed_signal() is None
    assert state.current_annual_rate() is None


def test_invalid_window() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        CoinState("BTC", 0)
    with pytest.raises(ValueError, match="must be positive"):
        CoinState("BTC", -1)


def test_add_one_tick() -> None:
    state = CoinState("BTC", 12)
    t = _tick(coin="BTC", offset_h=0, rate=0.0001)
    state.add_funding(t)
    assert state.samples == 1
    assert state.is_ready is False
    assert state.smoothed_signal() is None
    assert state.current_annual_rate() == pytest.approx(0.0001 * 8760)
    assert state.last_tick.ts == _BASE


def test_fill_window_makes_ready() -> None:
    state = CoinState("BTC", 3)
    # cutoff after last tick = _BASE+2h - 3h = _BASE-1h; all 3 ticks have ts > _BASE-1h
    rates = [0.0001, 0.0002, 0.0003]
    for i, rate in enumerate(rates):
        state.add_funding(_tick(coin="BTC", offset_h=float(i), rate=rate))
    assert state.is_ready is True
    assert state.samples == 3
    assert state.smoothed_signal() == pytest.approx(0.0002 * 8760)
    assert state.current_annual_rate() == pytest.approx(0.0003 * 8760)


def test_buffer_evicts_oldest() -> None:
    state = CoinState("BTC", 2)
    # ticks at offset_h=0,1,2,3; after last add cutoff=_BASE+3h-2h=_BASE+1h
    # keeps only offset_h=2 (rate=0.0003) and offset_h=3 (rate=0.0004)
    rates = [0.0001, 0.0002, 0.0003, 0.0004]
    for i, rate in enumerate(rates):
        state.add_funding(_tick(coin="BTC", offset_h=float(i), rate=rate))
    assert state.samples == 2
    assert state.smoothed_signal() == pytest.approx(((0.0003 + 0.0004) / 2) * 8760)
    assert state.current_annual_rate() == pytest.approx(0.0004 * 8760)


def test_coin_mismatch_raises() -> None:
    state = CoinState("BTC", 12)
    with pytest.raises(ValueError, match="coin mismatch"):
        state.add_funding(_tick(coin="ETH", offset_h=0, rate=0.0001))


def test_out_of_order_tick_raises() -> None:
    state = CoinState("BTC", 12)
    state.add_funding(_tick(coin="BTC", offset_h=2.0, rate=0.0001))
    with pytest.raises(ValueError, match="out-of-order"):
        state.add_funding(_tick(coin="BTC", offset_h=1.5, rate=0.0001))


def test_equal_timestamp_is_idempotent() -> None:
    state = CoinState("BTC", 12)
    state.add_funding(_tick(coin="BTC", offset_h=2.0, rate=0.0001))
    # Re-applying the same ts is a no-op, not an error. This is how DB-loaded
    # warmup ticks coexist with the engine's first hour-tick fetching the same
    # boundary from HL.
    state.add_funding(_tick(coin="BTC", offset_h=2.0, rate=0.0001))
    assert state.samples == 1


def test_repr() -> None:
    state = CoinState("BTC", 12)
    assert repr(state) == "CoinState(coin='BTC', samples=0, window=12)"


def test_window_drops_old_ticks_after_gap() -> None:
    state = CoinState("BTC", 12)
    # Add 11 consecutive hourly ticks (offset_h=0..10) — not yet ready
    for i in range(11):
        state.add_funding(_tick(coin="BTC", offset_h=float(i), rate=0.0001))
    assert state.samples == 11
    assert state.is_ready is False
    assert state.smoothed_signal() is None

    # Simulate a 29h data gap: new tick at offset_h=40
    # cutoff = _BASE+40h - 12h = _BASE+28h; all prior ticks (ts <= _BASE+10h) are pruned
    state.add_funding(_tick(coin="BTC", offset_h=40.0, rate=0.0005))
    assert state.samples == 1
    assert state.is_ready is False
    assert state.smoothed_signal() is None
    assert state.last_tick.ts == _BASE + timedelta(hours=40)
    assert state.current_annual_rate() == pytest.approx(0.0005 * 8760)


# ---------------------------------------------------------------------------
# MarketState tests
# ---------------------------------------------------------------------------


def test_market_init_with_coins() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    assert len(ms) == 2
    assert ms.coins() == ["BTC", "ETH"]
    assert "BTC" in ms
    assert "SOL" not in ms


def test_market_get_unknown_raises() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    with pytest.raises(KeyError, match="unknown coin"):
        ms.get("SOL")


def test_market_get_returns_state() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    state = ms.get("BTC")
    assert isinstance(state, CoinState)
    assert state.coin == "BTC"
    assert state.window == 12


def test_market_add_funding_routes_to_coin() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    ms.add_funding(_tick(coin="BTC", offset_h=0, rate=0.0001))
    assert ms.get("BTC").samples == 1
    assert ms.get("ETH").samples == 0


def test_market_add_funding_unknown_coin_raises() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    with pytest.raises(KeyError, match="unknown coin"):
        ms.add_funding(_tick(coin="SOL", offset_h=0, rate=0.0001))


def test_market_add_funding_batch() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    # Small offsets within 12h window so none are pruned
    ticks = [
        _tick(coin="BTC", offset_h=1.0, rate=0.0001),
        _tick(coin="ETH", offset_h=1.001, rate=0.0001),
        _tick(coin="BTC", offset_h=2.0, rate=0.0001),
        _tick(coin="ETH", offset_h=2.001, rate=0.0001),
        _tick(coin="BTC", offset_h=3.0, rate=0.0001),
    ]
    ms.add_funding_batch(ticks)
    assert ms.get("BTC").samples == 3
    assert ms.get("ETH").samples == 2


def test_market_signals_per_coin() -> None:
    ms = MarketState(["BTC", "ETH"], 2)
    # BTC: offset_h=1,2; cutoff=_BASE+2h-2h=_BASE; both ts > _BASE → both kept
    ms.add_funding(_tick(coin="BTC", offset_h=1.0, rate=0.0001))
    ms.add_funding(_tick(coin="BTC", offset_h=2.0, rate=0.0002))
    ms.add_funding(_tick(coin="ETH", offset_h=1.001, rate=0.0003))
    signals = ms.signals()
    assert signals["BTC"] == pytest.approx(0.00015 * 8760)
    assert signals["ETH"] is None


def test_market_empty() -> None:
    ms = MarketState([], 12)
    assert len(ms) == 0
    assert ms.coins() == []
    assert ms.signals() == {}


def test_market_funding_interval_propagates() -> None:
    """funding_interval_hours kwarg propagates from MarketState down to each CoinState."""
    ms = MarketState(["BTC", "ETH"], 12, funding_interval_hours=0.5)
    assert ms.get("BTC")._funding_interval_h == 0.5
    assert ms.get("ETH")._funding_interval_h == 0.5


# ---------------------------------------------------------------------------
# Forward-fill tests
# ---------------------------------------------------------------------------


def test_forward_fill_one_missing_hour() -> None:
    """Gap of 2 hours → 1 synthetic at offset_h=1; last_tick stays real at offset_h=2."""
    state = CoinState("BTC", 12)
    t0 = _tick(coin="BTC", offset_h=0, rate=0.0001)
    t2 = _tick(coin="BTC", offset_h=2, rate=0.0002)
    state.add_funding(t0)
    state.add_funding(t2)
    # 3 ticks: real@0, synthetic@1, real@2
    assert state.samples == 3
    assert state.last_tick.ts == _BASE + timedelta(hours=2)
    # synthetic at offset 1 carries forward rate from t0
    synthetic = state._ticks[1]
    assert synthetic.ts == _BASE + timedelta(hours=1)
    assert synthetic.rate == pytest.approx(0.0001)
    # window=12, samples=3 → not ready yet but smoothed_signal() still returns None
    assert state.is_ready is False


def test_forward_fill_three_missing_hours() -> None:
    """Gap of 4 hours → 3 synthetics at offsets 1,2,3 carrying first tick's rate."""
    state = CoinState("BTC", 12)
    t0 = _tick(coin="BTC", offset_h=0, rate=0.0001)
    t4 = _tick(coin="BTC", offset_h=4, rate=0.0002)
    state.add_funding(t0)
    state.add_funding(t4)
    # 5 ticks: real@0, synthetic@1, synthetic@2, synthetic@3, real@4
    assert state.samples == 5
    # Synthetics carry rate from t0
    for i in range(1, 4):
        assert state._ticks[i].rate == pytest.approx(0.0001)
        assert state._ticks[i].ts == _BASE + timedelta(hours=i)
    # Real tick at offset 4
    assert state._ticks[4].rate == pytest.approx(0.0002)
    assert state._ticks[4].ts == _BASE + timedelta(hours=4)


def test_forward_fill_capped_at_window() -> None:
    """A 100-hour gap is capped at window=3 synthetics; after prune only a few ticks survive."""
    state = CoinState("BTC", window_hours=3)
    state.add_funding(_tick(coin="BTC", offset_h=0, rate=0.0001))
    state.add_funding(_tick(coin="BTC", offset_h=100, rate=0.0002))
    # missing=99, capped to 3 → synthetics at offsets 1,2,3
    # prune: cutoff = 100 - 3 = 97, keep ts > 97h
    # synthetic@1,2,3 all <= 97 → pruned
    # real@100 survives
    assert state.samples <= 3  # at most a handful, not 99
    assert state.last_tick.ts == _BASE + timedelta(hours=100)


def test_forward_fill_no_gap() -> None:
    """Sequential hourly ticks produce no synthetics — count matches exactly what was added."""
    state = CoinState("BTC", 12)
    for i in range(6):
        state.add_funding(_tick(coin="BTC", offset_h=float(i), rate=0.0001))
    assert state.samples == 6


def test_forward_fill_with_fractional_funding_interval() -> None:
    """With interval=0.5h, a 1.5h gap creates 2 synthetics at offsets 0.5 and 1.0."""
    state = CoinState("BTC", window_hours=4, funding_interval_hours=0.5)
    state.add_funding(_tick(coin="BTC", offset_h=0, rate=0.0003))
    state.add_funding(_tick(coin="BTC", offset_h=1.5, rate=0.0004))
    # ticks: real@0, synthetic@0.5, synthetic@1.0, real@1.5
    assert state.samples == 4
    assert state._ticks[1].ts == _BASE + timedelta(hours=0.5)
    assert state._ticks[2].ts == _BASE + timedelta(hours=1.0)
    assert state._ticks[1].rate == pytest.approx(0.0003)
    assert state._ticks[2].rate == pytest.approx(0.0003)


def test_forward_fill_preserves_premium_and_annualized() -> None:
    """Synthetic ticks carry forward premium and annualized_pct from the prior real tick."""
    state = CoinState("BTC", 12)
    t0 = FundingTick(coin="BTC", ts=_BASE, rate=0.0001, premium=0.01, annualized_pct=8.76)
    t2 = FundingTick(coin="BTC", ts=_BASE + timedelta(hours=2), rate=0.0002, premium=0.02, annualized_pct=17.52)
    state.add_funding(t0)
    state.add_funding(t2)
    synthetic = state._ticks[1]  # synthetic at offset 1
    assert synthetic.premium == pytest.approx(0.01)
    assert synthetic.annualized_pct == pytest.approx(8.76)
    assert synthetic.rate == pytest.approx(0.0001)


def test_invalid_funding_interval_hours() -> None:
    """funding_interval_hours=0 or negative raises ValueError."""
    with pytest.raises(ValueError, match="funding_interval_hours must be positive"):
        CoinState("BTC", 12, funding_interval_hours=0)
    with pytest.raises(ValueError, match="funding_interval_hours must be positive"):
        CoinState("BTC", 12, funding_interval_hours=-1.0)


def test_forward_fill_then_signal_passes_readiness() -> None:
    """Reproduces the production scenario: 11 consecutive ticks then a 2h gap at offset 12.

    With forward-fill the synthetic at offset 11 fills the gap so samples==12
    and smoothed_signal() returns a valid float.
    """
    state = CoinState("BTC", window_hours=12, funding_interval_hours=1.0)
    for i in range(11):
        state.add_funding(_tick(coin="BTC", offset_h=float(i), rate=0.0001))
    assert state.samples == 11
    assert state.is_ready is False

    # Tick at offset 12 (skipping offset 11)
    state.add_funding(_tick(coin="BTC", offset_h=12.0, rate=0.0001))
    # After forward-fill: synthetic@11 inserted; prune cutoff = 12-12 = 0
    # offset_h=0 is exactly at cutoff (ts > cutoff requires strictly greater) → pruned
    # offsets 1..10 (10 real) + offset 11 (synthetic) + offset 12 (real) = 12 ticks
    assert state.samples == 12
    assert state.is_ready is True
    sig = state.smoothed_signal()
    assert sig is not None
    assert isinstance(sig, float)
