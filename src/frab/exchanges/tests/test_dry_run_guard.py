"""Tests for DryRunAdapterGuard (F2.2)."""
from __future__ import annotations

import pytest
from datetime import UTC, datetime

from frab.domain.exchange import Exchange
from frab.domain.exchange_profile import ExchangeProfile
from frab.domain.position import Position
from frab.exchanges.adapter import ExchangeAdapter
from frab.exchanges.base import Quote
from frab.exchanges.dry_run import DryRunAdapterGuard


_FIXED_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def underlying(mocker):
    m = mocker.MagicMock()
    m.exchange = Exchange.HYPERLIQUID
    m.open_position = mocker.AsyncMock()
    m.close_position = mocker.AsyncMock()
    m.adjust_margin = mocker.AsyncMock()
    m.fetch_quote = mocker.AsyncMock(return_value=Quote(
        coin="BTC", ts=datetime(2026, 5, 27, tzinfo=UTC),
        bid=99.0, ask=101.0, mark=100.0, spot=100.0,
    ))
    m.get_exchange_profile = mocker.AsyncMock(return_value=ExchangeProfile(
        exchange=Exchange.HYPERLIQUID,
        funding_interval_hours=1.0,
        periods_per_year=24 * 365,
        default_spot_taker_bps=7.0,
        default_perp_taker_bps=3.5,
    ))
    m.get_wallet = mocker.AsyncMock(return_value="WALLET_SENTINEL")
    m.get_open_positions = mocker.AsyncMock(return_value=["POS"])
    m.get_market_specs = mocker.AsyncMock(return_value={"BTC": "SPEC"})
    m.fetch_funding = mocker.AsyncMock(return_value="FUNDING")
    m.fetch_funding_history = mocker.AsyncMock(return_value=["HIST"])
    m.fetch_user_fills = mocker.AsyncMock(return_value=["FILL"])
    m.fetch_user_funding = mocker.AsyncMock(return_value=["PAYMENT"])
    m.startup_validate = mocker.AsyncMock()
    m.close = mocker.AsyncMock()
    return m


@pytest.fixture
def guard(underlying):
    return DryRunAdapterGuard(underlying, clock_fn=lambda: _FIXED_NOW)


# ---------------------------------------------------------------------------
# 1. All reads forward to underlying
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_forward_to_underlying(guard, underlying):
    assert await guard.get_wallet() == "WALLET_SENTINEL"
    underlying.get_wallet.assert_awaited_once()

    assert await guard.get_open_positions() == ["POS"]
    underlying.get_open_positions.assert_awaited_once()

    assert await guard.get_market_specs() == {"BTC": "SPEC"}
    underlying.get_market_specs.assert_awaited_once()

    assert await guard.fetch_quote("BTC") == underlying.fetch_quote.return_value
    # fetch_quote is also called by open/close; here called once explicitly
    underlying.fetch_quote.assert_awaited()

    assert await guard.fetch_funding("BTC") == "FUNDING"
    underlying.fetch_funding.assert_awaited_once()

    assert await guard.fetch_funding_history("BTC", 0) == ["HIST"]
    underlying.fetch_funding_history.assert_awaited_once_with("BTC", 0)

    assert await guard.fetch_user_fills(0) == ["FILL"]
    underlying.fetch_user_fills.assert_awaited_once_with(0)

    assert await guard.fetch_user_funding(0) == ["PAYMENT"]
    underlying.fetch_user_funding.assert_awaited_once_with(0)

    await guard.startup_validate(("BTC",))
    underlying.startup_validate.assert_awaited_once_with(("BTC",))

    await guard.close()
    underlying.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. open_position must not call underlying.open_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_does_not_call_underlying(guard, underlying):
    await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=200.0)
    assert underlying.open_position.await_count == 0


# ---------------------------------------------------------------------------
# 3. open_position synthesises correct fill prices with slippage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_returns_position_with_synth_fill_price(guard):
    slip = 2.0 / 1e4
    expected_spot = 101.0 * (1.0 + slip)   # 101.0202
    expected_perp = 100.0 - 100.0 * slip * 0.5  # 99.99
    pos = await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=200.0)
    assert abs(pos.entry_spot_price - expected_spot) < 1e-9
    assert abs(pos.entry_perp_price - expected_perp) < 1e-9


# ---------------------------------------------------------------------------
# 4. spot_qty * spot_fill_price ≈ notional_usd
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_qty_matches_notional(guard):
    notional = 1000.0
    pos = await guard.open_position("BTC", notional_usd=notional, margin_reserve_usd=200.0)
    assert abs(pos.spot_qty * pos.entry_spot_price - notional) < 1e-6


# ---------------------------------------------------------------------------
# 5. paper position is cached after open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_records_paper_position_for_close(guard):
    pos = await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=200.0)
    assert guard._paper_positions["BTC"] is pos


# ---------------------------------------------------------------------------
# 6. close_position must not call underlying.close_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_does_not_call_underlying(guard, underlying):
    await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=200.0)
    await guard.close_position("BTC")
    assert underlying.close_position.await_count == 0


# ---------------------------------------------------------------------------
# 7. close_position raises ValueError when no open paper position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_raises_when_no_open(guard):
    with pytest.raises(ValueError, match="no paper position open for ETH"):
        await guard.close_position("ETH")


# ---------------------------------------------------------------------------
# 8. released_margin_usd matches what was passed to open_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_releases_margin(guard):
    margin = 350.0
    await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=margin)
    closed = await guard.close_position("BTC")
    assert closed.released_margin_usd == margin


# ---------------------------------------------------------------------------
# 9. realized_pnl is approximately correct (flat market, check sign / magnitude)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_computes_realized_pnl_when_flat(guard):
    # ask=101, bid=99, mark=100, slip=0.0002
    # spot open:  101 * 1.0002 = 101.0202
    # perp open:  100 - 100*0.0001 = 99.99
    # spot close: 99 * (1 - 0.0002) = 98.9802
    # perp close: 100 + 100*0.0001 = 100.01
    # notional=1000 -> spot_qty = 1000/101.0202 ≈ 9.899
    # perp_realized = (99.99 - 100.01) * qty ≈ -0.198
    # spot_realized = (98.9802 - 101.0202) * qty ≈ -20.2
    # PnL is negative (round-trip slippage cost), roughly -20.4
    await guard.open_position("BTC", notional_usd=1000.0, margin_reserve_usd=200.0)
    closed = await guard.close_position("BTC")
    # Both legs move against us (paying spread); total pnl should be < 0
    assert closed.realized_pnl < 0
    # Should be small relative to notional (within ~5%)
    assert abs(closed.realized_pnl) < 1000.0 * 0.05


# ---------------------------------------------------------------------------
# 10. adjust_margin is a no-op and never calls underlying
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_margin_is_noop(guard, underlying):
    result = await guard.adjust_margin("BTC", 50.0)
    assert result is None
    assert underlying.adjust_margin.await_count == 0


# ---------------------------------------------------------------------------
# 11. DryRunAdapterGuard satisfies ExchangeAdapter Protocol
# ---------------------------------------------------------------------------

def test_guard_satisfies_protocol(underlying):
    g = DryRunAdapterGuard(underlying)
    assert isinstance(g, ExchangeAdapter)


# ---------------------------------------------------------------------------
# 12. exchange property forwards to underlying
# ---------------------------------------------------------------------------

def test_exchange_property_forwards(guard, underlying):
    assert guard.exchange == underlying.exchange
    assert guard.exchange == Exchange.HYPERLIQUID
