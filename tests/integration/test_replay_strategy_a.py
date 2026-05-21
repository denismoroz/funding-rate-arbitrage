"""Integration test: replay real CSV data through full Strategy A stack."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from frab.engine.loop import Engine
from frab.events.bus import EventBus
from frab.exchanges.atomic import AtomicExecutor
from frab.exchanges.base import (
    FillReport,
    FundingTick,
    Leg,
    MarketDataSource,
    OrderRequest,
    PositionState,
    Quote,
    Side,
)
from frab.strategies.strategy_a import StrategyA, StrategyAParams


@dataclass
class _PosEntry:
    spot_units: float = 0.0
    perp_units: float = 0.0
    avg_spot: float | None = None
    avg_perp: float | None = None


class _ReplayExecutor:
    """Minimal paper-mode executor used only by the integration replay test."""

    name = "replay_paper"

    def __init__(self, market_data: MarketDataSource, spot_taker_bps: float, perp_taker_bps: float, extra_slip_bps: float = 0.0) -> None:
        self._md = market_data
        self._spot_bps = spot_taker_bps
        self._perp_bps = perp_taker_bps
        self._slip = extra_slip_bps
        self._positions: dict[str, _PosEntry] = {}

    async def submit(self, req: OrderRequest) -> FillReport:
        quote = await self._md.fetch_quote(req.coin)
        slip = self._slip / 1e4
        if req.leg == Leg.SPOT:
            price = (quote.ask * (1 + slip)) if req.side == Side.BUY else (quote.bid * (1 - slip))
            fee = req.qty * price * self._spot_bps / 1e4
        else:
            price = (quote.ask * (1 + slip)) if req.side == Side.BUY else (quote.bid * (1 - slip))
            fee = req.qty * price * self._perp_bps / 1e4
        entry = self._positions.setdefault(req.coin, _PosEntry())
        delta = req.qty if req.side == Side.BUY else -req.qty
        if req.leg == Leg.SPOT:
            entry.spot_units += delta
        else:
            entry.perp_units += delta
        return FillReport(
            coin=req.coin, leg=req.leg, side=req.side,
            ts=datetime.now(UTC), qty=req.qty, price=price,
            fee=fee, slippage_bps=self._slip, is_paper=True,
            client_ref=req.client_ref,
        )

    async def get_position(self, coin: str) -> PositionState | None:
        e = self._positions.get(coin)
        if e is None or (abs(e.spot_units) < 1e-12 and abs(e.perp_units) < 1e-12):
            return None
        return PositionState(coin=coin, spot_units=e.spot_units, perp_units=e.perp_units,
                             avg_entry_spot=e.avg_spot, avg_entry_perp=e.avg_perp)

    async def reconcile(self) -> None:
        return None

    async def round_qty(self, coin: str, qty: float) -> float:
        return qty

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        return qty

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
    bus = EventBus()
    replay_executor = _ReplayExecutor(
        market_data=market,
        spot_taker_bps=7.0,
        perp_taker_bps=3.5,
        extra_slip_bps=0.0,  # no extra slippage — for cleaner replay convergence with backtest
    )
    executor = AtomicExecutor(replay_executor, bus, max_attempts=1, sleep_between_attempts=())
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
