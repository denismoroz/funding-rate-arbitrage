"""Unit tests for BackfillFeesAction."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from frab.db.models import (
    Base,
    Exchange as DBExchange,
    FarbPosition as DBFarbPosition,
    Fill as DBFill,
    Position as DBPosition,
    Strategy as DBStrategy,
)
from frab.domain import Instrument, PositionStatus, Side
from frab.exchanges.hyperliquid.actions._base import HLActionContext
from frab.exchanges.hyperliquid.actions.backfill_fees import BackfillFeesAction
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.symbols import HLSymbols
from frab.exchanges.hyperliquid.wire import HLUserFill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_TS_MS = 1_700_000_000_000  # arbitrary epoch ms


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


def _make_symbols(mock_client):
    return HLSymbols(client=mock_client, spot_token_map={}, spot_quote_token="USDC")


def make_action(session_factory, mock_client, *, address="0xabc"):
    ctx = HLActionContext(
        client=mock_client,
        symbols=_make_symbols(mock_client),
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=address,
        clock_fn=lambda: datetime.now(UTC),
    )
    return BackfillFeesAction(ctx)


async def _seed_db(
    sf,
    *,
    coin: str = "BTC",
    instrument: Instrument = Instrument.PERP,
    side: Side = Side.LONG,
    qty: float = 1.0,
    fee: float = 0.0,
    ts_ms: int = _FIXED_TS_MS,
) -> tuple[int, int, int]:
    """Seed strategy/farb_position/position/fill rows. Returns (strategy_id, position_id, fill_id)."""
    async with sf() as s:
        exc_id = await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid"))

        strategy = DBStrategy(name="test", version="1", params_json={}, status="idle")
        s.add(strategy)
        await s.flush()

        farb = DBFarbPosition(
            strategy_id=strategy.id,
            coin=coin,
            state="open",
            state_data={},
            opened_at=ts_ms - 3_600_000,
        )
        s.add(farb)
        await s.flush()

        pos = DBPosition(
            exchange_id=exc_id,
            coin=coin,
            instrument=instrument.value,
            side=side.value,
            qty=qty,
            entry_price=50_000.0,
            opened_at=ts_ms - 3_600_000,
            closed_at=None,
            status=PositionStatus.OPEN.value,
            farb_position_id=farb.id,
        )
        s.add(pos)
        await s.flush()

        fill = DBFill(
            position_id=pos.id,
            ts_ms=ts_ms,
            side=side.value,
            qty=qty,
            price=50_000.0,
            fee=fee,
            slippage_bps=0.0,
            is_paper=False,
        )
        s.add(fill)
        await s.commit()
        return strategy.id, pos.id, fill.id


def _make_hl_fill(
    *,
    side: str = "B",
    sz: float = 1.0,
    ts_ms: int = _FIXED_TS_MS,
    coin: str = "BTC",
    fee_raw: float = 0.05,
    fee_token: str = "USDC",
    px: float = 50_000.0,
    oid: int = 1,
) -> HLUserFill:
    return HLUserFill(
        oid=oid,
        side=side,
        sz=sz,
        px=px,
        ts_ms=ts_ms,
        fee_raw=fee_raw,
        fee_token=fee_token,
        coin=coin,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_no_address_raises_runtime_error(session_factory, mock_client):
    ctx = HLActionContext(
        client=mock_client,
        symbols=_make_symbols(mock_client),
        session_factory=session_factory,
        exchange_name="hyperliquid",
        address=None,
        clock_fn=lambda: datetime.now(UTC),
    )
    action = BackfillFeesAction(ctx)
    with pytest.raises(RuntimeError, match="account_address required"):
        await action.execute(strategy_id=1)


async def test_empty_fills_returns_zero(session_factory, mock_client):
    """No zero-fee fills in DB → returns 0, never calls user_fills_by_time."""
    strategy_id, _, _ = await _seed_db(session_factory, fee=0.10)  # non-zero fee

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0
    mock_client.user_fills_by_time.assert_not_called()


async def test_hl_fetch_fails_returns_zero_with_warning(session_factory, mock_client, caplog):
    """user_fills_by_time raises → returns 0, logs a warning."""
    strategy_id, _, _ = await _seed_db(session_factory, fee=0.0)
    mock_client.user_fills_by_time.side_effect = RuntimeError("network error")

    action = make_action(session_factory, mock_client)
    with caplog.at_level(logging.WARNING):
        result = await action.execute(strategy_id)

    assert result == 0
    assert "user_fills_by_time failed" in caplog.text


async def test_happy_path_perp_usdc_fee(session_factory, mock_client):
    """One zero-fee PERP fill, USDC fee token → DB row updated, returns 1."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == pytest.approx(0.05)


