"""Tests for src/frab/engine/loop.py."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from frab.engine.loop import (
    Engine,
    NullRecorder,
    Recorder,
    TickOutcome,
)
from frab.events.bus import Event, EventBus
from frab.exchanges.base import FillReport, FundingTick, Leg, MarketDataSource, Quote, Side
from frab.strategies.base import EquitySnapshot, SignalEvent, Strategy, TickReport

# Reference datetime: 2023-11-14 22:13:20 UTC (a known epoch anchor)
_T0 = datetime(2023, 11, 14, 22, 0, 0, tzinfo=UTC)  # on the hour
_HOUR = timedelta(hours=1)
_MINUTE = timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote(coin="BTC", mark=100.0, ts: datetime = _T0):
    return Quote(coin=coin, ts=ts, bid=mark, ask=mark, mark=mark, spot=None)


def _funding(coin="BTC", ts: datetime = _T0, rate=0.0001):
    return FundingTick(
        coin=coin,
        ts=ts,
        rate=rate,
        premium=None,
        annualized_pct=rate * 8760 * 100,
    )


def _equity(ts: datetime = _T0, total=6000.0):
    return EquitySnapshot(
        ts=ts,
        total_equity=total,
        cash=total,
        spot_value=0.0,
        perp_unrealized=0.0,
        perp_realized_cum=0.0,
        funding_cum=0.0,
        fees_cum=0.0,
    )


def _tick_report(ts: datetime = _T0):
    return TickReport(ts=ts, signals=(), fills=(), opened=(), closed=())


def _spot_buy_fill(coin="BTC", price=100.0, qty=10.0):
    return FillReport(coin=coin, leg=Leg.SPOT, side=Side.BUY, ts=_T0, qty=qty,
                      price=price, fee=0.07, slippage_bps=2.0, is_paper=True)


def _perp_sell_fill(coin="BTC", price=100.0, qty=10.0):
    return FillReport(coin=coin, leg=Leg.PERP, side=Side.SELL, ts=_T0, qty=qty,
                      price=price, fee=0.035, slippage_bps=2.0, is_paper=True)


def _spot_sell_fill(coin="BTC", price=110.0, qty=10.0):
    return FillReport(coin=coin, leg=Leg.SPOT, side=Side.SELL, ts=_T0, qty=qty,
                      price=price, fee=0.07, slippage_bps=2.0, is_paper=True)


def _perp_buy_fill(coin="BTC", price=110.0, qty=10.0):
    return FillReport(coin=coin, leg=Leg.PERP, side=Side.BUY, ts=_T0, qty=qty,
                      price=price, fee=0.035, slippage_bps=2.0, is_paper=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def market_data(mocker):
    md = mocker.AsyncMock(spec=MarketDataSource)
    return md


@pytest.fixture
def strategy(mocker):
    s = mocker.AsyncMock(spec=Strategy)
    # compute_equity is sync; replace with MagicMock
    s.compute_equity = mocker.MagicMock(return_value=_equity())
    return s


@pytest.fixture
def recorder(mocker):
    return mocker.AsyncMock(spec=Recorder)


# ---------------------------------------------------------------------------
# Test 1: init validates empty coins
# ---------------------------------------------------------------------------


def test_init_validates_empty_coins(mocker):
    md = mocker.AsyncMock(spec=MarketDataSource)
    s = mocker.AsyncMock(spec=Strategy)
    with pytest.raises(ValueError, match="coins must be non-empty"):
        Engine(market_data=md, strategy=s, coins=())


# ---------------------------------------------------------------------------
# Test 2: init defaults
# ---------------------------------------------------------------------------


def test_init_defaults(mocker):
    md = mocker.AsyncMock(spec=MarketDataSource)
    s = mocker.AsyncMock(spec=Strategy)
    engine = Engine(market_data=md, strategy=s, coins=("BTC",))
    assert isinstance(engine._recorder, NullRecorder)
    assert callable(engine._clock_fn)
    assert engine._sleep is asyncio.sleep


# ---------------------------------------------------------------------------
# Test 3: NullRecorder methods are async no-ops
# ---------------------------------------------------------------------------


async def test_null_recorder_methods_are_async_noops():
    r = NullRecorder()
    await r.save_quote(_quote())
    await r.save_funding(_funding())
    await r.save_tick_report(_tick_report())
    await r.save_equity(_equity())


# ---------------------------------------------------------------------------
# Test 4: tick_once first tick does hour and minute
# ---------------------------------------------------------------------------


async def test_tick_once_first_tick_does_hour_and_minute(market_data, strategy, recorder, mocker):
    now = _T0

    market_data.fetch_quote.side_effect = [_quote("BTC", 100.0), _quote("ETH", 200.0)]
    market_data.fetch_funding.side_effect = [
        _funding("BTC", rate=0.0001),
        _funding("ETH", rate=0.00005),
    ]
    strategy.on_hour_tick.return_value = _tick_report(ts=now)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    outcome = await engine.tick_once(now)

    # Basic outcome fields
    assert outcome.ts == now
    assert set(outcome.quotes.keys()) == {"BTC", "ETH"}
    assert outcome.quotes["BTC"].mark == 100.0
    assert outcome.quotes["ETH"].mark == 200.0
    assert outcome.funding is not None
    assert set(outcome.funding.keys()) == {"BTC", "ETH"}
    assert outcome.tick_report is not None
    assert outcome.equity == _equity()

    # Strategy calls
    strategy.on_minute_tick.assert_called_once_with(now, outcome.quotes)
    strategy.on_hour_tick.assert_called_once_with(now, outcome.funding)
    strategy.compute_equity.assert_called_once_with(now)

    # Market data calls
    assert market_data.fetch_quote.call_count == 2
    assert market_data.fetch_funding.call_count == 2

    # Recorder calls
    assert recorder.save_quote.call_count == 2
    assert recorder.save_funding.call_count == 2
    assert recorder.save_tick_report.call_count == 1
    assert recorder.save_equity.call_count == 1


# ---------------------------------------------------------------------------
# Test 5: subsequent minute within same hour skips funding
# ---------------------------------------------------------------------------


async def test_tick_once_subsequent_minute_within_same_hour_skips_funding(
    market_data, strategy, recorder
):
    t0 = _T0  # on the hour

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report(ts=t0)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    # First tick
    market_data.fetch_quote.side_effect = [_quote("BTC"), _quote("ETH")]
    market_data.fetch_funding.side_effect = [_funding("BTC"), _funding("ETH")]
    await engine.tick_once(t0)

    # Reset side_effect for second call; use return_value
    market_data.fetch_quote.side_effect = None
    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.side_effect = None
    market_data.fetch_funding.return_value = _funding()

    # Second tick — same hour
    t1 = t0 + _MINUTE
    assert t1.replace(minute=0, second=0, microsecond=0) == t0.replace(minute=0, second=0, microsecond=0)

    outcome2 = await engine.tick_once(t1)

    assert outcome2.funding is None
    assert outcome2.tick_report is None

    # on_hour_tick only called once (from first tick)
    assert strategy.on_hour_tick.call_count == 1
    # on_minute_tick called twice total
    assert strategy.on_minute_tick.call_count == 2
    # compute_equity called twice total
    assert strategy.compute_equity.call_count == 2
    # fetch_funding total = 2 (only from first tick, 2 coins)
    assert market_data.fetch_funding.call_count == 2


# ---------------------------------------------------------------------------
# Test 6: new hour triggers funding again
# ---------------------------------------------------------------------------


async def test_tick_once_new_hour_triggers_funding_again(market_data, strategy, recorder):
    t0 = _T0

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    # First tick
    market_data.fetch_quote.side_effect = [_quote("BTC"), _quote("ETH")]
    market_data.fetch_funding.side_effect = [_funding("BTC"), _funding("ETH")]
    await engine.tick_once(t0)

    # Second tick — new hour
    t1 = t0 + _HOUR
    assert t1.replace(minute=0, second=0, microsecond=0) != t0.replace(minute=0, second=0, microsecond=0)

    market_data.fetch_quote.side_effect = [_quote("BTC"), _quote("ETH")]
    market_data.fetch_funding.side_effect = [_funding("BTC"), _funding("ETH")]

    outcome2 = await engine.tick_once(t1)

    assert outcome2.funding is not None
    assert outcome2.tick_report is not None

    # on_hour_tick called twice
    assert strategy.on_hour_tick.call_count == 2
    # fetch_funding called 4 times total (2 coins × 2 ticks)
    assert market_data.fetch_funding.call_count == 4


# ---------------------------------------------------------------------------
# Test 7: tick_once propagates strategy tick_report with signals
# ---------------------------------------------------------------------------


async def test_tick_once_propagates_strategy_tick_report(market_data, strategy, recorder):
    now = _T0

    signal = SignalEvent(
        coin="BTC",
        ts=now,
        signal_value=1.5,
        regime_pass=True,
        action="OPEN",
    )
    expected_report = TickReport(
        ts=now,
        signals=(signal,),
        fills=(),
        opened=("BTC",),
        closed=(),
    )

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = expected_report

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
    )

    outcome = await engine.tick_once(now)

    assert outcome.tick_report is not None
    assert len(outcome.tick_report.signals) == 1
    assert outcome.tick_report.signals[0].coin == "BTC"
    assert outcome.tick_report.signals[0].action == "OPEN"


# ---------------------------------------------------------------------------
# Test 8: concurrent quote fetch — functional verification
# ---------------------------------------------------------------------------


async def test_concurrent_quote_fetch(market_data, strategy, recorder):
    now = _T0
    coins = ("BTC", "ETH", "SOL")

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=coins,
        recorder=recorder,
    )

    await engine.tick_once(now)

    assert market_data.fetch_quote.call_count == len(coins)


# ---------------------------------------------------------------------------
# Test 9: quote fetch failure propagates
# ---------------------------------------------------------------------------


async def test_quote_fetch_failure_propagates(market_data, strategy, recorder):
    now = _T0

    market_data.fetch_quote.side_effect = [_quote("BTC"), RuntimeError("boom")]

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await engine.tick_once(now)


# ---------------------------------------------------------------------------
# Test 10: recorder receives all saves in order
# ---------------------------------------------------------------------------


async def test_recorder_receives_all_saves_in_order(market_data, strategy, recorder):
    now = _T0

    btc_quote = _quote("BTC", 100.0)
    eth_quote = _quote("ETH", 200.0)
    btc_funding = _funding("BTC", rate=0.0001)
    eth_funding = _funding("ETH", rate=0.00005)
    eq = _equity()
    report = _tick_report(ts=now)

    market_data.fetch_quote.side_effect = [btc_quote, eth_quote]
    market_data.fetch_funding.side_effect = [btc_funding, eth_funding]
    strategy.on_hour_tick.return_value = report
    strategy.compute_equity.return_value = eq

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    await engine.tick_once(now)

    # Counts
    assert recorder.save_quote.call_count == 2
    assert recorder.save_funding.call_count == 2
    assert recorder.save_tick_report.call_count == 1
    assert recorder.save_equity.call_count == 1

    # Payloads
    quote_args = [call.args[0] for call in recorder.save_quote.call_args_list]
    assert btc_quote in quote_args
    assert eth_quote in quote_args

    funding_args = [call.args[0] for call in recorder.save_funding.call_args_list]
    assert btc_funding in funding_args
    assert eth_funding in funding_args

    recorder.save_tick_report.assert_called_once_with(report)
    recorder.save_equity.assert_called_once_with(eq)


# ---------------------------------------------------------------------------
# Test 11: stop is idempotent
# ---------------------------------------------------------------------------


def test_stop_idempotent(mocker):
    md = mocker.AsyncMock(spec=MarketDataSource)
    s = mocker.AsyncMock(spec=Strategy)
    engine = Engine(market_data=md, strategy=s, coins=("BTC",))

    engine.stop()
    engine.stop()

    assert engine._stop is True


def test_force_hour_tick_resets_last_hour(mocker):
    """force_hour_tick clears _last_hour so the next tick crosses the boundary."""
    md = mocker.AsyncMock(spec=MarketDataSource)
    s = mocker.AsyncMock(spec=Strategy)
    engine = Engine(market_data=md, strategy=s, coins=("BTC",))

    engine._last_hour = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    engine.force_hour_tick()

    assert engine._last_hour is None


# ---------------------------------------------------------------------------
# Test 12: run calls tick_once until stop
# ---------------------------------------------------------------------------


async def test_run_calls_tick_once_until_stop(market_data, strategy, mocker):
    # Clock returns a sequence of datetimes
    t0 = _T0
    clock = mocker.MagicMock(
        side_effect=[
            t0,
            t0 + timedelta(seconds=30),
            t0 + timedelta(seconds=60),
            t0 + timedelta(seconds=90),
        ]
    )

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_fn=clock,
    )

    calls = 0

    async def fake_sleep(s):
        nonlocal calls
        calls += 1
        if calls >= 2:
            engine.stop()

    sleep_fn = mocker.AsyncMock(side_effect=fake_sleep)
    engine._sleep = sleep_fn

    await engine.run()

    assert sleep_fn.call_count == 2
    # Only 1 tick fired: first sleep completes without stop → tick. Second sleep calls stop → break.
    assert strategy.compute_equity.call_count == 1


# ---------------------------------------------------------------------------
# Test 13: run sleep delay computed to next minute boundary
# ---------------------------------------------------------------------------


async def test_run_sleep_delay_computed_to_next_minute_boundary(market_data, strategy, mocker):
    # Use a time that is NOT on the minute boundary so delay > 0
    now = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)  # :13:20
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)  # :14:00
    expected_delay = (next_minute - now).total_seconds()  # 40s

    clock = mocker.MagicMock(return_value=now)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_fn=clock,
    )

    sleep_args = []

    async def fake_sleep(s):
        sleep_args.append(s)
        engine.stop()

    engine._sleep = fake_sleep

    await engine.run()

    assert len(sleep_args) >= 1
    assert sleep_args[0] == pytest.approx(expected_delay)


# ---------------------------------------------------------------------------
# Test 14: run does not tick after stop called during sleep
# ---------------------------------------------------------------------------


async def test_run_does_not_tick_after_stop_called_during_sleep(market_data, strategy, mocker):
    t0 = _T0
    clock = mocker.MagicMock(return_value=t0)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_fn=clock,
    )

    async def fake_sleep(s):
        engine.stop()

    engine._sleep = fake_sleep

    await engine.run()

    # stop was called during sleep → break before tick
    assert strategy.compute_equity.call_count == 0


# ---------------------------------------------------------------------------
# Test 15: tick_once without event_bus runs cleanly
# ---------------------------------------------------------------------------


async def test_tick_once_without_event_bus_does_not_publish(market_data, strategy, recorder):
    now = _T0
    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(_spot_buy_fill("BTC"), _perp_sell_fill("BTC")),
        opened=("BTC",),
        closed=(),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
    )

    outcome = await engine.tick_once(now)

    assert outcome.tick_report is not None
    recorder.save_tick_report.assert_called_once()


# ---------------------------------------------------------------------------
# Test 16: tick_once publishes position.opened
# ---------------------------------------------------------------------------


async def test_tick_once_publishes_position_opened(market_data, strategy, recorder, mocker):
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(_spot_buy_fill("BTC", price=100.0, qty=10.0), _perp_sell_fill("BTC", price=100.0, qty=10.0)),
        opened=("BTC",),
        closed=(),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )

    await engine.tick_once(now)

    published_events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = [e.kind for e in published_events]
    assert "position.opened" in kinds
    assert "tick.completed" in kinds
    event: Event = next(e for e in published_events if e.kind == "position.opened")
    assert event.source == "engine"
    assert event.level == "INFO"
    assert event.payload_json["coin"] == "BTC"
    assert event.payload_json["spot_entry_price"] == 100.0
    assert event.payload_json["spot_qty"] == 10.0
    assert event.payload_json["perp_entry_price"] == 100.0
    assert event.payload_json["perp_qty"] == 10.0
    assert event.payload_json["is_paper"] is True


# ---------------------------------------------------------------------------
# Test 17: tick_once publishes position.closed
# ---------------------------------------------------------------------------


async def test_tick_once_publishes_position_closed(market_data, strategy, recorder, mocker):
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(_spot_sell_fill("BTC", price=110.0, qty=10.0), _perp_buy_fill("BTC", price=110.0, qty=10.0)),
        opened=(),
        closed=("BTC",),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )

    await engine.tick_once(now)

    published_events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = [e.kind for e in published_events]
    assert "position.closed" in kinds
    assert "tick.completed" in kinds
    event: Event = next(e for e in published_events if e.kind == "position.closed")
    assert event.source == "engine"
    assert event.level == "INFO"
    assert event.payload_json["coin"] == "BTC"
    assert event.payload_json["spot_exit_price"] == 110.0
    assert event.payload_json["spot_qty"] == 10.0
    assert event.payload_json["perp_exit_price"] == 110.0
    assert event.payload_json["perp_qty"] == 10.0
    assert event.payload_json["is_paper"] is True


# ---------------------------------------------------------------------------
# Test 18: tick_once publishes both opened and closed in same tick
# ---------------------------------------------------------------------------


async def test_tick_once_publishes_both_opened_and_closed_in_same_tick(
    market_data, strategy, recorder, mocker
):
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(
            _spot_buy_fill("BTC", price=100.0),
            _perp_sell_fill("BTC", price=100.0),
            _spot_sell_fill("ETH", price=110.0),
            _perp_buy_fill("ETH", price=110.0),
        ),
        opened=("BTC",),
        closed=("ETH",),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
        event_bus=bus,
    )

    await engine.tick_once(now)

    assert bus.publish.call_count == 3  # position.opened + position.closed + tick.completed
    published_events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = {e.kind for e in published_events}
    assert "position.opened" in kinds
    assert "position.closed" in kinds
    assert "tick.completed" in kinds
    opened_event = next(e for e in published_events if e.kind == "position.opened")
    closed_event = next(e for e in published_events if e.kind == "position.closed")
    assert opened_event.payload_json["coin"] == "BTC"
    assert closed_event.payload_json["coin"] == "ETH"


# ---------------------------------------------------------------------------
# Test 19: tick_once skips publish when open fill missing
# ---------------------------------------------------------------------------


async def test_tick_once_skips_publish_when_open_fill_missing(
    market_data, strategy, recorder, mocker
):
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(),
        opened=("BTC",),
        closed=(),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )

    outcome = await engine.tick_once(now)

    # position.opened skipped (no fill), but tick.completed is always published
    published_events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = [e.kind for e in published_events]
    assert "position.opened" not in kinds
    assert "tick.completed" in kinds
    assert outcome.tick_report is not None


# ---------------------------------------------------------------------------
# Test 20: run publishes engine.started and engine.stopping
# ---------------------------------------------------------------------------


async def test_run_publishes_engine_started_and_stopping(market_data, strategy, mocker):
    t0 = _T0
    clock = mocker.MagicMock(
        side_effect=[
            t0,               # engine.started
            t0,               # first loop iteration: now
            t0 + timedelta(seconds=30),  # second loop iteration: now (after stop)
            t0 + timedelta(seconds=60),  # engine.stopping
        ]
    )

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    bus = mocker.AsyncMock(spec=EventBus)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_fn=clock,
        event_bus=bus,
    )

    async def fake_sleep(s):
        engine.stop()

    engine._sleep = fake_sleep

    await engine.run()

    assert bus.publish.call_count >= 2
    all_events = [call.args[0] for call in bus.publish.call_args_list]
    assert all_events[0].kind == "engine.started"
    assert all_events[-1].kind == "engine.stopping"


# ---------------------------------------------------------------------------
# Test 21: tick_once skips publish when close fill missing
# ---------------------------------------------------------------------------


async def test_tick_once_skips_publish_when_close_fill_missing(
    market_data, strategy, recorder, mocker
):
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(),
        opened=(),
        closed=("BTC",),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )

    outcome = await engine.tick_once(now)

    # position.closed skipped (no fill), but tick.completed is always published
    published_events = [call.args[0] for call in bus.publish.call_args_list]
    kinds = [e.kind for e in published_events]
    assert "position.closed" not in kinds
    assert "tick.completed" in kinds
    assert outcome.tick_report is not None


# ---------------------------------------------------------------------------
# Test 22: tick.completed published on minute tick (no funding / not hour tick)
# ---------------------------------------------------------------------------


async def test_tick_completed_published_on_minute_tick(market_data, strategy, recorder, mocker):
    """Minute tick (not hour boundary): tick.completed with is_hour_tick=False,
    empty opened/closed, and correct total_equity."""
    bus = mocker.AsyncMock(spec=EventBus)
    equity = _equity(total=7500.0)
    strategy.compute_equity = mocker.MagicMock(return_value=equity)

    # Put engine in state where _last_hour is already set to _T0 so the next
    # minute tick does NOT cross an hour boundary.
    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )
    engine._last_hour = _T0.replace(minute=0, second=0, microsecond=0)

    # Tick at _T0 + 1 minute (same hour, no funding)
    t1 = _T0 + _MINUTE
    market_data.fetch_quote.return_value = _quote("BTC", ts=t1)

    await engine.tick_once(t1)

    published_events = [call.args[0] for call in bus.publish.call_args_list]
    tick_events = [e for e in published_events if e.kind == "tick.completed"]
    assert len(tick_events) == 1

    evt = tick_events[0]
    assert evt.source == "engine"
    assert evt.level == "INFO"
    assert evt.ts == t1
    assert evt.payload_json["is_hour_tick"] is False
    assert evt.payload_json["opened_coins"] == []
    assert evt.payload_json["closed_coins"] == []
    assert evt.payload_json["total_equity"] == 7500.0


# ---------------------------------------------------------------------------
# Test 23: tick.completed published on hour tick with correct opened/closed
# ---------------------------------------------------------------------------


async def test_tick_completed_published_on_hour_tick(market_data, strategy, recorder, mocker):
    """Hour tick: tick.completed with is_hour_tick=True and correct opened/closed lists."""
    now = _T0  # on-the-hour → always crosses hour boundary on first tick
    bus = mocker.AsyncMock(spec=EventBus)
    equity = _equity(total=9000.0)
    strategy.compute_equity = mocker.MagicMock(return_value=equity)

    market_data.fetch_quote.return_value = _quote("BTC", ts=now)
    market_data.fetch_funding.return_value = _funding("BTC", ts=now)
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(
            _spot_buy_fill("BTC", price=100.0),
            _perp_sell_fill("BTC", price=100.0),
        ),
        opened=("BTC",),
        closed=(),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        recorder=recorder,
        event_bus=bus,
    )

    await engine.tick_once(now)

    published_events = [call.args[0] for call in bus.publish.call_args_list]
    tick_events = [e for e in published_events if e.kind == "tick.completed"]
    assert len(tick_events) == 1

    evt = tick_events[0]
    assert evt.source == "engine"
    assert evt.level == "INFO"
    assert evt.ts == now
    assert evt.payload_json["is_hour_tick"] is True
    assert evt.payload_json["opened_coins"] == ["BTC"]
    assert evt.payload_json["closed_coins"] == []
    assert evt.payload_json["total_equity"] == 9000.0


async def test_tick_completed_hour_tick_with_closed_coins(market_data, strategy, recorder, mocker):
    """Hour tick with closed coins: tick.completed includes closed_coins."""
    now = _T0
    bus = mocker.AsyncMock(spec=EventBus)
    equity = _equity(total=8000.0)
    strategy.compute_equity = mocker.MagicMock(return_value=equity)

    market_data.fetch_quote.return_value = _quote("ETH", ts=now)
    market_data.fetch_funding.return_value = _funding("ETH", ts=now)
    strategy.on_hour_tick.return_value = TickReport(
        ts=now,
        signals=(),
        fills=(
            _spot_sell_fill("ETH", price=110.0),
            _perp_buy_fill("ETH", price=110.0),
        ),
        opened=(),
        closed=("ETH",),
    )

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("ETH",),
        recorder=recorder,
        event_bus=bus,
    )

    await engine.tick_once(now)

    published_events = [call.args[0] for call in bus.publish.call_args_list]
    tick_events = [e for e in published_events if e.kind == "tick.completed"]
    assert len(tick_events) == 1

    evt = tick_events[0]
    assert evt.payload_json["is_hour_tick"] is True
    assert evt.payload_json["opened_coins"] == []
    assert evt.payload_json["closed_coins"] == ["ETH"]
    assert evt.payload_json["total_equity"] == 8000.0
