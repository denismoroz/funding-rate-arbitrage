"""Tests for EngineLoop (src/frab/engine/loop.py).

Uses mocker (pytest-mock) to mock Exchange, Strategy, Ledger. Time is
controlled by patching frab.engine.loop.utcnow_ms. No real sleeping beyond
a tiny minute_interval_s (0.05 s) to let the loop iterate quickly.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Exchange as ExchangeRow, FundingRate as FundingRateRow, Price as PriceRow, Strategy as StrategyRow
from frab.db.session import init_db, make_session_factory, session_scope
from frab.engine.loop import EngineLoop, utcnow_ms
from frab.events.bus import EventBus
from frab.exchanges.protocol import FundingTick, Quote, WalletKind
from frab.strategy.two_phase import TwoPhaseParams


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _make_quote(coin: str = "BTC") -> Quote:
    return Quote(coin=coin, mark=50000.0, spot=49900.0, bid=49950.0, ask=50050.0, ts_ms=1_700_000_000_000)


def _make_funding_tick(coin: str = "BTC") -> FundingTick:
    return FundingTick(coin=coin, ts_ms=1_700_000_000_000, rate=0.0001, premium=0.0001, annualized_pct=8.76)


@pytest.fixture
def mock_exchange():
    exc = MagicMock()
    exc.name = "hyperliquid"
    exc.get_quote = AsyncMock(side_effect=lambda coin: _make_quote(coin))
    exc.get_funding_rate = AsyncMock(side_effect=lambda coin: _make_funding_tick(coin))
    exc.get_wallet = AsyncMock(return_value=1000.0)
    return exc


@pytest.fixture
def mock_strategy():
    strat = MagicMock()
    strat.strategy_id = 1
    strat.on_minute_tick = AsyncMock()
    strat.on_hour_tick = AsyncMock()
    return strat


@pytest.fixture
def mock_ledger():
    led = MagicMock()
    led.compute_and_save = AsyncMock()
    return led


@pytest.fixture
def mock_session_factory():
    """Minimal session_factory that won't be called in unit tests (exchange id is cached)."""
    return MagicMock()


@pytest.fixture
def event_bus():
    return EventBus()


def make_loop(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
    event_bus=None, coins=None, minute_interval_s=0.05,
    wallet_coins=None,
):
    coins = coins or ["BTC", "ETH"]
    return EngineLoop(
        strategy=mock_strategy,
        exchange=mock_exchange,
        ledger=mock_ledger,
        session_factory=mock_session_factory,
        coins=coins,
        event_bus=event_bus,
        minute_interval_s=minute_interval_s,
        wallet_coins=wallet_coins,
    )


# ─── Helper: run loop for a short duration then stop ─────────────────────────

async def _run_loop_briefly(loop: EngineLoop, duration_s: float = 0.25) -> None:
    await loop.start()
    await asyncio.sleep(duration_s)
    await loop.stop()


# ─── 1. _minute_tick: quotes fetched, prices saved, strategy + ledger called ─

@pytest.mark.asyncio
async def test_minute_tick_fetches_quotes_and_calls_strategy_and_ledger(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory, coins=["BTC", "ETH"])

    # Patch exchange_id resolution and _save_prices so no real DB is needed
    loop._exchange_id_cache = 1
    loop._save_prices = AsyncMock()

    now_ms = 1_700_000_060_000
    await loop._minute_tick(now_ms)

    # get_quote called once per coin
    assert mock_exchange.get_quote.call_count == 2
    mock_exchange.get_quote.assert_any_call("BTC")
    mock_exchange.get_quote.assert_any_call("ETH")

    # strategy.on_minute_tick called
    mock_strategy.on_minute_tick.assert_awaited_once_with(now_ms=now_ms)

    # ledger.compute_and_save called with strategy_id and quote dict
    mock_ledger.compute_and_save.assert_awaited_once()
    args, kwargs = mock_ledger.compute_and_save.call_args
    assert args[0] == mock_strategy.strategy_id  # strategy_id
    assert "BTC" in args[1]
    assert "ETH" in args[1]


# ─── 2. _hour_tick: funding fetched, saved, wallet refreshed, strategy called ─

