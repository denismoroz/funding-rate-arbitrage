"""Tests for reconcile_spot_leg — the DB-vs-wallet check right after a spot leg is opened."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from frab.db.models import Base, Exchange as DBExchange, Position as DBPosition
from frab.domain import FarbPosition, FarbState, Instrument, PositionStatus, Side
from frab.strategy.two_phase.actions.spot_reconcile import reconcile_spot_leg


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DBExchange(name="hyperliquid", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5))
        await s.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _add_position(sf, *, coin, qty, instrument=Instrument.SPOT, status=PositionStatus.OPEN):
    async with sf() as s:
        s.add(DBPosition(
            exchange_id=1, coin=coin, instrument=instrument.value, side=Side.LONG.value,
            qty=qty, entry_price=1.0, opened_at=1_700_000_000_000, closed_at=None,
            status=status.value, farb_position_id=None,
        ))
        await s.commit()


def _fp(coin="HYPE") -> FarbPosition:
    return FarbPosition(
        id=31, strategy_id=1, coin=coin, state=FarbState.OPENING_LONG, state_data={},
        spot_position_id=None, perp_position_id=None, margin_position_id=None,
        opened_at=datetime(2026, 10, 8, tzinfo=timezone.utc), closed_at=None,
    )


def _exchange(mocker, lot=0.01):
    exchange = mocker.AsyncMock()
    exchange.get_lot_size.return_value = lot
    return exchange


async def test_quiet_when_record_and_wallet_agree(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.19986561)
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.19986561)

    bus.publish.assert_not_awaited()


async def test_quiet_when_gap_is_a_sub_lot_remainder_of_an_earlier_close(mocker, sf):
    # HYPE #31 after the fix: recorded 0.19986561, wallet 0.20973794; the 0.00987233 left by
    # HYPE #23's close is below the 0.01 lot and covered by the hedge
    await _add_position(sf, coin="HYPE", qty=0.19986561)
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.20973794)

    bus.publish.assert_not_awaited()


async def test_quiet_when_gap_is_exactly_one_lot(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.2)
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.2 + 0.01)

    bus.publish.assert_not_awaited()


async def test_warns_when_wallet_exceeds_record_by_more_than_a_lot(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.19)
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.2097)

    bus.publish.assert_awaited_once()
    event = bus.publish.call_args.args[0]
    assert event.level == "WARNING"
    assert event.kind == "spot.mismatch"
    assert event.payload_json["farb_position_id"] == 31
    assert event.payload_json["coin"] == "HYPE"
    assert event.payload_json["db"] == pytest.approx(0.19)
    assert event.payload_json["wallet"] == pytest.approx(0.2097)
    assert event.payload_json["diff"] == pytest.approx(0.0197)
    assert event.payload_json["lot"] == pytest.approx(0.01)


async def test_warns_when_record_exceeds_wallet_by_more_than_a_lot(mocker, sf):
    # a leg recorded bigger than anything the wallet holds
    await _add_position(sf, coin="HYPE", qty=0.2)
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.1)

    event = bus.publish.call_args.args[0]
    assert event.kind == "spot.mismatch"
    assert event.payload_json["diff"] == pytest.approx(-0.1)


async def test_compares_the_sum_of_all_open_spot_rows_of_the_coin(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.2)
    await _add_position(sf, coin="HYPE", qty=0.3)
    await _add_position(sf, coin="HYPE", qty=9.0, status=PositionStatus.CLOSED)   # closed: not held
    await _add_position(sf, coin="HYPE", qty=9.0, instrument=Instrument.PERP)     # perp: not in the wallet
    await _add_position(sf, coin="ETH", qty=9.0)                                  # another coin
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.5)

    bus.publish.assert_not_awaited()


async def test_skips_when_exchange_has_no_lot_size(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.2)
    bus = mocker.AsyncMock()
    exchange = mocker.AsyncMock(spec=["open_position"])   # no get_lot_size

    await reconcile_spot_leg(exchange=exchange, session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=5.0)

    bus.publish.assert_not_awaited()


async def test_a_failing_check_never_raises(mocker, sf):
    exchange = mocker.AsyncMock()
    exchange.get_lot_size.side_effect = RuntimeError("HL down")
    bus = mocker.AsyncMock()

    await reconcile_spot_leg(exchange=exchange, session_factory=sf, event_bus=bus,
                             fp=_fp(), wallet_qty=0.2)

    bus.publish.assert_not_awaited()


async def test_works_without_an_event_bus(mocker, sf):
    await _add_position(sf, coin="HYPE", qty=0.19)

    await reconcile_spot_leg(exchange=_exchange(mocker), session_factory=sf, event_bus=None,
                             fp=_fp(), wallet_qty=5.0)
