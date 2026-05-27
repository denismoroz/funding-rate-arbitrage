"""Engine wiring tests for Strategy.margin_watchdog invocation + event publish."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from frab.domain.portfolio import Equity
from frab.engine.loop import Engine
from frab.events.bus import EventBus
from frab.exchanges.base import FundingTick, MarketDataSource, Quote
from frab.strategies.base import (
    EquitySnapshot,
    Strategy,
    TickReport,
    WatchdogAction,
    WatchdogReport,
)

T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _quote(coin: str = "BTC", mark: float = 100.0) -> Quote:
    return Quote(coin=coin, ts=T0, bid=mark, ask=mark, mark=mark, spot=None)


def _funding(coin: str = "BTC") -> FundingTick:
    return FundingTick(
        coin=coin, ts=T0, rate=0.0001, premium=None, annualized_pct=0.876,
    )


def _equity() -> EquitySnapshot:
    return EquitySnapshot(
        ts=T0, total_equity=1000.0, cash=1000.0,
        spot_value=0.0, perp_unrealized=0.0,
        perp_realized_cum=0.0, funding_cum=0.0, fees_cum=0.0,
    )


def _domain_equity() -> Equity:
    return Equity(
        ts=T0, total_equity=1000.0, cash=1000.0,
        spot_value=0.0, perp_unrealized=0.0,
        perp_realized_cum=0.0, funding_cum=0.0, fees_cum=0.0,
    )


def _tick_report() -> TickReport:
    return TickReport(ts=T0, signals=(), fills=(), opened=(), closed=())


def _make_strategy(mocker, watchdog_return):
    s = mocker.AsyncMock(spec=Strategy)
    s.on_hour_tick.return_value = _tick_report()
    s.margin_watchdog.return_value = watchdog_return
    return s


def _make_market_data(mocker):
    md = mocker.AsyncMock(spec=MarketDataSource)
    md.fetch_quote.return_value = _quote()
    md.fetch_funding.return_value = _funding()
    return md


def _make_portfolio_service(mocker):
    ps = mocker.MagicMock()
    ps.equity = mocker.MagicMock(return_value=_domain_equity())
    return ps


@pytest.mark.asyncio
async def test_engine_awaits_margin_watchdog_every_minute(mocker):
    strat = _make_strategy(mocker, None)
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",))

    await engine.tick_once(T0)
    strat.margin_watchdog.assert_awaited_once_with(T0)


@pytest.mark.asyncio
async def test_engine_does_not_publish_event_when_action_is_none(mocker):
    report = WatchdogReport(
        ts=T0, action=WatchdogAction.NONE, ratio=5.0,
        coin=None, amount_transferred=0.0, reason="healthy",
    )
    strat = _make_strategy(mocker, report)
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    bus = mocker.AsyncMock(spec=EventBus)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",), event_bus=bus)

    await engine.tick_once(T0)

    # Only the routine tick.completed event should publish, no margin.* event.
    kinds = [call.args[0].kind for call in bus.publish.call_args_list]
    assert all(not k.startswith("margin.") for k in kinds)


@pytest.mark.asyncio
async def test_engine_publishes_warning_event_on_top_up(mocker):
    report = WatchdogReport(
        ts=T0, action=WatchdogAction.TOP_UP, ratio=1.2,
        coin=None, amount_transferred=42.0, reason="topped up perp by $42.00",
    )
    strat = _make_strategy(mocker, report)
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    bus = mocker.AsyncMock(spec=EventBus)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",), event_bus=bus)

    await engine.tick_once(T0)

    margin_events = [c.args[0] for c in bus.publish.call_args_list
                     if c.args[0].kind.startswith("margin.")]
    assert len(margin_events) == 1
    ev = margin_events[0]
    assert ev.kind == "margin.top_up"
    assert ev.level == "WARNING"
    assert ev.source == "margin_watchdog"
    assert ev.payload_json["amount_transferred"] == 42.0
    assert ev.payload_json["action"] == "TOP_UP"


@pytest.mark.asyncio
async def test_engine_publishes_error_event_on_forced_close(mocker):
    report = WatchdogReport(
        ts=T0, action=WatchdogAction.FORCED_CLOSE, ratio=1.1,
        coin="ETH", amount_transferred=0.0, reason="forced close ETH",
    )
    strat = _make_strategy(mocker, report)
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    bus = mocker.AsyncMock(spec=EventBus)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",), event_bus=bus)

    await engine.tick_once(T0)

    margin_events = [c.args[0] for c in bus.publish.call_args_list
                     if c.args[0].kind.startswith("margin.")]
    assert len(margin_events) == 1
    ev = margin_events[0]
    assert ev.kind == "margin.forced_close"
    assert ev.level == "ERROR"
    assert ev.payload_json["coin"] == "ETH"


@pytest.mark.asyncio
async def test_engine_publishes_error_event_on_emergency(mocker):
    report = WatchdogReport(
        ts=T0, action=WatchdogAction.EMERGENCY, ratio=0.5,
        coin="BTC", amount_transferred=0.0, reason="emergency close BTC",
    )
    strat = _make_strategy(mocker, report)
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    bus = mocker.AsyncMock(spec=EventBus)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",), event_bus=bus)

    await engine.tick_once(T0)

    margin_events = [c.args[0] for c in bus.publish.call_args_list
                     if c.args[0].kind.startswith("margin.")]
    assert len(margin_events) == 1
    assert margin_events[0].kind == "margin.emergency"
    assert margin_events[0].level == "ERROR"


@pytest.mark.asyncio
async def test_engine_swallows_watchdog_exception(mocker):
    strat = _make_strategy(mocker, None)
    strat.margin_watchdog.side_effect = RuntimeError("boom")
    md = _make_market_data(mocker)
    ps = _make_portfolio_service(mocker)
    engine = Engine(market_data=md, strategy=strat, portfolio_service=ps, coins=("BTC",))

    outcome = await engine.tick_once(T0)
    # Tick still completes normally despite watchdog raising
    assert outcome.equity == _equity()