@pytest.mark.asyncio
async def test_hour_tick_fetches_funding_and_refreshes_wallets_and_calls_strategy(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    wallet_coins = [("USDC", WalletKind.SPOT), ("USDC", WalletKind.PERP)]
    loop = make_loop(
        mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
        coins=["BTC", "ETH"], wallet_coins=wallet_coins,
    )

    loop._exchange_id_cache = 1
    loop._save_funding = AsyncMock()

    now_ms = 1_700_003_600_000
    await loop._hour_tick(now_ms)

    # get_funding_rate called once per coin
    assert mock_exchange.get_funding_rate.call_count == 2
    mock_exchange.get_funding_rate.assert_any_call("BTC")
    mock_exchange.get_funding_rate.assert_any_call("ETH")

    # get_wallet called for each wallet_coin pair
    assert mock_exchange.get_wallet.call_count == 2
    mock_exchange.get_wallet.assert_any_call("USDC", WalletKind.SPOT)
    mock_exchange.get_wallet.assert_any_call("USDC", WalletKind.PERP)

    # strategy.on_hour_tick called
    mock_strategy.on_hour_tick.assert_awaited_once_with(now_ms=now_ms)


# ─── 3. Hour boundary detection ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hour_tick_fires_on_hour_boundary(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    """Minute tick at 12:59:30 should NOT fire hour tick. At 13:00:00 it should."""
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory)
    loop._exchange_id_cache = 1
    loop._save_prices = AsyncMock()
    loop._save_funding = AsyncMock()

    # Simulate 12:59:30 — hour index = (ts_ms // 3_600_000)
    # Choose explicit ms values: hour 0 is 0-3599999, hour 1 is 3600000-7199999, etc.
    ms_12_59 = 1_700_000_000_000  # arbitrary, just need two different hours
    ms_13_00 = ms_12_59 + 3_600_000

    hour_before = ms_12_59 // 3_600_000
    hour_after = ms_13_00 // 3_600_000
    assert hour_after != hour_before, "test setup: hours must differ"

    # First minute tick — hour changes from None → hour_before → hour tick fires
    loop._last_hour = None
    # Manually simulate what _run does (without actual sleeping):
    # At ms_12_59 → first tick, _last_hour is None so hour tick fires (initialisation)
    current_hour = ms_12_59 // 3_600_000
    await loop._minute_tick(ms_12_59)
    should_run_hour = loop._last_hour is None or current_hour != loop._last_hour
    if should_run_hour:
        await loop._hour_tick(ms_12_59)
    loop._last_hour = current_hour

    # Hour tick ran once
    assert mock_strategy.on_hour_tick.call_count == 1

    # Second minute tick within same hour — no hour tick
    same_hour_ms = ms_12_59 + 30_000
    current_hour2 = same_hour_ms // 3_600_000
    assert current_hour2 == current_hour
    await loop._minute_tick(same_hour_ms)
    should_run_hour2 = loop._last_hour is None or current_hour2 != loop._last_hour
    if should_run_hour2:
        await loop._hour_tick(same_hour_ms)
        loop._last_hour = current_hour2

    # Still only one hour tick
    assert mock_strategy.on_hour_tick.call_count == 1

    # Third tick — new hour — hour tick must fire
    current_hour3 = ms_13_00 // 3_600_000
    await loop._minute_tick(ms_13_00)
    should_run_hour3 = loop._last_hour is None or current_hour3 != loop._last_hour
    if should_run_hour3:
        await loop._hour_tick(ms_13_00)
        loop._last_hour = current_hour3

    assert mock_strategy.on_hour_tick.call_count == 2


# ─── 4. strategy.on_minute_tick raises → error logged, loop continues ─────────

@pytest.mark.asyncio
async def test_minute_tick_error_does_not_kill_loop(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory, event_bus,
):
    """on_minute_tick raises → error event logged; loop keeps ticking."""
    call_count = 0

    async def flaky_on_minute_tick(*, now_ms):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("strategy exploded")

    mock_strategy.on_minute_tick = flaky_on_minute_tick
    loop = make_loop(
        mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
        event_bus=event_bus, minute_interval_s=0.05,
    )
    loop._exchange_id_cache = 1
    loop._save_prices = AsyncMock()
    loop._last_hour = 999999  # prevent hour ticks during test

    await _run_loop_briefly(loop, duration_s=0.35)

    # Loop survived and strategy was called more than once
    assert call_count >= 2


