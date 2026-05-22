"""End-to-end margin watchdog scenarios.

Real stack: StrategyA + MarginManager + AtomicExecutor + Engine + Watchdog.
Only the exchange surface (executor + market data) is stubbed in-memory.

Scenarios:
1. top_up_under_adverse_move — open position, then adverse mark move drops the
   margin ratio below top_up_trigger but above 1.0. Watchdog transfers
   spot→perp to restore healthy ratio.
2. forced_close_when_spot_cash_insufficient — same setup but spot cash is too
   low to satisfy the top-up. Watchdog force-closes the position.
3. no_action_under_stable_marks — open positions, then drive many minute ticks
   at unchanged marks. Watchdog stays idle (zero margin events).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from frab.engine.loop import Engine
from frab.engine.margin_manager import MarginManager, PerCoinSpec
from frab.events.bus import Event, EventBus
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

T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Crafted market data — caller sets marks/funding per tick
# ---------------------------------------------------------------------------

class CraftedMarket(MarketDataSource):
    name = "crafted"

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}
        self._funding: dict[str, float] = {}
        self._ts = T0

    def set_mark(self, coin: str, mark: float) -> None:
        self._marks[coin] = mark

    def set_funding(self, coin: str, rate: float) -> None:
        self._funding[coin] = rate

    def set_ts(self, ts: datetime) -> None:
        self._ts = ts

    async def fetch_quote(self, coin: str) -> Quote:
        m = self._marks[coin]
        return Quote(coin=coin, ts=self._ts, bid=m, ask=m, mark=m, spot=None)

    async def fetch_funding(self, coin: str) -> FundingTick:
        rate = self._funding[coin]
        return FundingTick(
            coin=coin, ts=self._ts, rate=rate, premium=None,
            annualized_pct=rate * 8760 * 100,
        )

    async def fetch_funding_history(self, coin: str, since_ms: int):
        raise NotImplementedError

    async def fetch_meta(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory executor with real transfer methods
# ---------------------------------------------------------------------------

@dataclass
class _Pos:
    spot_units: float = 0.0
    perp_units: float = 0.0


class StubExecutor:
    """Paper executor with usdClassTransfer simulated as a no-op (success)."""
    name = "stub"

    def __init__(self, market: CraftedMarket) -> None:
        self._market = market
        self._positions: dict[str, _Pos] = {}
        self.transfer_calls: list[tuple[str, float]] = []

    async def submit(self, req: OrderRequest) -> FillReport:
        quote = await self._market.fetch_quote(req.coin)
        price = quote.mark
        p = self._positions.setdefault(req.coin, _Pos())
        delta = req.qty if req.side == Side.BUY else -req.qty
        if req.leg == Leg.SPOT:
            p.spot_units += delta
        else:
            p.perp_units += delta
        return FillReport(
            coin=req.coin, leg=req.leg, side=req.side,
            ts=quote.ts, qty=req.qty, price=price, fee=0.0, slippage_bps=0.0,
            client_ref=req.client_ref,
        )

    async def get_position(self, coin: str) -> PositionState | None:
        p = self._positions.get(coin)
        if p is None or (abs(p.spot_units) < 1e-12 and abs(p.perp_units) < 1e-12):
            return None
        return PositionState(coin=coin, spot_units=p.spot_units, perp_units=p.perp_units,
                             avg_entry_spot=None, avg_entry_perp=None)

    async def reconcile(self) -> None:
        return None

    async def round_qty(self, coin: str, qty: float) -> float:
        return qty

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        return qty

    async def transfer_spot_to_perp(self, usdc_amount: float) -> dict:
        self.transfer_calls.append(("spot_to_perp", usdc_amount))
        return {"status": "ok"}

    async def transfer_perp_to_spot(self, usdc_amount: float) -> dict:
        self.transfer_calls.append(("perp_to_spot", usdc_amount))
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_margin_manager(top_up: float = 1.5, healthy: float = 2.0) -> MarginManager:
    """BTC at 5x leverage, 5% maint, $100 spot, 1.5x buffer.

    Per-coin footprint: spot=100, required_margin = 100/5*1.5 = 30.
    Maintenance at entry mark=100 with qty=1: 1 * 100 * 0.05 = 5.
    Initial margin ratio: 30/5 = 6.0 (very healthy).
    """
    return MarginManager(
        per_coin_params={"BTC": PerCoinSpec(
            position_size_usd=100.0, leverage=5, maint_ratio=0.05,
        )},
        margin_buffer_x=1.5,
        top_up_trigger=top_up,
        healthy_ratio=healthy,
        budget_cap_usd=10_000.0,
    )


async def _open_btc(engine: Engine, market: CraftedMarket) -> None:
    """Drive a single hour-tick that opens BTC."""
    market.set_ts(T0)
    market.set_mark("BTC", 100.0)
    market.set_funding("BTC", 0.0001)  # 87.6% annual → way above 30% threshold
    await engine.tick_once(T0)


def _drain_funding_history(strategy: StrategyA, coin: str, n_ticks: int = 12) -> None:
    """Seed StrategyA's MarketState with positive funding so the smoothed
    signal stays above entry_threshold on the next hour tick. signal_window=2."""
    from frab.exchanges.base import FundingTick
    for i in range(n_ticks):
        tick = FundingTick(
            coin=coin, ts=T0 - HOUR * (n_ticks - i),
            rate=0.0001, premium=None, annualized_pct=0.876,
        )
        strategy._market_state.add_funding(tick)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_up_under_adverse_move():
    market = CraftedMarket()
    bus = EventBus()
    events: list[Event] = []
    original_publish = bus.publish

    async def _recording_publish(ev: Event) -> None:
        events.append(ev)
        await original_publish(ev)

    bus.publish = _recording_publish  # type: ignore[method-assign]
    stub = StubExecutor(market)
    atomic = AtomicExecutor(stub, bus, max_attempts=1, sleep_between_attempts=())

    mgr = _build_margin_manager(top_up=1.5, healthy=2.0)
    strat = StrategyA(
        params=StrategyAParams(
            coins=("BTC",), entry_threshold=0.30, exit_threshold=-0.15,
            min_hold_hours=120, signal_window_hours=2, concurrency_cap=1,
            position_size_usdc=100.0,
        ),
        executor=atomic,
        margin_manager=mgr,
    )
    _drain_funding_history(strat, "BTC")
    engine = Engine(market_data=market, strategy=strat, coins=("BTC",), event_bus=bus)

    # 1. Open BTC. C2 pre-flight transfers $30 (required_margin = 100/5*1.5) into perp.
    await _open_btc(engine, market)
    assert "BTC" in strat._positions
    assert strat.perp_cash == pytest.approx(30.0)
    transfers_after_open = len(stub.transfer_calls)
    assert transfers_after_open == 1
    cash_after_open = strat.cash

    # 2. Adverse mark move puts ratio into the TOP_UP band (>1.0, <trigger=1.5).
    # mark=122, qty=1 → maint=6.1, unrealized=1*(100-122)=-22.
    # effective_equity = perp_cash + unrealized = 30 - 22 = 8.
    # ratio = effective_equity / maint = 8 / 6.1 = 1.31 → in band.
    # top_up_amount = healthy_ratio * maint - effective_equity = 12.2 - 8 = 4.2.
    market.set_ts(T0 + MIN)
    market.set_mark("BTC", 122.0)
    market.set_funding("BTC", 0.0001)
    await engine.tick_once(T0 + MIN)

    # 3. Watchdog topped up.
    top_up_events = [e for e in events if e.kind == "margin.top_up"]
    assert len(top_up_events) == 1
    assert top_up_events[0].level == "WARNING"

    # 4. One additional spot→perp transfer happened (besides the open-time one).
    post_open_transfers = stub.transfer_calls[transfers_after_open:]
    assert len(post_open_transfers) == 1
    direction, amount = post_open_transfers[0]
    assert direction == "spot_to_perp"
    assert amount == pytest.approx(4.2)
    assert strat.cash == pytest.approx(cash_after_open - amount)
    assert strat.perp_cash == pytest.approx(30.0 + amount)
    assert "BTC" in strat._positions  # position still open


@pytest.mark.asyncio
async def test_forced_close_when_spot_cash_insufficient():
    market = CraftedMarket()
    bus = EventBus()
    events: list[Event] = []
    original_publish = bus.publish

    async def _recording_publish(ev: Event) -> None:
        events.append(ev)
        await original_publish(ev)

    bus.publish = _recording_publish  # type: ignore[method-assign]
    stub = StubExecutor(market)
    atomic = AtomicExecutor(stub, bus, max_attempts=1, sleep_between_attempts=())

    mgr = _build_margin_manager(top_up=1.5, healthy=2.0)
    strat = StrategyA(
        params=StrategyAParams(
            coins=("BTC",), entry_threshold=0.30, exit_threshold=-0.15,
            min_hold_hours=120, signal_window_hours=2, concurrency_cap=1,
            position_size_usdc=100.0,
        ),
        executor=atomic,
        margin_manager=mgr,
    )
    _drain_funding_history(strat, "BTC")
    engine = Engine(market_data=market, strategy=strat, coins=("BTC",), event_bus=bus)

    # Open BTC. C2 pre-flight transfers $30 from spot → perp.
    await _open_btc(engine, market)
    assert "BTC" in strat._positions
    transfers_after_open = len(stub.transfer_calls)

    # Drain spot cash so top-up cannot proceed (< $1 leftover, less than the
    # ~$4.20 top-up that would have been needed).
    strat._cash = 0.5

    # Adverse move puts ratio into TOP_UP band (mark=122).
    market.set_ts(T0 + MIN)
    market.set_mark("BTC", 122.0)
    market.set_funding("BTC", 0.0001)
    await engine.tick_once(T0 + MIN)

    # Watchdog took the FORCED_CLOSE branch.
    forced_events = [e for e in events if e.kind == "margin.forced_close"]
    assert len(forced_events) == 1
    assert forced_events[0].level == "ERROR"
    assert forced_events[0].payload_json["coin"] == "BTC"

    # No additional spot→perp transfer; one perp→spot transfer released lockup.
    post_open_transfers = stub.transfer_calls[transfers_after_open:]
    assert [t[0] for t in post_open_transfers] == ["perp_to_spot"]
    assert post_open_transfers[0][1] == pytest.approx(30.0)

    # Position closed in-memory.
    assert "BTC" not in strat._positions


@pytest.mark.asyncio
async def test_no_action_under_stable_marks():
    market = CraftedMarket()
    bus = EventBus()
    events: list[Event] = []
    original_publish = bus.publish

    async def _recording_publish(ev: Event) -> None:
        events.append(ev)
        await original_publish(ev)

    bus.publish = _recording_publish  # type: ignore[method-assign]
    stub = StubExecutor(market)
    atomic = AtomicExecutor(stub, bus, max_attempts=1, sleep_between_attempts=())

    mgr = _build_margin_manager(top_up=1.5, healthy=2.0)
    strat = StrategyA(
        params=StrategyAParams(
            coins=("BTC",), entry_threshold=0.30, exit_threshold=-0.15,
            min_hold_hours=120, signal_window_hours=2, concurrency_cap=1,
            position_size_usdc=100.0,
        ),
        executor=atomic,
        margin_manager=mgr,
    )
    _drain_funding_history(strat, "BTC")
    engine = Engine(market_data=market, strategy=strat, coins=("BTC",), event_bus=bus)

    # Open BTC. C2 pre-flight does one spot→perp transfer ($30).
    await _open_btc(engine, market)
    assert "BTC" in strat._positions
    transfers_after_open = len(stub.transfer_calls)
    initial_cash = strat.cash
    initial_perp_cash = strat.perp_cash

    # Drive 30 minute ticks at unchanged mark.
    for i in range(1, 31):
        ts = T0 + MIN * i
        market.set_ts(ts)
        market.set_mark("BTC", 100.0)
        market.set_funding("BTC", 0.0001)
        await engine.tick_once(ts)

    # No margin events fired.
    margin_events = [e for e in events if e.kind.startswith("margin.")]
    assert margin_events == []

    # No additional transfers beyond the open-time one.
    assert len(stub.transfer_calls) == transfers_after_open

    # Cash and perp_cash unchanged (no hour boundary crossed → no funding accrual either).
    assert strat.cash == pytest.approx(initial_cash)
    assert strat.perp_cash == pytest.approx(initial_perp_cash)

    # Position still open.
    assert "BTC" in strat._positions
