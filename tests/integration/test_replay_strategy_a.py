"""Integration test: replay real CSV data through full Strategy A stack."""
from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from frab.engine.loop import Engine
from frab.exchanges.base import FundingTick, MarketDataSource, Quote
from frab.exchanges.paper import PaperExecutor
from frab.strategies.strategy_a import StrategyA, StrategyAParams

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "research" / "data"
COINS = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")

# 30-day window for the smoke test
WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2024, 2, 1, tzinfo=UTC)


def _parse_dt(dt_str: str) -> datetime:
    """Parse ISO8601-ish timestamp string to UTC-aware datetime."""
    s = dt_str.strip()
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)


def _load_coin_data(coin: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
    """Inner-join funding + 1h close on hour timestamp, return list of (dt, rate, close) within [start, end)."""
    funding: dict[datetime, float] = {}
    with (DATA_DIR / f"{coin}.csv").open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = _parse_dt(row["time"]).replace(minute=0, second=0, microsecond=0)
            if start <= dt < end:
                funding[dt] = float(row["fundingRate"])

    prices: dict[datetime, float] = {}
    with (DATA_DIR / f"{coin}_1h.csv").open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = _parse_dt(row["time"]).replace(minute=0, second=0, microsecond=0)
            if start <= dt < end:
                prices[dt] = float(row["close"])

    common = sorted(set(funding.keys()) & set(prices.keys()))
    return [(dt, funding[dt], prices[dt]) for dt in common]


class ReplayMarketData(MarketDataSource):
    """Pre-indexed market data replayed by datetime. The driver loop calls set_now() before each tick."""

    name = "replay"

    def __init__(self, data: dict[str, list[tuple[datetime, float, float]]]):
        # data: coin -> sorted [(dt, rate, close), ...]
        # Build per-coin index by datetime for O(1) lookup.
        self._funding_by_ts: dict[str, dict[datetime, FundingTick]] = {}
        self._quote_by_ts: dict[str, dict[datetime, Quote]] = {}
        for coin, rows in data.items():
            f_map: dict[datetime, FundingTick] = {}
            q_map: dict[datetime, Quote] = {}
            for dt, rate, close in rows:
                f_map[dt] = FundingTick(coin=coin, ts=dt, rate=rate, premium=None, annualized_pct=rate * 8760 * 100)
                q_map[dt] = Quote(coin=coin, ts=dt, bid=close, ask=close, mark=close, spot=None)
            self._funding_by_ts[coin] = f_map
            self._quote_by_ts[coin] = q_map
        self._now: datetime | None = None

    def set_now(self, dt: datetime) -> None:
        self._now = dt

    async def fetch_quote(self, coin: str) -> Quote:
        return self._quote_by_ts[coin][self._now]

    async def fetch_funding(self, coin: str) -> FundingTick:
        return self._funding_by_ts[coin][self._now]

    async def fetch_funding_history(self, coin: str, since_ms: int):
        # Not used by Engine; raise if called.
        raise NotImplementedError("replay does not implement history")

    async def fetch_meta(self):
        raise NotImplementedError("replay does not implement meta")


def _common_timeline(data_by_coin: dict[str, list[tuple[datetime, float, float]]]) -> list[datetime]:
    """Intersection of timestamps across all coins (so a tick is fired only when every coin has data)."""
    sets = [set(dt for dt, _, _ in rows) for rows in data_by_coin.values()]
    if not sets:
        return []
    common = set.intersection(*sets)
    return sorted(common)


@pytest.mark.asyncio
async def test_replay_strategy_a_one_month_smoke():
    # 1. Load real data for all 7 coins in the window.
    data_by_coin: dict[str, list[tuple[datetime, float, float]]] = {}
    for coin in COINS:
        rows = _load_coin_data(coin, WINDOW_START, WINDOW_END)
        assert len(rows) > 0, f"no data for {coin} in window"
        data_by_coin[coin] = rows

    timeline = _common_timeline(data_by_coin)
    assert len(timeline) >= 24 * 28, f"timeline too short: {len(timeline)} hours (expected ~720)"

    # 2. Build production stack.
    market = ReplayMarketData(data_by_coin)
    executor = PaperExecutor(
        market_data=market,
        spot_taker_bps=7.0,
        perp_taker_bps=3.5,
        extra_slip_bps=0.0,  # no extra slippage — for cleaner replay convergence with backtest
    )
    params = StrategyAParams(
        coins=COINS,
        entry_threshold=0.30,
        exit_threshold=-0.15,
        min_hold_hours=120,
        signal_window_hours=12,
        concurrency_cap=3,
        position_size_usdc=1000.0,
    )
    strategy = StrategyA(params=params, executor=executor)
    initial_cash = strategy.cash

    engine = Engine(market_data=market, strategy=strategy, coins=COINS)

    # 3. Drive engine through every hour in the window.
    opens_per_coin: dict[str, int] = {c: 0 for c in COINS}
    closes_per_coin: dict[str, int] = {c: 0 for c in COINS}
    total_fills = 0
    total_signals_open = 0
    last_equity = None

    for ts in timeline:
        market.set_now(ts)
        outcome = await engine.tick_once(ts)
        assert outcome.equity is not None
        last_equity = outcome.equity
        if outcome.tick_report is not None:
            total_fills += len(outcome.tick_report.fills)
            for coin in outcome.tick_report.opened:
                opens_per_coin[coin] += 1
            for coin in outcome.tick_report.closed:
                closes_per_coin[coin] += 1
            for sig in outcome.tick_report.signals:
                if sig.action == "OPEN":
                    total_signals_open += 1

    # 4. Assertions: production stack ran cleanly and produced plausible activity.
    total_opens = sum(opens_per_coin.values())
    total_closes = sum(closes_per_coin.values())

    print(f"\ntotal_opens={total_opens}")
    print(f"total_closes={total_closes}")
    print(f"total_fills={total_fills}")
    print(f"last_equity.total_equity={last_equity.total_equity}")
    print(f"initial_cash={initial_cash}")

    assert total_opens >= 1, f"expected >=1 open in {len(timeline)}h window, got 0"
    assert total_closes <= total_opens, f"more closes than opens: {total_closes} > {total_opens}"

    assert last_equity is not None
    assert 0.9 * initial_cash <= last_equity.total_equity <= 1.1 * initial_cash, (
        f"final equity {last_equity.total_equity} outside +-10% of initial {initial_cash}"
    )

    mtm = last_equity.spot_value + last_equity.perp_unrealized
    assert last_equity.total_equity == pytest.approx(last_equity.cash + mtm, abs=1e-6)

    # Number of fills = 2 x opens + 2 x closes (each open/close is two legs).
    assert total_fills == 2 * (total_opens + total_closes), (
        f"expected {2 * (total_opens + total_closes)} fills, got {total_fills}"
    )

    # Signal log records every OPEN decision: at least one OPEN signal must have fired.
    assert total_signals_open >= total_opens, (
        f"OPEN signals {total_signals_open} should be >= executed opens {total_opens}"
    )