# ─── 5. exchange.get_quote fails for one coin → other coins still saved ────────

@pytest.mark.asyncio
async def test_quote_failure_one_coin_other_coins_still_fetched(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    """If get_quote raises for one coin, the other coins should still be fetched."""
    async def get_quote_side(coin):
        if coin == "BTC":
            raise ConnectionError("HL unreachable")
        return _make_quote(coin)

    mock_exchange.get_quote = AsyncMock(side_effect=get_quote_side)

    loop = make_loop(
        mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
        coins=["BTC", "ETH", "SOL"],
    )
    loop._exchange_id_cache = 1
    loop._save_prices = AsyncMock()

    now_ms = 1_700_000_060_000
    await loop._minute_tick(now_ms)

    # BTC failed, ETH and SOL succeeded
    assert mock_exchange.get_quote.call_count == 3
    # _save_prices was called with the two successful quotes
    loop._save_prices.assert_awaited_once()
    saved_quotes = loop._save_prices.call_args[0][0]
    saved_coins = {q.coin for q in saved_quotes}
    assert "BTC" not in saved_coins
    assert "ETH" in saved_coins
    assert "SOL" in saved_coins


# ─── 6. asyncio.CancelledError propagates ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_minute_tick(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    mock_exchange.get_quote = AsyncMock(side_effect=asyncio.CancelledError)

    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory)
    loop._exchange_id_cache = 1

    with pytest.raises(asyncio.CancelledError):
        await loop._minute_tick(1_700_000_000_000)


# ─── 7. start() is idempotent; second start is a no-op ─────────────────────────

@pytest.mark.asyncio
async def test_start_is_idempotent(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory)
    loop._exchange_id_cache = 1
    loop._last_hour = 999999  # suppress hour ticks

    await loop.start()
    task_first = loop._task

    # Second start should not spawn a new task
    await loop.start()
    assert loop._task is task_first, "second start() must not replace the running task"

    await loop.stop()


# ─── 8. stop() cancels cleanly; subsequent stop() is no-op ────────────────────

@pytest.mark.asyncio
async def test_stop_cancels_and_subsequent_stop_is_noop(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory,
):
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory)
    loop._exchange_id_cache = 1
    loop._last_hour = 999999

    await loop.start()
    assert loop._task is not None
    assert not loop._task.done()

    await loop.stop()
    assert loop._task.done()

    # Subsequent stop must not raise
    await loop.stop()


# ─── DB fixtures for idempotency tests ───────────────────────────────────────

@pytest.fixture
async def db_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return make_session_factory(db_engine)


@pytest.fixture
async def seeded_db_session_factory(db_session_factory):
    """Session factory with the 'hyperliquid' exchange row seeded."""
    async with session_scope(db_session_factory) as s:
        s.add(ExchangeRow(
            name="hyperliquid",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=3.5,
        ))
    return db_session_factory


# ─── 9. _save_funding is idempotent on duplicate ts_ms ────────────────────────

@pytest.mark.asyncio
async def test_save_funding_idempotent_on_duplicate_ts(
    mock_exchange, mock_strategy, mock_ledger, seeded_db_session_factory,
):
    """Pre-insert a funding rate row, then call _save_funding with the same
    (exchange_id, coin, ts_ms) — must not raise IntegrityError and must not
    create a duplicate row."""
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, seeded_db_session_factory)
    # Force exchange_id resolution
    await loop._resolve_exchange_id()

    tick = FundingTick(coin="BTC", ts_ms=1_700_000_000_000, rate=0.0001, premium=0.0001, annualized_pct=8.76)

    # First insert — baseline
    await loop._save_funding([tick], now_ms=tick.ts_ms)

    async with session_scope(seeded_db_session_factory) as s:
        rows_before = (await s.execute(select(FundingRateRow))).scalars().all()
    assert len(rows_before) == 1

    # Second insert with same (exchange_id, coin, ts_ms) — must be a no-op
    await loop._save_funding([tick], now_ms=tick.ts_ms)

    async with session_scope(seeded_db_session_factory) as s:
        rows_after = (await s.execute(select(FundingRateRow))).scalars().all()
    assert len(rows_after) == 1, "_save_funding must not create duplicate rows on restart"