async def test_happy_path_spot_wrapped_token_fee(session_factory, mock_client):
    """SPOT fill with UBTC fee_token → fee_usdc = fee_raw * px."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG,
        qty=0.001, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(
            side="B", sz=0.001, ts_ms=_FIXED_TS_MS, coin="@142",
            fee_raw=0.0001, fee_token="UBTC", px=60_000.0,
        ),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == pytest.approx(0.0001 * 60_000.0)


async def test_unknown_fee_token_raw_passthrough(session_factory, mock_client):
    """fee_token not USDC and not in SPOT_TOKEN_INVERSE → fee_usdc = fee_raw."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.07, fee_token="XYZ"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == pytest.approx(0.07)


async def test_fee_usdc_zero_skips_update(session_factory, mock_client):
    """HL match found but fee_raw=0.0 → row NOT updated, returns 0."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.0, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == 0.0


async def test_side_mismatch_no_match(session_factory, mock_client):
    """DB fill is LONG (side='long'); HL fill side='A' (sell) → no match."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="A", sz=1.0, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == 0.0


async def test_qty_mismatch_over_one_pct_no_match(session_factory, mock_client):
    """DB qty=1.0, HL sz=1.02 → mismatch >1%, no match."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.02, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0


async def test_qty_within_one_pct_tolerance_matches(session_factory, mock_client):
    """DB qty=1.0, HL sz=1.005 → within 1% tolerance, match succeeds."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.005, ts_ms=_FIXED_TS_MS, coin="BTC", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1


async def test_time_drift_over_30s_no_match(session_factory, mock_client):
    """DB ts_ms=1000000, HL ts_ms=1031001 → drift >30000ms, no match."""
    base_ts = 1_000_000
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=base_ts,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=base_ts + 31_000, coin="BTC", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0


async def test_perp_coin_mismatch_no_match(session_factory, mock_client):
    """DB coin='BTC', HL coin='ETH' → PERP coin mismatch, no match."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=_FIXED_TS_MS, coin="ETH", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 0


async def test_spot_match_via_at_prefix(session_factory, mock_client):
    """SPOT fill with HL coin starting '@' → match accepted."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG,
        qty=0.01, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=0.01, ts_ms=_FIXED_TS_MS, coin="@142", fee_raw=0.05, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == pytest.approx(0.05)


async def test_spot_match_via_slash(session_factory, mock_client):
    """SPOT fill with HL coin like 'UBTC/USDC' → match accepted."""
    strategy_id, _, fill_id = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.SPOT, side=Side.LONG,
        qty=0.01, fee=0.0, ts_ms=_FIXED_TS_MS,
    )
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=0.01, ts_ms=_FIXED_TS_MS, coin="UBTC/USDC", fee_raw=0.03, fee_token="USDC"),
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 1
    async with session_factory() as s:
        row = await s.get(DBFill, fill_id)
    assert row.fee == pytest.approx(0.03)


