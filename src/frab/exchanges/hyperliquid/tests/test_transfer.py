"""Unit tests for TransferAction."""
from __future__ import annotations

import logging
from datetime import datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.db.models import Base, Exchange as DBExchange, WalletSnapshot as DBWalletSnapshot
from frab.exchanges.hyperliquid.actions.transfer import TransferAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import (
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
        spot_token_map={"BTC": "UBTC"},
        spot_quote_token="USDC",
    )


def make_action(session_factory, mock_client, symbols, *, address="0xabc"):
    return TransferAction(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=lambda: _FIXED_DT,
    )


def _perp_state(account_value: float) -> HLPerpState:
    return HLPerpState(account_value=account_value, asset_positions=[])


def _spot_state(*entries: tuple[str, float, float]) -> HLSpotState:
    return HLSpotState(balances=[HLSpotBalance(coin=c, total=t, hold=h) for c, t, h in entries])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_raises_on_negative_amount(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    with pytest.raises(ValueError, match="amount must be positive, got -1.0"):
        await action.execute("USDC", -1.0, WalletKind.SPOT, WalletKind.PERP)


async def test_raises_on_zero_amount(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    with pytest.raises(ValueError, match="amount must be positive, got 0.0"):
        await action.execute("USDC", 0.0, WalletKind.SPOT, WalletKind.PERP)


async def test_spot_to_perp_calls_usd_class_transfer_with_true(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", 100.0, WalletKind.SPOT, WalletKind.PERP)

    mock_client.usd_class_transfer.assert_called_once_with(100.0, True)


async def test_perp_to_spot_calls_usd_class_transfer_with_false(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", 50.0, WalletKind.PERP, WalletKind.SPOT)

    mock_client.usd_class_transfer.assert_called_once_with(50.0, False)


async def test_spot_to_spot_raises_unsupported(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    with pytest.raises(ValueError, match="unsupported transfer direction"):
        await action.execute("USDC", 10.0, WalletKind.SPOT, WalletKind.SPOT)


async def test_perp_to_perp_raises_unsupported(session_factory, mock_client, symbols):
    action = make_action(session_factory, mock_client, symbols)
    with pytest.raises(ValueError, match="unsupported transfer direction"):
        await action.execute("USDC", 10.0, WalletKind.PERP, WalletKind.PERP)


async def test_no_session_factory_skips_snapshot(mock_client, symbols):
    action = TransferAction(
        client=mock_client,
        symbols=symbols,
        session_factory=None,
        exchange_name="hyperliquid",
        address="0xabc",
        clock_fn=lambda: _FIXED_DT,
    )
    await action.execute("USDC", 10.0, WalletKind.SPOT, WalletKind.PERP)

    # usd_class_transfer still called
    mock_client.usd_class_transfer.assert_called_once_with(10.0, True)
    # user_state never called (no snapshot)
    mock_client.user_state.assert_not_called()


async def test_no_address_skips_snapshot(session_factory, mock_client, symbols):
    action = TransferAction(
        client=mock_client,
        symbols=symbols,
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=None,
        clock_fn=lambda: _FIXED_DT,
    )
    await action.execute("USDC", 10.0, WalletKind.SPOT, WalletKind.PERP)

    mock_client.usd_class_transfer.assert_called_once_with(10.0, True)
    mock_client.user_state.assert_not_called()

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()
    assert rows == []


async def test_happy_path_usdc_writes_snapshot_with_perp_plus_spot(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(800.0)
    mock_client.spot_user_state.return_value = _spot_state(("USDC", 200.0, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("USDC", 50.0, WalletKind.SPOT, WalletKind.PERP)

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "hl_account_total"
    assert row.balance == pytest.approx(1000.0)  # 800 + 200
    assert row.ts_ms == _FIXED_MS


async def test_happy_path_non_usdc_writes_snapshot_with_spot_only(session_factory, mock_client, symbols):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state(("UBTC", 1.5, 0.0))

    action = make_action(session_factory, mock_client, symbols)
    await action.execute("BTC", 0.5, WalletKind.SPOT, WalletKind.PERP)

    async with session_factory() as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()

    assert len(rows) == 1
    assert rows[0].balance == pytest.approx(1.5)


async def test_logs_transfer_info(session_factory, mock_client, symbols, caplog):
    mock_client.user_state.return_value = _perp_state(0.0)
    mock_client.spot_user_state.return_value = _spot_state()

    action = make_action(session_factory, mock_client, symbols)
    with caplog.at_level(logging.INFO):
        await action.execute("BTC", 1.0, WalletKind.SPOT, WalletKind.PERP)

    assert "transfer coin=BTC amount=1.0000" in caplog.text
    assert "ok" in caplog.text