# ─── 10. _save_prices is idempotent on duplicate ts_ms ────────────────────────

@pytest.mark.asyncio
async def test_save_prices_idempotent_on_duplicate_ts(
    mock_exchange, mock_strategy, mock_ledger, seeded_db_session_factory,
):
    """Pre-insert a price row, then call _save_prices with the same
    (exchange_id, coin, ts_ms) — must not raise IntegrityError and must not
    create a duplicate row."""
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, seeded_db_session_factory)
    await loop._resolve_exchange_id()

    quote = Quote(coin="ETH", mark=3000.0, spot=2990.0, bid=2995.0, ask=3005.0, ts_ms=1_700_000_060_000)
    now_ms = quote.ts_ms

    # First insert
    await loop._save_prices([quote], now_ms=now_ms)

    async with session_scope(seeded_db_session_factory) as s:
        rows_before = (await s.execute(select(PriceRow))).scalars().all()
    assert len(rows_before) == 1

    # Second insert with same (exchange_id, coin, ts_ms) — must be a no-op
    await loop._save_prices([quote], now_ms=now_ms)

    async with session_scope(seeded_db_session_factory) as s:
        rows_after = (await s.execute(select(PriceRow))).scalars().all()
    assert len(rows_after) == 1, "_save_prices must not create duplicate rows on restart"


# ─── Fixtures for reload_params tests ────────────────────────────────────────

@pytest.fixture
async def strategy_db_session_factory(db_session_factory):
    """Session factory with an exchange + strategy row seeded."""
    async with session_scope(db_session_factory) as s:
        s.add(ExchangeRow(
            name="hyperliquid",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=3.5,
        ))
        strat = StrategyRow(
            name="two_phase_test",
            version="v2",
            params_json={
                "entry_threshold_apr": 0.25,
                "concurrency_cap": 5,
            },
        )
        s.add(strat)
        await s.flush()
    return db_session_factory


# ─── 11. _hour_tick calls reload_params with fresh DB params ──────────────────

@pytest.mark.asyncio
async def test_hour_tick_calls_reload_params_with_db_params(
    mock_exchange, mock_ledger, strategy_db_session_factory, mocker,
):
    """_hour_tick reloads params from DB and calls strategy.reload_params with the
    fresh TwoPhaseParams before running funding fetch / strategy.on_hour_tick."""
    mock_strategy = MagicMock()
    mock_strategy.strategy_id = 1
    mock_strategy.on_hour_tick = AsyncMock()
    mock_strategy.reload_params = MagicMock()  # synchronous — reload_params is not async

    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, strategy_db_session_factory)
    loop._exchange_id_cache = 1
    loop._save_funding = AsyncMock()

    now_ms = 1_700_003_600_000
    await loop._hour_tick(now_ms)

    # reload_params must have been called exactly once
    mock_strategy.reload_params.assert_called_once()
    called_params = mock_strategy.reload_params.call_args[0][0]
    assert isinstance(called_params, TwoPhaseParams)
    # The DB row has entry_threshold_apr=0.25 and concurrency_cap=5
    assert called_params.entry_threshold_apr == pytest.approx(0.25)
    assert called_params.concurrency_cap == 5

    # on_hour_tick still called normally after reload
    mock_strategy.on_hour_tick.assert_awaited_once_with(now_ms=now_ms)


@pytest.mark.asyncio
async def test_hour_tick_continues_on_reload_params_db_error(
    mock_exchange, mock_strategy, mock_ledger, mock_session_factory, mocker,
):
    """If _reload_strategy_params_from_db raises, _hour_tick logs and continues
    (stale params are better than a skipped tick)."""
    loop = make_loop(mock_exchange, mock_strategy, mock_ledger, mock_session_factory)
    loop._exchange_id_cache = 1
    loop._save_funding = AsyncMock()

    # Patch reload to always raise
    async def _fail():
        raise RuntimeError("DB unavailable")

    mocker.patch.object(loop, "_reload_strategy_params_from_db", side_effect=_fail)

    now_ms = 1_700_003_600_000
    # Must not raise
    await loop._hour_tick(now_ms)

    # on_hour_tick still called despite reload failure
    mock_strategy.on_hour_tick.assert_awaited_once_with(now_ms=now_ms)
