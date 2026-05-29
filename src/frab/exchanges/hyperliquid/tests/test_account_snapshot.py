"""Unit tests for AccountSnapshotAction."""
from __future__ import annotations

import logging

import pytest
from unittest.mock import AsyncMock

from frab.exchanges.hyperliquid.actions.account_snapshot import AccountSnapshotAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import (
    HLPerpAssetPosition,
    HLPerpState,
    HLSpotBalance,
    HLSpotState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock(spec=HLClient)
    client.user_state = AsyncMock()
    client.spot_user_state = AsyncMock()
    return client


@pytest.fixture()
def symbols(mock_client):
    sym = HLSymbols(
        client=mock_client,
        spot_token_map={"BTC": "UBTC", "ETH": "UETH"},
        spot_quote_token="USDC",
    )
    return sym


def make_action(mock_client, symbols, *, address="0xabc"):
    return AccountSnapshotAction(
        client=mock_client,
        symbols=symbols,
        address=address,
    )


def _empty_perp_state() -> HLPerpState:
    return HLPerpState(account_value=0.0, asset_positions=[])


def _empty_spot_state() -> HLSpotState:
    return HLSpotState(balances=[])


# ---------------------------------------------------------------------------
# 1. address=None raises RuntimeError for get_snapshot, get_wallet_state,
#    and get_perp_unrealized_by_coin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_snapshot_no_address_raises(mock_client, symbols):
    action = AccountSnapshotAction(client=mock_client, symbols=symbols, address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.get_snapshot()


@pytest.mark.asyncio
async def test_get_wallet_state_no_address_raises(mock_client, symbols):
    action = AccountSnapshotAction(client=mock_client, symbols=symbols, address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.get_wallet_state()


@pytest.mark.asyncio
async def test_get_perp_unrealized_no_address_raises(mock_client, symbols):
    action = AccountSnapshotAction(client=mock_client, symbols=symbols, address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.get_perp_unrealized_by_coin()


# ---------------------------------------------------------------------------
# 2. get_snapshot returns (HLPerpState, HLSpotState) tuple
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_snapshot_returns_typed_tuple(mock_client, symbols):
    perp = HLPerpState(
        account_value=1000.5,
        asset_positions=[
            HLPerpAssetPosition(coin="BTC", szi=0.5, unrealized_pnl=50.0, cum_funding_since_open=-3.0),
        ],
    )
    spot = HLSpotState(balances=[HLSpotBalance(coin="USDC", total=200.0, hold=0.0)])
    mock_client.user_state.return_value = perp
    mock_client.spot_user_state.return_value = spot

    action = make_action(mock_client, symbols)
    result = await action.get_snapshot()

    perp_out, spot_out = result
    assert isinstance(perp_out, HLPerpState)
    assert isinstance(spot_out, HLSpotState)
    assert perp_out.account_value == pytest.approx(1000.5)
    assert len(perp_out.asset_positions) == 1
    assert spot_out.balances[0].coin == "USDC"


# ---------------------------------------------------------------------------
# 3. get_snapshot calls user_state and spot_user_state exactly once each
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_snapshot_calls_both_endpoints_once(mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(mock_client, symbols)
    await action.get_snapshot()

    mock_client.user_state.assert_called_once_with("0xabc")
    mock_client.spot_user_state.assert_called_once_with("0xabc")


# ---------------------------------------------------------------------------
# 4. get_snapshot with empty positions/balances returns empty typed objects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_snapshot_empty_returns_empty_typed_objects(mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(mock_client, symbols)
    perp_out, spot_out = await action.get_snapshot()

    assert perp_out.account_value == pytest.approx(0.0)
    assert perp_out.asset_positions == []
    assert spot_out.balances == []


# ---------------------------------------------------------------------------
# 5. get_perp_unrealized_by_coin returns dict of coin → unrealized_pnl
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_perp_unrealized_by_coin_returns_dict(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(
        account_value=0.0,
        asset_positions=[
            HLPerpAssetPosition(coin="BTC", szi=1.0, unrealized_pnl=100.0, cum_funding_since_open=0.0),
            HLPerpAssetPosition(coin="ETH", szi=-2.0, unrealized_pnl=-50.0, cum_funding_since_open=0.0),
            HLPerpAssetPosition(coin="SOL", szi=10.0, unrealized_pnl=20.0, cum_funding_since_open=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_perp_unrealized_by_coin()

    assert result == {"BTC": 100.0, "ETH": -50.0, "SOL": 20.0}


# ---------------------------------------------------------------------------
# 6. get_perp_unrealized_by_coin skips positions with empty/falsy coin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_perp_unrealized_skips_empty_coin(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(
        account_value=0.0,
        asset_positions=[
            HLPerpAssetPosition(coin="BTC", szi=1.0, unrealized_pnl=100.0, cum_funding_since_open=0.0),
            HLPerpAssetPosition(coin="", szi=0.0, unrealized_pnl=0.0, cum_funding_since_open=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_perp_unrealized_by_coin()

    assert result == {"BTC": 100.0}
    assert "" not in result


# ---------------------------------------------------------------------------
# 7. get_perp_unrealized_by_coin swallows user_state exception, logs warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_perp_unrealized_swallows_exception(mock_client, symbols, caplog):
    mock_client.user_state.side_effect = RuntimeError("HL down")

    action = make_action(mock_client, symbols)
    with caplog.at_level(logging.WARNING):
        result = await action.get_perp_unrealized_by_coin()

    assert result == {}
    assert "user_state failed" in caplog.text


# ---------------------------------------------------------------------------
# 8. get_wallet_state happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_happy_path(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(
        account_value=1000.0,
        asset_positions=[
            HLPerpAssetPosition(coin="BTC", szi=0.1, unrealized_pnl=50.0, cum_funding_since_open=0.0),
        ],
    )
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[
            HLSpotBalance(coin="USDC", total=200.0, hold=0.0),
            HLSpotBalance(coin="UBTC", total=0.5, hold=0.0),
            HLSpotBalance(coin="UETH", total=10.0, hold=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state(mark_prices={"BTC": 60000.0, "ETH": 3000.0})

    assert result["perp_account_value"] == pytest.approx(1000.0)
    assert result["perp_unrealized_pnl"] == pytest.approx(50.0)
    assert result["usdc_spot"] == pytest.approx(200.0)

    spot_balances = {b["coin"]: b for b in result["spot_balances"]}
    assert spot_balances["BTC"]["qty"] == pytest.approx(0.5)
    assert spot_balances["BTC"]["mark"] == pytest.approx(60000.0)
    assert spot_balances["BTC"]["usd_value"] == pytest.approx(30000.0)
    assert spot_balances["ETH"]["qty"] == pytest.approx(10.0)
    assert spot_balances["ETH"]["mark"] == pytest.approx(3000.0)
    assert spot_balances["ETH"]["usd_value"] == pytest.approx(30000.0)

    assert result["total_usd"] == pytest.approx(1000.0 + 30000.0 + 30000.0 + 200.0)


# ---------------------------------------------------------------------------
# 9. get_wallet_state with no mark_prices → spot tokens get mark=0, usd_value=0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_no_mark_prices(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(
        account_value=500.0,
        asset_positions=[],
    )
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[
            HLSpotBalance(coin="UBTC", total=1.0, hold=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state()

    assert result["spot_balances"][0]["mark"] == pytest.approx(0.0)
    assert result["spot_balances"][0]["usd_value"] == pytest.approx(0.0)
    assert result["total_usd"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 10. get_wallet_state skips balances with total <= 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_skips_zero_balance(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(account_value=0.0, asset_positions=[])
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[
            HLSpotBalance(coin="UBTC", total=0.0, hold=0.0),
            HLSpotBalance(coin="UETH", total=-1.0, hold=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state()

    assert result["spot_balances"] == []
    assert result["usdc_spot"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 11. get_wallet_state: UBTC → canonical "BTC" via normalize_spot_coin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_canonical_coin_mapping(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(account_value=0.0, asset_positions=[])
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[HLSpotBalance(coin="UBTC", total=1.0, hold=0.0)],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state(mark_prices={"BTC": 50000.0})

    assert len(result["spot_balances"]) == 1
    assert result["spot_balances"][0]["coin"] == "BTC"
    assert result["spot_balances"][0]["usd_value"] == pytest.approx(50000.0)


# ---------------------------------------------------------------------------
# 12. get_wallet_state sums multiple USDC balance entries into usdc_spot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_sums_multiple_usdc_entries(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(account_value=0.0, asset_positions=[])
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[
            HLSpotBalance(coin="USDC", total=100.0, hold=0.0),
            HLSpotBalance(coin="USDC", total=50.0, hold=0.0),
        ],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state()

    assert result["usdc_spot"] == pytest.approx(150.0)
    assert result["spot_balances"] == []


# ---------------------------------------------------------------------------
# 13. get_wallet_state with USDC-only wallet: total_usd = perp_account_value + usdc_spot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_usdc_only_wallet(mock_client, symbols):
    mock_client.user_state.return_value = HLPerpState(account_value=1000.0, asset_positions=[])
    mock_client.spot_user_state.return_value = HLSpotState(
        balances=[HLSpotBalance(coin="USDC", total=500.0, hold=0.0)],
    )

    action = make_action(mock_client, symbols)
    result = await action.get_wallet_state()

    assert result["spot_balances"] == []
    assert result["usdc_spot"] == pytest.approx(500.0)
    assert result["total_usd"] == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# 14. get_wallet_state calls user_state and spot_user_state exactly once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_state_no_duplicate_roundtrips(mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()
    mock_client.spot_user_state.return_value = _empty_spot_state()

    action = make_action(mock_client, symbols)
    await action.get_wallet_state()

    mock_client.user_state.assert_called_once_with("0xabc")
    mock_client.spot_user_state.assert_called_once_with("0xabc")


# ---------------------------------------------------------------------------
# 15. get_perp_unrealized_by_coin calls user_state only once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_perp_unrealized_no_duplicate_roundtrips(mock_client, symbols):
    mock_client.user_state.return_value = _empty_perp_state()

    action = make_action(mock_client, symbols)
    await action.get_perp_unrealized_by_coin()

    mock_client.user_state.assert_called_once_with("0xabc")
