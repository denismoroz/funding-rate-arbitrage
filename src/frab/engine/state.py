"""Rolling per-coin and multi-coin funding state for Strategy A."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from frab.exchanges.base import FundingTick
from frab.engine.signals import annualize_rate


class CoinState:
    """Per-coin time-based rolling buffer of funding ticks plus last-tick metadata."""

    def __init__(self, coin: str, window_hours: int, funding_interval_hours: float = 1.0) -> None:
        self.coin = coin
        if window_hours <= 0:
            raise ValueError("window_hours must be positive")
        if funding_interval_hours <= 0:
            raise ValueError("funding_interval_hours must be positive")
        self._window = window_hours
        self._funding_interval_h = funding_interval_hours
        self._ticks: list[FundingTick] = []
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
            # Forward-fill any missing intermediate ticks
            elapsed_h = (tick.ts - self._last_tick.ts).total_seconds() / 3600.0
            missing = int(round(elapsed_h / self._funding_interval_h)) - 1
            if missing > 0:
                missing = min(missing, self._window)
                for i in range(1, missing + 1):
                    synthetic_ts = self._last_tick.ts + timedelta(hours=self._funding_interval_h * i)
                    if synthetic_ts >= tick.ts:
                        break
                    synthetic = FundingTick(
                        coin=self.coin,
                        ts=synthetic_ts,
                        rate=self._last_tick.rate,
                        premium=self._last_tick.premium,
                        annualized_pct=self._last_tick.annualized_pct,
                    )
                    self._ticks.append(synthetic)
        self._ticks.append(tick)
        self._last_tick = tick
        cutoff = tick.ts - timedelta(hours=self._window)
        self._ticks = [t for t in self._ticks if t.ts > cutoff]

    @property
    def window(self) -> int:
        return self._window

    @property
    def samples(self) -> int:
        return len(self._ticks)

    @property
    def last_tick(self) -> FundingTick | None:
        return self._last_tick

    @property
    def is_ready(self) -> bool:
        return len(self._ticks) >= self._window

    def smoothed_signal(self) -> float | None:
        if not self._ticks:
            return None
        if len(self._ticks) < self._window:
            return None
        mean_rate = sum(t.rate for t in self._ticks) / len(self._ticks)
        return annualize_rate(mean_rate)

    def current_annual_rate(self) -> float | None:
        if self._last_tick is None:
            return None
        return annualize_rate(self._last_tick.rate)

    def __repr__(self) -> str:
        return f"CoinState(coin={self.coin!r}, samples={self.samples}, window={self._window})"


class MarketState:
    """Multi-coin aggregator. Thin wrapper over a dict of CoinState."""

    def __init__(self, coins: Iterable[str], window_hours: int, funding_interval_hours: float = 1.0) -> None:
        self._coins: dict[str, CoinState] = {c: CoinState(c, window_hours, funding_interval_hours) for c in coins}

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
