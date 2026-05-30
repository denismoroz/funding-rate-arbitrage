"""Unit tests for GetWalletAction."""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.db.models import Base, Exchange as DBExchange, WalletSnapshot as DBWalletSnapshot
from frab.exchanges.hyperliquid.actions._base import HLActionContext
from frab.exchanges.hyperliquid.actions.get_wallet import GetWalletAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import (
    HLPerpAssetPosition,
    HLPerpState,
    HLSpotBalance,
    HLSpotState,
)
from frab.exchanges.protocol import WalletKind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_FIXED_MS = int(_FIXED_DT.timestamp() * 1000)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(DBExchange(name="hyperliquid", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5))
        await s.commit()
    try:
        yield sf
    finally:
        await engine.dispose()


@pytest.fixture
def mock_client(mocker):
    return mocker.AsyncMock(spec=HLClient)


@pytest.fixture
def symbols(mock_client):
    return HLSymbols(
        client=mock_client,
        spot_token_map={"BTC": "UBTC", "ETH": "UETH"},
        spot_quote_token="USDC",
    )


def make_action(session_factory, mock_client, symbols, *, address="0xabc"):
    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=lambda: _FIXED_DT,
    )
    return GetWalletAction(ctx)


def _perp_state(account_value: float, *positions) -> HLPerpState:
    return HLPerpState(
        account_value=account_value,
        asset_positions=[
            HLPerpAssetPosition(coin=c, szi=0.0, unrealized_pnl=u, cum_funding_since_open=f)
            for c, u, f in positions
        ],
    )


def _spot_state(*entries: tuple[str, float, float]) -> HLSpotState:
    return HLSpotState(balances=[HLSpotBalance(coin=c, total=t, hold=h) for c, t, h in entries])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_raises_when_address_is_none(session_factory, mock_client, symbols):
    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=None,
        clock_fn=lambda: _FIXED_DT,
    )
    action = GetWalletAction(ctx)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.execute("USDC", WalletKind.PERP)


async def test_perp_usdc_returns_account_value(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(1234.5)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute("USDC", WalletKind.PERP)

    assert result == pytest.approx(1234.5)


async def test_perp_non_usdc_returns_zero(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(1000.0)
    mock_client.spot_user_state.return_value = _spot_state(("UBTC", 0.5, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute("BTC", WalletKind.PERP)

    assert result == 0.0


async def test_spot_usdc_walks_balances(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state(("USDC", 500.0, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute("USDC", WalletKind.SPOT)

    assert result == pytest.approx(500.0)


async def test_spot_matches_via_spot_token_map(session_factory, mock_client, symbols):
    # BTC maps to UBTC; balance entry uses UBTC
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state(("UBTC", 2.5, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute("BTC", WalletKind.SPOT)

    assert result == pytest.approx(2.5)


async def test_spot_no_match_returns_zero(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state(("UETH", 1.0, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    result = await action.execute("BTC", WalletKind.SPOT)

    assert result == 0.0


async def test_writes_wallet_snapshot_row(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(1000.0)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", WalletKind.PERP)

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "hl_account_total"
    assert row.ts_ms == _FIXED_MS
    assert row.coin == "USDC"


async def test_snapshot_balance_uses_compute_total_usdc_for_usdc(session_factory, mock_client, symbols):
    # account_value=1000, unrealized=50, cum_funding_since_open=-5 (received=+5)
    # spot total=200, hold=50
    # perp_standalone = 1000 - 50 - 50 - 5 = 895; total = 200 + 895 = 1095
    mock_client.user_state.return_value = _perp_state(1000.0, ("BTC", 50.0, -5.0))
    mock_client.spot_user_state.return_value = _spot_state(("USDC", 200.0, 50.0))

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", WalletKind.PERP)

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()

    assert rows[0].balance == pytest.approx(1095.0)


async def test_snapshot_balance_uses_non_usdc_total_for_btc(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state(("UBTC", 3.0, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("BTC", WalletKind.SPOT)

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()

    assert rows[0].balance == pytest.approx(3.0)


async def test_raises_when_exchange_not_in_db(mock_client, symbols):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    # No DBExchange row seeded

    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state()

    ctx = HLActionContext(
        client=mock_client,
        symbols=symbols,
        session_factory=sf,
        exchange_name="hyperliquid",
        address="0xabc",
        clock_fn=lambda: _FIXED_DT,
    )
    action = GetWalletAction(ctx)
    with pytest.raises(RuntimeError, match="run `frab seed` first"):
        await action.execute("USDC", WalletKind.PERP)

    await engine.dispose()


async def test_calls_user_state_and_spot_user_state_once(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", WalletKind.PERP)

    mock_client.user_state.assert_called_once_with("0xabc")
    mock_client.spot_user_state.assert_called_once_with("0xabc")
