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
