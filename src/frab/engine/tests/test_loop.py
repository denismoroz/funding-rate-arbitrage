"""Tests for src/frab/engine/loop.py."""
from __future__ import annotations

import asyncio

import pytest

from frab.engine.loop import (
    MS_PER_HOUR,
    MS_PER_MINUTE,
    Engine,
    NullRecorder,
    Recorder,
    TickOutcome,
)
from frab.exchanges.base import FundingTick, MarketDataSource, Quote
from frab.strategies.base import EquitySnapshot, SignalEvent, Strategy, TickReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote(coin="BTC", mark=100.0, ts_ms=1_700_000_000_000):
    return Quote(coin=coin, ts_ms=ts_ms, bid=mark, ask=mark, mark=mark, spot=None)


def _funding(coin="BTC", ts_ms=1_700_000_000_000, rate=0.0001):
    return FundingTick(
        coin=coin,
        ts_ms=ts_ms,
        rate=rate,
        premium=None,
        annualized_pct=rate * 8760 * 100,
    )


def _equity(ts_ms=1_700_000_000_000, total=6000.0):
    return EquitySnapshot(
        ts_ms=ts_ms,
        total_equity=total,
        cash=total,
        spot_value=0.0,
        perp_unrealized=0.0,
        perp_realized_cum=0.0,
        funding_cum=0.0,
        fees_cum=0.0,
    )


def _tick_report(ts_ms=1_700_000_000_000):
    return TickReport(ts_ms=ts_ms, signals=(), fills=(), opened=(), closed=())


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
    assert callable(engine._clock_ms)
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
    now_ms = 1_700_000_000_000

    market_data.fetch_quote.side_effect = [_quote("BTC", 100.0), _quote("ETH", 200.0)]
    market_data.fetch_funding.side_effect = [
        _funding("BTC", rate=0.0001),
        _funding("ETH", rate=0.00005),
    ]
    strategy.on_hour_tick.return_value = _tick_report(ts_ms=now_ms)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    outcome = await engine.tick_once(now_ms)

    # Basic outcome fields
    assert outcome.ts_ms == now_ms
    assert set(outcome.quotes.keys()) == {"BTC", "ETH"}
    assert outcome.quotes["BTC"].mark == 100.0
    assert outcome.quotes["ETH"].mark == 200.0
    assert outcome.funding is not None
    assert set(outcome.funding.keys()) == {"BTC", "ETH"}
    assert outcome.tick_report is not None
    assert outcome.equity == _equity()

    # Strategy calls
    strategy.on_minute_tick.assert_called_once_with(now_ms, outcome.quotes)
    strategy.on_hour_tick.assert_called_once_with(now_ms, outcome.funding)
    strategy.compute_equity.assert_called_once_with(now_ms)

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
    t0 = 1_700_000_000_000

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report(ts_ms=t0)

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

    # Second tick — same hour bucket
    t1 = t0 + MS_PER_MINUTE
    assert t1 // MS_PER_HOUR == t0 // MS_PER_HOUR  # same bucket

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
    t0 = 1_700_000_000_000

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
    t1 = t0 + MS_PER_HOUR
    assert t1 // MS_PER_HOUR != t0 // MS_PER_HOUR

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
    now_ms = 1_700_000_000_000

    signal = SignalEvent(
        coin="BTC",
        ts_ms=now_ms,
        signal_value=1.5,
        regime_pass=True,
        action="OPEN",
    )
    expected_report = TickReport(
        ts_ms=now_ms,
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

    outcome = await engine.tick_once(now_ms)

    assert outcome.tick_report is not None
    assert len(outcome.tick_report.signals) == 1
    assert outcome.tick_report.signals[0].coin == "BTC"
    assert outcome.tick_report.signals[0].action == "OPEN"


# ---------------------------------------------------------------------------
# Test 8: concurrent quote fetch — functional verification
# ---------------------------------------------------------------------------


async def test_concurrent_quote_fetch(market_data, strategy, recorder):
    now_ms = 1_700_000_000_000
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

    await engine.tick_once(now_ms)

    assert market_data.fetch_quote.call_count == len(coins)


# ---------------------------------------------------------------------------
# Test 9: quote fetch failure propagates
# ---------------------------------------------------------------------------


async def test_quote_fetch_failure_propagates(market_data, strategy, recorder):
    now_ms = 1_700_000_000_000

    market_data.fetch_quote.side_effect = [_quote("BTC"), RuntimeError("boom")]

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC", "ETH"),
        recorder=recorder,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await engine.tick_once(now_ms)


# ---------------------------------------------------------------------------
# Test 10: recorder receives all saves in order
# ---------------------------------------------------------------------------


async def test_recorder_receives_all_saves_in_order(market_data, strategy, recorder):
    now_ms = 1_700_000_000_000

    btc_quote = _quote("BTC", 100.0)
    eth_quote = _quote("ETH", 200.0)
    btc_funding = _funding("BTC", rate=0.0001)
    eth_funding = _funding("ETH", rate=0.00005)
    eq = _equity()
    report = _tick_report(ts_ms=now_ms)

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

    await engine.tick_once(now_ms)

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


# ---------------------------------------------------------------------------
# Test 12: run calls tick_once until stop
# ---------------------------------------------------------------------------


async def test_run_calls_tick_once_until_stop(market_data, strategy, mocker):
    t0 = 1_700_000_000_000

    clock = mocker.MagicMock(
        side_effect=[t0, t0 + 30_000, t0 + 60_000, t0 + 90_000]
    )

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_ms_fn=clock,
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
    now = 1_700_000_000_000
    # Compute expected next minute
    next_minute_ms = ((now // MS_PER_MINUTE) + 1) * MS_PER_MINUTE
    expected_delay = (next_minute_ms - now) / 1000.0

    clock = mocker.MagicMock(return_value=now)

    market_data.fetch_quote.return_value = _quote()
    market_data.fetch_funding.return_value = _funding()
    strategy.on_hour_tick.return_value = _tick_report()

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_ms_fn=clock,
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
    t0 = 1_700_000_000_000
    clock = mocker.MagicMock(return_value=t0)

    engine = Engine(
        market_data=market_data,
        strategy=strategy,
        coins=("BTC",),
        clock_ms_fn=clock,
    )

    async def fake_sleep(s):
        engine.stop()

    engine._sleep = fake_sleep

    await engine.run()

    # stop was called during sleep → break before tick
    assert strategy.compute_equity.call_count == 0
