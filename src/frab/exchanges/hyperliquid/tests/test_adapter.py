"""Tests for HyperliquidAdapter (F2.4).

All underlying components (HLExchangeReader, LiveHLExecutor, AtomicExecutor)
are replaced with MagicMock/AsyncMock — no network calls.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from frab.domain.exchange import Exchange
from frab.domain.position import ClosedPosition, Position
from frab.domain.wallet import WalletInfo
from frab.exchanges.adapter import ExchangeAdapter
from frab.exchanges.base import (
    FillReport,
    Leg,
    MarketSpec as BaseMarketSpec,
    PositionState,
    Side,
)
from frab.exchanges.hyperliquid.adapter import HyperliquidAdapter
from frab.exchanges._paired_results import PairedCloseResult, PairedOpenResult


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter(mocker):
    market = mocker.MagicMock()
    market.aclose = mocker.AsyncMock()
    market.fetch_quote = mocker.AsyncMock()
    market.fetch_funding = mocker.AsyncMock()
    market.fetch_funding_history = mocker.AsyncMock()
    market.fetch_user_fills = mocker.AsyncMock()
    market.fetch_user_funding = mocker.AsyncMock()
    market.fetch_meta = mocker.AsyncMock(return_value=[])
    market._post = mocker.AsyncMock()

    live = mocker.MagicMock()
    live.transfer_spot_to_perp = mocker.AsyncMock()
    live.transfer_perp_to_spot = mocker.AsyncMock()
    live.fetch_wallet_state = mocker.AsyncMock(return_value={
        "withdrawable": 500.0,
        "perp_equity": 200.0,
        "account_value": 700.0,
    })
    live.get_position = mocker.AsyncMock()

    atomic = mocker.MagicMock()
    atomic.open_paired = mocker.AsyncMock()
    atomic.close_paired = mocker.AsyncMock()

    return HyperliquidAdapter(
        market_data=market,
        live_executor=live,
        atomic=atomic,
        network="testnet",
        user_address="0xDEADBEEF",
    )


# ---------------------------------------------------------------------------
# 1. Protocol conformance
# ---------------------------------------------------------------------------

def test_satisfies_protocol(adapter):
    assert isinstance(adapter, ExchangeAdapter)


# ---------------------------------------------------------------------------
# 2. Exchange attribute
# ---------------------------------------------------------------------------

def test_exchange_attribute(adapter):
    assert adapter.exchange == Exchange.HYPERLIQUID


# ---------------------------------------------------------------------------
# 3. Exchange profile constants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_exchange_profile_returns_hl_constants(adapter):
    profile = await adapter.get_exchange_profile()
    assert profile.funding_interval_hours == 1.0
    assert profile.default_spot_taker_bps == 7.0
    assert profile.default_perp_taker_bps == 3.5
    assert profile.periods_per_year == 24 * 365
    assert profile.exchange == Exchange.HYPERLIQUID


# ---------------------------------------------------------------------------
# 4. Read methods delegate to underlying components
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_methods_delegate(adapter):
    # fetch_quote
    await adapter.fetch_quote("BTC")
    adapter._market.fetch_quote.assert_awaited_once_with("BTC")

    # fetch_funding
    await adapter.fetch_funding("ETH")
    adapter._market.fetch_funding.assert_awaited_once_with("ETH")

    # fetch_funding_history
    await adapter.fetch_funding_history("SOL", 1_000_000)
    adapter._market.fetch_funding_history.assert_awaited_once_with("SOL", 1_000_000)

    # fetch_user_fills — adapter injects stored user_address
    await adapter.fetch_user_fills(9999)
    adapter._market.fetch_user_fills.assert_awaited_once_with("0xDEADBEEF", 9999)

    # fetch_user_funding
    await adapter.fetch_user_funding(8888)
    adapter._market.fetch_user_funding.assert_awaited_once_with("0xDEADBEEF", 8888)


# ---------------------------------------------------------------------------
# 5. get_wallet normalizes fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_normalizes(adapter):
    wallet = await adapter.get_wallet()
    assert isinstance(wallet, WalletInfo)
    assert wallet.exchange == Exchange.HYPERLIQUID
    assert wallet.available_usdc == 500.0
    assert wallet.reserved_usdc == 200.0
    assert wallet.total_value_usd == 700.0


# ---------------------------------------------------------------------------
# 6. get_open_positions baseline — empty list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_open_positions_baseline_empty(adapter):
    positions = await adapter.get_open_positions()
    assert positions == []


# ---------------------------------------------------------------------------
# 7. get_market_specs builds domain MarketSpec with defaults
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_market_specs_builds_domain_specs(adapter):
    base = BaseMarketSpec(
        coin="BTC",
        has_spot=False,
        has_perp=True,
        min_size=0.0001,
        tick_size=0.1,
    )
    adapter._market.fetch_meta.return_value = [base]

    specs = await adapter.get_market_specs()

    assert "BTC" in specs
    domain_spec = specs["BTC"]
    assert domain_spec.coin == "BTC"
    assert domain_spec.has_perp is True
    assert domain_spec.max_leverage == 10
    assert domain_spec.maint_ratio == 0.01
    assert domain_spec.min_size == 0.0001
    assert domain_spec.tick_size == 0.1


# ---------------------------------------------------------------------------
# 8. open_position happy path
# ---------------------------------------------------------------------------

def _make_fill(*, coin: str, leg: Leg, side: Side, price: float, qty: float, fee: float) -> FillReport:
    return FillReport(
        coin=coin,
        leg=leg,
        side=side,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        qty=qty,
        price=price,
        fee=fee,
        slippage_bps=10.0,
        client_ref=None,
    )


@pytest.mark.asyncio
async def test_open_position_happy_path(adapter, mocker):
    from frab.exchanges.base import Quote

    adapter._market.fetch_quote.return_value = Quote(
        coin="BTC",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        bid=99_900.0,
        ask=100_000.0,
        mark=99_950.0,
        spot=None,
    )
    perp_fill = _make_fill(coin="BTC", leg=Leg.PERP, side=Side.SELL, price=99_950.0, qty=0.001, fee=0.35)
    spot_fill = _make_fill(coin="BTC", leg=Leg.SPOT, side=Side.BUY, price=100_000.0, qty=0.001, fee=0.70)
    adapter._atomic.open_paired.return_value = PairedOpenResult(
        status="ok",
        perp_fill=perp_fill,
        spot_fill=spot_fill,
        perp_attempts=1,
        spot_attempts=1,
        errors=(),
    )

    pos = await adapter.open_position(
        "BTC",
        notional_usd=100.0,
        margin_reserve_usd=20.0,
        client_ref="test-ref",
    )

    # margin was transferred first
    adapter._live.transfer_spot_to_perp.assert_awaited_once_with(20.0)

    assert isinstance(pos, Position)
    assert pos.exchange == Exchange.HYPERLIQUID
    assert pos.coin == "BTC"
    assert pos.spot_qty == 0.001
    assert pos.perp_qty == 0.001
    assert pos.notional_usd == 100.0
    assert pos.margin_reserve_usd == 20.0
    assert pos.entry_spot_price == 100_000.0
    assert pos.entry_perp_price == 99_950.0
    assert pos.fees_paid == pytest.approx(0.35 + 0.70)


# ---------------------------------------------------------------------------
# 9. open_position failure rolls back margin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_position_failure_rolls_back_margin(adapter):
    from frab.exchanges.base import Quote

    adapter._market.fetch_quote.return_value = Quote(
        coin="ETH",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        bid=3_000.0,
        ask=3_001.0,
        mark=3_000.5,
        spot=None,
    )
    adapter._atomic.open_paired.return_value = PairedOpenResult(
        status="failed",
        perp_fill=None,
        spot_fill=None,
        perp_attempts=3,
        spot_attempts=3,
        errors=("timeout",),
    )

    with pytest.raises(RuntimeError, match="open_position failed"):
        await adapter.open_position("ETH", notional_usd=50.0, margin_reserve_usd=10.0)

    adapter._live.transfer_spot_to_perp.assert_awaited_once_with(10.0)
    adapter._live.transfer_perp_to_spot.assert_awaited_once_with(10.0)


# ---------------------------------------------------------------------------
# 10. close_position happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_happy_path(adapter):
    venue_pos = PositionState(
        coin="BTC",
        spot_units=0.001,
        perp_units=-0.001,
        avg_entry_spot=100_000.0,
        avg_entry_perp=99_950.0,
    )
    adapter._live.get_position.return_value = venue_pos

    perp_fill = _make_fill(coin="BTC", leg=Leg.PERP, side=Side.BUY, price=98_000.0, qty=0.001, fee=0.34)
    spot_fill = _make_fill(coin="BTC", leg=Leg.SPOT, side=Side.SELL, price=97_900.0, qty=0.001, fee=0.69)
    adapter._atomic.close_paired.return_value = PairedCloseResult(
        status="ok",
        perp_fill=perp_fill,
        spot_fill=spot_fill,
        perp_attempts=1,
        spot_attempts=1,
        errors=(),
    )

    closed = await adapter.close_position("BTC")

    assert isinstance(closed, ClosedPosition)
    assert closed.exchange == Exchange.HYPERLIQUID
    assert closed.coin == "BTC"

    # perp realized: (entry_perp - exit_perp) * abs(perp_units)
    # = (99950 - 98000) * 0.001 = 1.95
    # spot realized: (exit_spot - entry_spot) * spot_units
    # = (97900 - 100000) * 0.001 = -2.1
    # total = -0.15
    expected_pnl = (99_950.0 - 98_000.0) * 0.001 + (97_900.0 - 100_000.0) * 0.001
    assert closed.realized_pnl == pytest.approx(expected_pnl)
    assert closed.fees_paid_total == pytest.approx(0.34 + 0.69)


# ---------------------------------------------------------------------------
# 11. close_position raises when no venue position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_raises_when_no_venue_position(adapter):
    adapter._live.get_position.return_value = None

    with pytest.raises(RuntimeError, match="no venue position"):
        await adapter.close_position("BTC")


# ---------------------------------------------------------------------------
# 12. adjust_margin positive calls transfer_spot_to_perp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_margin_positive_calls_transfer_spot_to_perp(adapter):
    await adapter.adjust_margin("BTC", 50.0)
    adapter._live.transfer_spot_to_perp.assert_awaited_once_with(50.0)
    adapter._live.transfer_perp_to_spot.assert_not_awaited()


# ---------------------------------------------------------------------------
# 13. adjust_margin negative calls transfer_perp_to_spot with abs(delta)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_margin_negative_calls_transfer_perp_to_spot(adapter):
    await adapter.adjust_margin("BTC", -30.0)
    adapter._live.transfer_perp_to_spot.assert_awaited_once_with(30.0)
    adapter._live.transfer_spot_to_perp.assert_not_awaited()


# ---------------------------------------------------------------------------
# 14. adjust_margin zero is a no-op
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adjust_margin_zero_is_noop(adapter):
    await adapter.adjust_margin("BTC", 0.0)
    adapter._live.transfer_spot_to_perp.assert_not_awaited()
    adapter._live.transfer_perp_to_spot.assert_not_awaited()


# ---------------------------------------------------------------------------
# 15. startup_validate mainnet calls validate_spot_pairs (via market._post)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_validate_mainnet_calls_validate_spot_pairs(mocker):
    market = mocker.MagicMock()
    market.aclose = mocker.AsyncMock()
    market.fetch_meta = mocker.AsyncMock(return_value=[])
    # Return a valid spotMeta payload so validate_spot_pairs passes
    market._post = mocker.AsyncMock(return_value={
        "tokens": [
            {"index": 0, "name": "USDC"},
            {"index": 1, "name": "UBTC"},
        ],
        "universe": [
            {"name": "UBTC/USDC", "tokens": [1, 0]},
        ],
    })

    live = mocker.MagicMock()
    live.fetch_wallet_state = mocker.AsyncMock(return_value={})
    live.transfer_spot_to_perp = mocker.AsyncMock()
    live.transfer_perp_to_spot = mocker.AsyncMock()

    atomic = mocker.MagicMock()

    mainnet_adapter = HyperliquidAdapter(
        market_data=market,
        live_executor=live,
        atomic=atomic,
        network="mainnet",
        user_address="0xABC",
    )

    await mainnet_adapter.startup_validate(("BTC",))

    market._post.assert_awaited_once()
    call_args = market._post.call_args[0][0]
    assert call_args.get("type") == "spotMeta"


# ---------------------------------------------------------------------------
# 16. startup_validate testnet skips validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_validate_testnet_skips(adapter):
    await adapter.startup_validate(("BTC", "ETH"))
    adapter._market._post.assert_not_awaited()


# ---------------------------------------------------------------------------
# 17. close calls market.aclose
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_calls_aclose(adapter):
    await adapter.close()
    adapter._market.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# 18. paired_router exposes AtomicExecutor
# ---------------------------------------------------------------------------

def test_paired_router_returns_atomic(adapter):
    assert adapter.paired_router is adapter._atomic


# ---------------------------------------------------------------------------
# 19. user_address resolved from live_executor.account_address if present
# ---------------------------------------------------------------------------

def test_user_address_from_live_executor_attribute(mocker):
    market = mocker.MagicMock()
    live = mocker.MagicMock()
    live.account_address = "0xLIVEADDR"
    atomic = mocker.MagicMock()

    a = HyperliquidAdapter(
        market_data=market,
        live_executor=live,
        atomic=atomic,
        network="testnet",
    )
    assert a._user_address == "0xLIVEADDR"


# ---------------------------------------------------------------------------
# 20. user_address kwarg used as fallback when live has no attribute
# ---------------------------------------------------------------------------

def test_user_address_kwarg_fallback(mocker):
    market = mocker.MagicMock()
    live = mocker.MagicMock(spec=[])  # no account_address attribute
    atomic = mocker.MagicMock()

    a = HyperliquidAdapter(
        market_data=market,
        live_executor=live,
        atomic=atomic,
        network="testnet",
        user_address="0xFALLBACK",
    )
    assert a._user_address == "0xFALLBACK"
