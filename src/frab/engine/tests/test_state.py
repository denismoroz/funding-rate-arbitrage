from __future__ import annotations

import pytest

from frab.exchanges.base import FundingTick
from frab.engine.state import CoinState, MarketState


def _tick(coin: str = "BTC", ts_ms: int = 1_000_000, rate: float = 0.0001) -> FundingTick:
    return FundingTick(coin=coin, ts_ms=ts_ms, rate=rate, premium=None, annualized_pct=rate * 8760 * 100)


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
    state.add_funding(_tick(coin="BTC", ts_ms=1_000_000, rate=0.0001))
    assert state.samples == 1
    assert state.is_ready is False
    assert state.smoothed_signal() is None
    assert state.current_annual_rate() == pytest.approx(0.0001 * 8760)
    assert state.last_tick.ts_ms == 1_000_000


def test_fill_window_makes_ready() -> None:
    state = CoinState("BTC", 3)
    rates = [0.0001, 0.0002, 0.0003]
    for i, rate in enumerate(rates):
        state.add_funding(_tick(coin="BTC", ts_ms=1_000_000 + i * 1000, rate=rate))
    assert state.is_ready is True
    assert state.samples == 3
    assert state.smoothed_signal() == pytest.approx(0.0002 * 8760)
    assert state.current_annual_rate() == pytest.approx(0.0003 * 8760)


def test_buffer_evicts_oldest() -> None:
    state = CoinState("BTC", 2)
    rates = [0.0001, 0.0002, 0.0003, 0.0004]
    for i, rate in enumerate(rates):
        state.add_funding(_tick(coin="BTC", ts_ms=1_000_000 + i * 1000, rate=rate))
    assert state.samples == 2
    assert state.smoothed_signal() == pytest.approx(((0.0003 + 0.0004) / 2) * 8760)
    assert state.current_annual_rate() == pytest.approx(0.0004 * 8760)


def test_coin_mismatch_raises() -> None:
    state = CoinState("BTC", 12)
    with pytest.raises(ValueError, match="coin mismatch"):
        state.add_funding(_tick(coin="ETH", ts_ms=1_000_000, rate=0.0001))


def test_non_monotonic_tick_raises() -> None:
    state = CoinState("BTC", 12)
    state.add_funding(_tick(coin="BTC", ts_ms=2000, rate=0.0001))
    with pytest.raises(ValueError, match="non-monotonic"):
        state.add_funding(_tick(coin="BTC", ts_ms=1500, rate=0.0001))


def test_equal_timestamp_raises() -> None:
    state = CoinState("BTC", 12)
    state.add_funding(_tick(coin="BTC", ts_ms=2000, rate=0.0001))
    with pytest.raises(ValueError, match="non-monotonic"):
        state.add_funding(_tick(coin="BTC", ts_ms=2000, rate=0.0001))


def test_repr() -> None:
    state = CoinState("BTC", 12)
    assert repr(state) == "CoinState(coin='BTC', samples=0, window=12)"


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
    ms.add_funding(_tick(coin="BTC", ts_ms=1_000_000, rate=0.0001))
    assert ms.get("BTC").samples == 1
    assert ms.get("ETH").samples == 0


def test_market_add_funding_unknown_coin_raises() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    with pytest.raises(KeyError, match="unknown coin"):
        ms.add_funding(_tick(coin="SOL", ts_ms=1_000_000, rate=0.0001))


def test_market_add_funding_batch() -> None:
    ms = MarketState(["BTC", "ETH"], 12)
    ticks = [
        _tick(coin="BTC", ts_ms=1000, rate=0.0001),
        _tick(coin="ETH", ts_ms=1001, rate=0.0001),
        _tick(coin="BTC", ts_ms=2000, rate=0.0001),
        _tick(coin="ETH", ts_ms=2001, rate=0.0001),
        _tick(coin="BTC", ts_ms=3000, rate=0.0001),
    ]
    ms.add_funding_batch(ticks)
    assert ms.get("BTC").samples == 3
    assert ms.get("ETH").samples == 2


def test_market_signals_per_coin() -> None:
    ms = MarketState(["BTC", "ETH"], 2)
    ms.add_funding(_tick(coin="BTC", ts_ms=1000, rate=0.0001))
    ms.add_funding(_tick(coin="BTC", ts_ms=2000, rate=0.0002))
    ms.add_funding(_tick(coin="ETH", ts_ms=1001, rate=0.0003))
    signals = ms.signals()
    assert signals["BTC"] == pytest.approx(0.00015 * 8760)
    assert signals["ETH"] is None


def test_market_empty() -> None:
    ms = MarketState([], 12)
    assert len(ms) == 0
    assert ms.coins() == []
    assert ms.signals() == {}
