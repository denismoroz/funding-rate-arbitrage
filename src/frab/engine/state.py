"""Rolling per-coin and multi-coin funding state for Strategy A."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from frab.exchanges.base import FundingTick
from frab.engine.signals import annualize_rate, rolling_mean


class CoinState:
    """Per-coin rolling buffer of funding rates plus last-tick metadata."""

    def __init__(self, coin: str, window_hours: int) -> None:
        self.coin = coin
        if window_hours <= 0:
            raise ValueError("window_hours must be positive")
        self._window = window_hours
        self._rates: deque[float] = deque(maxlen=window_hours)
        self._last_tick: FundingTick | None = None

    def add_funding(self, tick: FundingTick) -> None:
        if tick.coin != self.coin:
            raise ValueError(f"coin mismatch: state={self.coin!r}, tick={tick.coin!r}")
        if self._last_tick is not None:
            if tick.ts == self._last_tick.ts:
                return  # idempotent: same tick re-applied (e.g. DB load + engine fetch)
            if tick.ts < self._last_tick.ts:
                raise ValueError(
                    f"out-of-order funding tick: last_ts={self._last_tick.ts!r}, new_ts={tick.ts!r}"
                )
        self._rates.append(tick.rate)
        self._last_tick = tick

    @property
    def window(self) -> int:
        return self._window

    @property
    def samples(self) -> int:
        return len(self._rates)

    @property
    def last_tick(self) -> FundingTick | None:
        return self._last_tick

    @property
    def is_ready(self) -> bool:
        return len(self._rates) >= self._window

    def smoothed_signal(self) -> float | None:
        mean_rate = rolling_mean(list(self._rates), self._window)
        if mean_rate is None:
            return None
        return annualize_rate(mean_rate)

    def current_annual_rate(self) -> float | None:
        if self._last_tick is None:
            return None
        return annualize_rate(self._last_tick.rate)

    def __repr__(self) -> str:
        return f"CoinState(coin={self.coin!r}, samples={self.samples}, window={self._window})"


class MarketState:
    """Multi-coin aggregator. Thin wrapper over a dict of CoinState."""

    def __init__(self, coins: Iterable[str], window_hours: int) -> None:
        self._coins: dict[str, CoinState] = {c: CoinState(c, window_hours) for c in coins}

    def coins(self) -> list[str]:
        return list(self._coins.keys())

    def get(self, coin: str) -> CoinState:
        if coin not in self._coins:
            raise KeyError(f"unknown coin: {coin!r}")
        return self._coins[coin]

    def add_funding(self, tick: FundingTick) -> None:
        self.get(tick.coin).add_funding(tick)

    def add_funding_batch(self, ticks: Iterable[FundingTick]) -> None:
        for tick in ticks:
            self.add_funding(tick)

    def signals(self) -> dict[str, float | None]:
        return {coin: state.smoothed_signal() for coin, state in self._coins.items()}

    def __contains__(self, coin: str) -> bool:
        return coin in self._coins

    def __len__(self) -> int:
        return len(self._coins)
