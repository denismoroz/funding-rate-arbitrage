"""Tests for HLExchange write methods and Protocol conformance."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hyperliquid.utils import constants
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Fill as DBFill
from frab.db.models import FundingAccrual as DBFundingAccrual
from frab.db.models import Position as DBPosition
from frab.db.models import WalletSnapshot as DBWalletSnapshot
from frab.db.session import init_db, make_session_factory, session_scope
from frab.domain import Instrument, PositionStatus, Side
from frab.exchanges.hyperliquid.exchange import HLExchange as LiveHLExecutor, HLTransferError, PartialFillError
from frab.exchanges.protocol import Exchange, OpenRequest, WalletKind

_FIXED_DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
_CLOCK = lambda: _FIXED_DT  # noqa: E731


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    from sqlalchemy import event

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
async def seeded_session_factory(session_factory):
    """Session factory with the 'hyperliquid' exchange row seeded."""
    from sqlalchemy import select
    from frab.db.models import Exchange as DBExchange
    async with session_scope(session_factory) as s:
        existing = await s.scalar(select(DBExchange).where(DBExchange.name == "hyperliquid"))
        if existing is None:
            s.add(DBExchange(
                name="hyperliquid",
                funding_interval_h=1,
                spot_taker_bps=7.0,
                perp_taker_bps=3.5,
            ))
    return session_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filled_resp(qty: float = 0.5, px: float = 30000.0, fee: float = 0.1):
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{
            "filled": {"totalSz": str(qty), "avgPx": str(px), "oid": 12345, "fee": str(fee)}
        }]}},
    }


def _make_executor(mocker, *, spot_token_map=None, slippage=0.01, account_address="0x" + "b" * 40,
                   session_factory=None):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    return LiveHLExecutor(
        info=info,
        exchange=exchange,
        account_address=account_address,
        spot_token_map=spot_token_map,
        slippage=slippage,
        clock_fn=_CLOCK,
        session_factory=session_factory,
    ), info, exchange


# ---------------------------------------------------------------------------
# 1. Protocol structural conformance
# ---------------------------------------------------------------------------

def test_hl_exchange_satisfies_protocol(mocker):
    """HLExchange must satisfy the runtime_checkable Exchange Protocol."""
    ex, _, _ = _make_executor(mocker)
    assert isinstance(ex, Exchange)


# ---------------------------------------------------------------------------
# 2. Constructor builds Info for testnet
# ---------------------------------------------------------------------------

async def test_constructor_builds_info_for_testnet_when_not_injected(mocker):
    mock_info_cls = mocker.patch("frab.exchanges.hyperliquid.exchange.Info")
    mock_exchange_cls = mocker.patch("frab.exchanges.hyperliquid.exchange.Exchange")
    mocker.patch("frab.exchanges.hyperliquid.exchange.Account")

    LiveHLExecutor(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="testnet",
    )

    mock_info_cls.assert_called_once_with(constants.TESTNET_API_URL, skip_ws=True)
    call_kwargs = mock_exchange_cls.call_args.kwargs
    assert call_kwargs["base_url"] == constants.TESTNET_API_URL


# ---------------------------------------------------------------------------
# 3. Constructor uses mainnet URL
# ---------------------------------------------------------------------------

async def test_constructor_uses_mainnet_url(mocker):
    mock_info_cls = mocker.patch("frab.exchanges.hyperliquid.exchange.Info")
    mock_exchange_cls = mocker.patch("frab.exchanges.hyperliquid.exchange.Exchange")
    mocker.patch("frab.exchanges.hyperliquid.exchange.Account")

    LiveHLExecutor(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="mainnet",
    )

    mock_info_cls.assert_called_once_with(constants.MAINNET_API_URL, skip_ws=True)
    call_kwargs = mock_exchange_cls.call_args.kwargs
    assert call_kwargs["base_url"] == constants.MAINNET_API_URL


# ---------------------------------------------------------------------------
# 4. open_position PERP writes Position + Fill to DB
# ---------------------------------------------------------------------------

async def test_open_position_perp_writes_db(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=30000.0, fee=0.1)))

    req = OpenRequest(
        coin="BTC",
        instrument=Instrument.PERP,
        side=Side.SHORT,
        qty=0.5,
    )
    pos = await ex.open_position(req)

    assert pos.id is not None
    assert pos.coin == "BTC"
    assert pos.instrument == Instrument.PERP
    assert pos.side == Side.SHORT
    assert pos.qty == pytest.approx(0.5)
    assert pos.entry_price == pytest.approx(30000.0)
    assert pos.status == PositionStatus.OPEN
    assert pos.exchange_name == "hyperliquid"

    # Verify DB has the row
    from sqlalchemy import select
    async with session_scope(seeded_session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos is not None
        assert db_pos.qty == pytest.approx(0.5)
        assert db_pos.entry_price == pytest.approx(30000.0)

        fills_result = await s.execute(
            select(DBFill).where(DBFill.position_id == pos.id)
        )
        fills = fills_result.scalars().all()
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(30000.0)
        assert fills[0].fee == pytest.approx(0.1)
        assert fills[0].is_paper is False


# ---------------------------------------------------------------------------
# 5. open_position SPOT writes Position + Fill to DB
# ---------------------------------------------------------------------------

async def test_open_position_spot_writes_db(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.001, px=80000.0, fee=0.0)))

    req = OpenRequest(
        coin="BTC",
        instrument=Instrument.SPOT,
        side=Side.LONG,
        qty=0.001,
    )
    pos = await ex.open_position(req)

    assert pos.instrument == Instrument.SPOT
    assert pos.side == Side.LONG
    assert pos.qty == pytest.approx(0.001)
    assert pos.entry_price == pytest.approx(80000.0)


# ---------------------------------------------------------------------------
# 6. open_position HL order rejected raises RuntimeError
# ---------------------------------------------------------------------------

async def test_open_position_rejected_raises(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(
        return_value={"status": "err", "response": "insufficient margin"}
    ))

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    with pytest.raises(RuntimeError, match="HL order rejected"):
        await ex.open_position(req)


# ---------------------------------------------------------------------------
# 7. open_position inner error status raises
# ---------------------------------------------------------------------------

async def test_open_position_inner_error_raises(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    resp = {"status": "ok", "response": {"data": {"statuses": [{"error": "min size"}]}}}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=resp))

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    with pytest.raises(RuntimeError, match="min size"):
        await ex.open_position(req)


# ---------------------------------------------------------------------------
# 8. open_position partial fill raises PartialFillError
# ---------------------------------------------------------------------------

async def test_open_position_partial_fill_raises(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    # Requested 0.5, filled 0.05 — way below 1% tolerance
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.05, px=24.0, fee=0.0)))

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    with pytest.raises(PartialFillError) as exc_info:
        await ex.open_position(req)
    err = exc_info.value
    assert err.requested_qty == 0.5
    assert err.filled_qty == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# 9. open_position PERP uses bare coin name
# ---------------------------------------------------------------------------

async def test_open_position_perp_uses_bare_coin_name(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    await ex.open_position(req)

    call_args = mock_to_thread.call_args
    assert call_args.args[1] == "BTC"


# ---------------------------------------------------------------------------
# 10. open_position SPOT with token map uses pair name
# ---------------------------------------------------------------------------

async def test_open_position_spot_uses_pair_name(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(
        mocker,
        spot_token_map={"BTC": "UBTC"},
        session_factory=seeded_session_factory,
    )
    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = OpenRequest(coin="BTC", instrument=Instrument.SPOT, side=Side.LONG, qty=0.001)
    await ex.open_position(req)

    call_args = mock_to_thread.call_args
    assert call_args.args[1] == "UBTC/USDC"


# ---------------------------------------------------------------------------
# 11. close_position PERP updates DB status
# ---------------------------------------------------------------------------

async def test_close_position_perp_updates_db(mocker, seeded_session_factory):
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=31000.0, fee=0.05)))

    # First open a position
    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    pos = await ex.open_position(req)

    # Now close it
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=29000.0, fee=0.04)))
    closed_pos = await ex.close_position(pos)

    assert closed_pos.status == PositionStatus.CLOSED
    assert closed_pos.closed_at is not None

    # Verify DB
    async with session_scope(seeded_session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos.status == PositionStatus.CLOSED.value
        assert db_pos.closed_at is not None


# ---------------------------------------------------------------------------
# 12. transfer spot→perp happy path
# ---------------------------------------------------------------------------

def _ok_transfer_resp():
    return {"status": "ok", "response": {"type": "default"}}


async def test_transfer_spot_to_perp_happy_path(mocker):
    ex, _, exchange = _make_executor(mocker)
    mock_to_thread = mocker.patch(
        "asyncio.to_thread",
        new=mocker.AsyncMock(return_value=_ok_transfer_resp()),
    )

    await ex.transfer("USDC", 100.0, WalletKind.SPOT, WalletKind.PERP)

    mock_to_thread.assert_called_once()
    call = mock_to_thread.call_args
    assert call.args[0] is exchange.usd_class_transfer
    assert call.args[1] == pytest.approx(100.0)
    assert call.args[2] is True  # to_perp=True


# ---------------------------------------------------------------------------
# 13. transfer perp→spot happy path
# ---------------------------------------------------------------------------

async def test_transfer_perp_to_spot_happy_path(mocker):
    ex, _, exchange = _make_executor(mocker)
    mock_to_thread = mocker.patch(
        "asyncio.to_thread",
        new=mocker.AsyncMock(return_value=_ok_transfer_resp()),
    )

    await ex.transfer("USDC", 250.0, WalletKind.PERP, WalletKind.SPOT)

    mock_to_thread.assert_called_once()
    call = mock_to_thread.call_args
    assert call.args[0] is exchange.usd_class_transfer
    assert call.args[1] == pytest.approx(250.0)
    assert call.args[2] is False  # to_perp=False


# ---------------------------------------------------------------------------
# 14. transfer HL error → HLTransferError
# ---------------------------------------------------------------------------

async def test_transfer_hl_error_raises(mocker):
    ex, _, exchange = _make_executor(mocker)
    error_resp = {"status": "err", "response": "insufficient balance"}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=error_resp))

    with pytest.raises(HLTransferError, match="insufficient balance"):
        await ex.transfer("USDC", 50.0, WalletKind.SPOT, WalletKind.PERP)


# ---------------------------------------------------------------------------
# 15. transfer non-positive amount → ValueError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_amount", [0, -1.0, -100.0])
async def test_transfer_non_positive_raises(mocker, bad_amount):
    ex, _, _ = _make_executor(mocker)
    with pytest.raises(ValueError):
        await ex.transfer("USDC", bad_amount, WalletKind.SPOT, WalletKind.PERP)


# ---------------------------------------------------------------------------
# 16. round_qty floors at szDecimals
# ---------------------------------------------------------------------------

async def test_round_qty_floors_at_sz_decimals(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
    ]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    assert await ex.round_qty("BTC", 0.000149895) == pytest.approx(0.00014, abs=1e-9)
    assert await ex.round_qty("BTC", 0.00014) == pytest.approx(0.00014, abs=1e-9)
    assert await ex.round_qty("ETH", 0.00333333) == pytest.approx(0.0033, abs=1e-9)


# ---------------------------------------------------------------------------
# 17. round_qty_to_nearest uses HALF_UP
# ---------------------------------------------------------------------------

async def test_round_qty_to_nearest_uses_half_up(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
    ]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    assert await ex.round_qty_to_nearest("BTC", 0.000149895) == pytest.approx(0.00015, abs=1e-9)
    assert await ex.round_qty_to_nearest("BTC", 0.000145) == pytest.approx(0.00015, abs=1e-9)
    assert await ex.round_qty_to_nearest("BTC", 0.000144) == pytest.approx(0.00014, abs=1e-9)
    assert await ex.round_qty_to_nearest("ETH", 0.00335) == pytest.approx(0.0034, abs=1e-9)


# ---------------------------------------------------------------------------
# 18. round_qty raises ValueError on unknown coin
# ---------------------------------------------------------------------------

async def test_round_qty_raises_on_unknown_coin(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    with pytest.raises(ValueError, match="unknown coin"):
        await ex.round_qty("DOGE", 1.0)
    with pytest.raises(ValueError, match="unknown coin"):
        await ex.round_qty_to_nearest("DOGE", 1.0)


# ---------------------------------------------------------------------------
# 19. fetch_account_state returns combined dict
# ---------------------------------------------------------------------------

async def test_fetch_account_state_returns_combined(mocker):
    ex, info, _ = _make_executor(mocker)

    perp_data = {"assetPositions": [], "crossMarginSummary": {}}
    spot_data = {"balances": []}

    async def fake_gather(*coros):
        return (perp_data, spot_data)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    result = await ex.fetch_account_state()
    assert result == {"perp": perp_data, "spot": spot_data}


# ---------------------------------------------------------------------------
# 20. fetch_wallet_state normalizes UBTC→BTC
# ---------------------------------------------------------------------------

async def test_fetch_wallet_state_normalizes_spot_coins(mocker):
    ex, info, _ = _make_executor(mocker, spot_token_map={"BTC": "UBTC", "ETH": "UETH"})

    perp_state = {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [
            {"position": {"coin": "BTC", "unrealizedPnl": "-50.0"}},
        ],
    }
    spot_state = {
        "balances": [
            {"coin": "UBTC", "total": "0.001"},
            {"coin": "UETH", "total": "0.5"},
            {"coin": "USDC", "total": "200.0"},
        ]
    }

    async def fake_gather(*coros):
        return (perp_state, spot_state)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    mark_prices = {"BTC": 95_000.0, "ETH": 2_000.0}
    result = await ex.fetch_wallet_state(mark_prices=mark_prices)

    assert result["perp_account_value"] == pytest.approx(1_000.0)
    assert result["perp_unrealized_pnl"] == pytest.approx(-50.0)
    coins_in_balances = {b["coin"] for b in result["spot_balances"]}
    assert coins_in_balances == {"BTC", "ETH"}
    assert result["usdc_spot"] == pytest.approx(200.0)
    assert result["total_usd"] == pytest.approx(2_295.0)


# ---------------------------------------------------------------------------
# 21. fetch_wallet_state without account_address raises
# ---------------------------------------------------------------------------

async def test_fetch_wallet_state_without_address_raises(mocker):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    ex = LiveHLExecutor(info=info, exchange=exchange, account_address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await ex.fetch_wallet_state()


# ---------------------------------------------------------------------------
# 22. open_position requires session_factory
# ---------------------------------------------------------------------------

async def test_open_position_requires_session_factory(mocker):
    """open_position without session_factory raises RuntimeError."""
    ex, _, exchange = _make_executor(mocker, session_factory=None)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    with pytest.raises(RuntimeError, match="session_factory required"):
        await ex.open_position(req)


# ---------------------------------------------------------------------------
# 23. open_position COLLATERAL writes Position row, no Fill
# ---------------------------------------------------------------------------

async def test_open_position_collateral_writes_position_no_fill(mocker, seeded_session_factory):
    """COLLATERAL open: Position row written, no Fill row created."""
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)

    # transfer call needs to be mocked (COLLATERAL → calls self.transfer internally)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_ok_transfer_resp()))

    # Also patch get_wallet calls that transfer makes for wallet snapshots.
    # transfer checks self._address is not None; our executor has address set.
    # After transfer, it reads perp/spot state — patch info calls.
    ex._info.user_state.return_value = {"marginSummary": {"accountValue": "1000.0"}, "assetPositions": []}
    ex._info.spot_user_state.return_value = {"balances": []}

    req = OpenRequest(coin="USDC", instrument=Instrument.COLLATERAL, side=Side.NONE, qty=600.0)
    pos = await ex.open_position(req)

    assert pos.instrument == Instrument.COLLATERAL
    assert pos.side == Side.NONE
    assert pos.qty == pytest.approx(600.0)
    assert pos.entry_price == pytest.approx(1.0)
    assert pos.status == PositionStatus.OPEN
    assert pos.id is not None

    async with session_scope(seeded_session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos is not None
        assert db_pos.instrument == Instrument.COLLATERAL.value
        assert db_pos.qty == pytest.approx(600.0)

        fills = (await s.execute(select(DBFill).where(DBFill.position_id == pos.id))).scalars().all()
        assert len(fills) == 0, "COLLATERAL open must NOT write a Fill row"


# ---------------------------------------------------------------------------
# 24. open_position SPOT writes Position + Fill
# ---------------------------------------------------------------------------

async def test_open_position_spot_writes_position_and_fill(mocker, seeded_session_factory):
    """SPOT open: both Position row and Fill row are written to DB."""
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(
        return_value=_filled_resp(qty=0.001, px=80000.0, fee=5.6)
    ))

    req = OpenRequest(coin="BTC", instrument=Instrument.SPOT, side=Side.LONG, qty=0.001)
    pos = await ex.open_position(req)

    assert pos.instrument == Instrument.SPOT
    assert pos.id is not None

    async with session_scope(seeded_session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos is not None
        assert db_pos.instrument == Instrument.SPOT.value
        assert db_pos.qty == pytest.approx(0.001)

        fills = (await s.execute(select(DBFill).where(DBFill.position_id == pos.id))).scalars().all()
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(80000.0)
        assert fills[0].fee == pytest.approx(5.6)
        assert fills[0].side == Side.LONG.value


# ---------------------------------------------------------------------------
# 25. close_position writes closing Fill (PERP)
# ---------------------------------------------------------------------------

async def test_close_position_writes_closing_fill(mocker, seeded_session_factory):
    """close_position PERP: DB position updated to CLOSED + closing Fill inserted."""
    ex, _, exchange = _make_executor(mocker, session_factory=seeded_session_factory)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=30000.0, fee=0.1)))

    # Open first
    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    pos = await ex.open_position(req)

    # Now close
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=28000.0, fee=0.08)))
    closed = await ex.close_position(pos)

    assert closed.status == PositionStatus.CLOSED
    assert closed.closed_at is not None

    async with session_scope(seeded_session_factory) as s:
        db_pos = await s.get(DBPosition, pos.id)
        assert db_pos.status == PositionStatus.CLOSED.value
        assert db_pos.closed_at is not None

        fills = (await s.execute(select(DBFill).where(DBFill.position_id == pos.id))).scalars().all()
        # Opening fill + closing fill = 2
        assert len(fills) == 2
        closing_fill = next(f for f in fills if f.price == pytest.approx(28000.0))
        assert closing_fill.side == Side.LONG.value  # closing a SHORT → LONG fill
        assert closing_fill.fee == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# 26. get_wallet writes wallet_snapshots row, returns balance
# ---------------------------------------------------------------------------

async def test_get_wallet_writes_snapshot(mocker, seeded_session_factory):
    """get_wallet: inserts WalletSnapshot row; return value matches balance."""
    ex, info, _ = _make_executor(mocker, session_factory=seeded_session_factory)

    perp_state = {"marginSummary": {"accountValue": "2500.75"}, "assetPositions": []}
    spot_state = {"balances": [{"coin": "USDC", "total": "100.0"}]}

    async def _fake_gather(*coros):
        return (perp_state, spot_state)

    mocker.patch("asyncio.gather", side_effect=_fake_gather)

    balance = await ex.get_wallet("USDC", WalletKind.PERP)
    assert balance == pytest.approx(2500.75)

    async with session_scope(seeded_session_factory) as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()
    assert len(rows) == 1
    assert rows[0].balance == pytest.approx(2500.75)
    assert rows[0].coin == "USDC"
    assert rows[0].source == "hl_account_state"


# ---------------------------------------------------------------------------
# 27. transfer writes wallet_snapshot rows after success
# ---------------------------------------------------------------------------

async def test_transfer_writes_wallet_snapshots(mocker, seeded_session_factory):
    """transfer: after success writes perp+spot WalletSnapshot rows."""
    ex, info, exchange = _make_executor(mocker, session_factory=seeded_session_factory)

    transfer_resp = _ok_transfer_resp()
    perp_state = {"marginSummary": {"accountValue": "600.0"}, "assetPositions": []}
    spot_state = {"balances": [{"coin": "USDC", "total": "400.0"}]}

    call_count = {"n": 0}

    async def _fake_to_thread(fn, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # The usd_class_transfer call
            return transfer_resp
        # Subsequent calls are info.user_state / info.spot_user_state
        return fn(*args, **kwargs)

    # asyncio.gather is used inside transfer for post-transfer balance fetch
    async def _fake_gather(*coros):
        return (perp_state, spot_state)

    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=transfer_resp))
    mocker.patch("asyncio.gather", side_effect=_fake_gather)

    await ex.transfer("USDC", 100.0, WalletKind.SPOT, WalletKind.PERP)

    async with session_scope(seeded_session_factory) as s:
        rows = (await s.execute(select(DBWalletSnapshot))).scalars().all()
    assert len(rows) == 2
    sources = {r.source for r in rows}
    assert "hl_post_transfer_perp" in sources
    assert "hl_post_transfer_spot" in sources
    perp_row = next(r for r in rows if r.source == "hl_post_transfer_perp")
    spot_row = next(r for r in rows if r.source == "hl_post_transfer_spot")
    assert perp_row.balance == pytest.approx(600.0)
    assert spot_row.balance == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# 28. get_accrued_funding inserts funding_accruals and is idempotent
# ---------------------------------------------------------------------------

async def test_get_accrued_funding_writes_and_is_idempotent(mocker, seeded_session_factory):
    """get_accrued_funding: inserts FundingAccrual rows idempotently."""
    ex, info, _ = _make_executor(mocker, session_factory=seeded_session_factory)

    # Seed a PERP position in DB first
    async with session_scope(seeded_session_factory) as s:
        from frab.db.models import Exchange as DBExchange
        exc_row = (await s.execute(select(DBExchange).where(DBExchange.name == "hyperliquid"))).scalar_one()
        pos_row = DBPosition(
            exchange_id=exc_row.id,
            coin="BTC",
            instrument=Instrument.PERP.value,
            side=Side.SHORT.value,
            qty=0.5,
            entry_price=30000.0,
            opened_at=int(_FIXED_DT.timestamp() * 1000),
            closed_at=None,
            status=PositionStatus.OPEN.value,
            farb_position_id=None,
        )
        s.add(pos_row)
        await s.flush()
        pos_id = pos_row.id

    from frab.domain.position import Position as DomainPosition
    domain_pos = DomainPosition(
        id=pos_id,
        exchange_name="hyperliquid",
        coin="BTC",
        instrument=Instrument.PERP,
        side=Side.SHORT,
        qty=0.5,
        entry_price=30000.0,
        opened_at=_FIXED_DT,
        closed_at=None,
        status=PositionStatus.OPEN,
        farb_position_id=None,
    )

    # Mock HL API response: 2 funding accrual records
    base_ms = int(_FIXED_DT.timestamp() * 1000)
    funding_data = [
        {"time": str(base_ms + 3600_000), "delta": {"coin": "BTC", "usdc": "1.5", "type": "funding"}},
        {"time": str(base_ms + 7200_000), "delta": {"coin": "BTC", "usdc": "2.0", "type": "funding"}},
        {"time": str(base_ms + 7200_000), "delta": {"coin": "ETH", "usdc": "0.5", "type": "funding"}},  # different coin — skip
    ]

    async def _fake_post(body):
        if body.get("type") == "userFunding":
            return funding_data
        return {}

    ex._post = _fake_post  # type: ignore[assignment]

    total = await ex.get_accrued_funding(domain_pos)
    assert total == pytest.approx(3.5)

    async with session_scope(seeded_session_factory) as s:
        rows = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()
    assert len(rows) == 2

    # Call again — idempotent: no duplicate rows
    total2 = await ex.get_accrued_funding(domain_pos)
    assert total2 == pytest.approx(3.5)

    async with session_scope(seeded_session_factory) as s:
        rows2 = (await s.execute(
            select(DBFundingAccrual).where(DBFundingAccrual.position_id == pos_id)
        )).scalars().all()
    assert len(rows2) == 2, "Second call must NOT duplicate accrual rows"