async def test_min_ts_window_calculation(session_factory, mock_client):
    """user_fills_by_time called with min(fill.ts_ms) - 60_000."""
    ts1 = _FIXED_TS_MS
    ts2 = _FIXED_TS_MS + 5_000

    # Seed two fills with different ts_ms
    strategy_id, _, _ = await _seed_db(
        session_factory, coin="BTC", instrument=Instrument.PERP, side=Side.LONG,
        qty=1.0, fee=0.0, ts_ms=ts1,
    )
    # Second fill for same strategy: need a new farb_position + position
    async with session_factory() as s:
        exc_id = await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid"))
        strat_id = await s.scalar(select(DBStrategy.id).limit(1))
        farb2 = DBFarbPosition(
            strategy_id=strat_id,
            coin="ETH",
            state="open",
            state_data={},
            opened_at=ts2 - 3_600_000,
        )
        s.add(farb2)
        await s.flush()
        pos2 = DBPosition(
            exchange_id=exc_id,
            coin="ETH",
            instrument=Instrument.PERP.value,
            side=Side.LONG.value,
            qty=1.0,
            entry_price=3_000.0,
            opened_at=ts2 - 3_600_000,
            closed_at=None,
            status=PositionStatus.OPEN.value,
            farb_position_id=farb2.id,
        )
        s.add(pos2)
        await s.flush()
        fill2 = DBFill(
            position_id=pos2.id,
            ts_ms=ts2,
            side=Side.LONG.value,
            qty=1.0,
            price=3_000.0,
            fee=0.0,
            slippage_bps=0.0,
            is_paper=False,
        )
        s.add(fill2)
        await s.commit()

    mock_client.user_fills_by_time.return_value = []

    action = make_action(session_factory, mock_client)
    await action.execute(strategy_id)

    expected_min_ts = ts1 - 60_000  # ts1 < ts2
    mock_client.user_fills_by_time.assert_called_once_with("0xabc", expected_min_ts)


async def test_multiple_fills_partial_match(session_factory, mock_client):
    """3 zero-fee fills: 2 match (different coins), 1 doesn't → returns 2."""
    ts = _FIXED_TS_MS

    async with session_factory() as s:
        exc_id = await s.scalar(select(DBExchange.id).where(DBExchange.name == "hyperliquid"))

        strategy = DBStrategy(name="multi", version="1", params_json={}, status="idle")
        s.add(strategy)
        await s.flush()

        fill_ids = []
        for coin in ("BTC", "ETH", "SOL"):
            farb = DBFarbPosition(
                strategy_id=strategy.id,
                coin=coin,
                state="open",
                state_data={},
                opened_at=ts - 3_600_000,
            )
            s.add(farb)
            await s.flush()

            pos = DBPosition(
                exchange_id=exc_id,
                coin=coin,
                instrument=Instrument.PERP.value,
                side=Side.LONG.value,
                qty=1.0,
                entry_price=100.0,
                opened_at=ts - 3_600_000,
                closed_at=None,
                status=PositionStatus.OPEN.value,
                farb_position_id=farb.id,
            )
            s.add(pos)
            await s.flush()

            fill = DBFill(
                position_id=pos.id,
                ts_ms=ts,
                side=Side.LONG.value,
                qty=1.0,
                price=100.0,
                fee=0.0,
                slippage_bps=0.0,
                is_paper=False,
            )
            s.add(fill)
            await s.flush()
            fill_ids.append(fill.id)

        await s.commit()
        strategy_id = strategy.id

    # BTC and ETH match, SOL has no HL fill
    mock_client.user_fills_by_time.return_value = [
        _make_hl_fill(side="B", sz=1.0, ts_ms=ts, coin="BTC", fee_raw=0.05, fee_token="USDC", oid=1),
        _make_hl_fill(side="B", sz=1.0, ts_ms=ts, coin="ETH", fee_raw=0.03, fee_token="USDC", oid=2),
        # No SOL fill
    ]

    action = make_action(session_factory, mock_client)
    result = await action.execute(strategy_id)

    assert result == 2

    async with session_factory() as s:
        btc_fill = await s.get(DBFill, fill_ids[0])
        eth_fill = await s.get(DBFill, fill_ids[1])
        sol_fill = await s.get(DBFill, fill_ids[2])

    assert btc_fill.fee == pytest.approx(0.05)
    assert eth_fill.fee == pytest.approx(0.03)
    assert sol_fill.fee == 0.0
