"""Tests for GET /api/positions and GET /api/positions/{position_id}/funding-history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from frab.db.models import (
    Exchange,
    Market,
    Position,
    PositionFundingAccrual,
    PositionMode,
    PositionStatus,
    Price,
    Strategy,
)
from frab.db.session import session_scope


def _utc(offset_hours: int = 0) -> datetime:
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(hours=offset_hours)


async def _seed_position(session_factory) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name="ph_strat", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_ph", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="BTC")
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strat.id,
            market_id=mkt.id,
            mode=PositionMode.PAPER,
            status=PositionStatus.OPEN,
            opened_at=_utc(),
            spot_units=0.1,
            perp_units=-0.1,
            entry_spot_price=30_000.0,
            entry_perp_price=30_010.0,
        )
        s.add(pos)
        await s.flush()
        return pos.id


async def test_get_funding_history_returns_rows_chronological(api_client, session_factory):
    pos_id = await _seed_position(session_factory)

    # Insert accrual rows in reverse chronological order to confirm sorting
    async with session_scope(session_factory) as s:
        s.add(PositionFundingAccrual(position_id=pos_id, ts=_utc(3), delta=0.03))
        s.add(PositionFundingAccrual(position_id=pos_id, ts=_utc(1), delta=0.01))
        s.add(PositionFundingAccrual(position_id=pos_id, ts=_utc(2), delta=0.02))

    resp = await api_client.get(f"/api/positions/{pos_id}/funding-history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert data[0]["ts"] < data[1]["ts"] < data[2]["ts"]
    assert data[0]["delta"] == pytest.approx(0.01)
    assert data[1]["delta"] == pytest.approx(0.02)
    assert data[2]["delta"] == pytest.approx(0.03)


async def test_get_funding_history_404_when_position_missing(api_client):
    resp = await api_client.get("/api/positions/999999/funding-history")
    assert resp.status_code == 404


async def test_get_funding_history_empty_list_for_position_with_no_accruals(api_client, session_factory):
    pos_id = await _seed_position(session_factory)

    resp = await api_client.get(f"/api/positions/{pos_id}/funding-history")
    assert resp.status_code == 200
    assert resp.json() == []


# ── MTM field tests ────────────────────────────────────────────────────────────

async def _seed_position_with_price(session_factory, mark: float) -> tuple[int, int]:
    """Seed a position + a Price row; return (pos_id, strategy_id)."""
    async with session_scope(session_factory) as s:
        strat = Strategy(name="mtm_strat", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_mtm", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="ETH")
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strat.id,
            market_id=mkt.id,
            mode=PositionMode.PAPER,
            status=PositionStatus.OPEN,
            opened_at=_utc(),
            spot_units=0.5,
            perp_units=-0.5,
            entry_spot_price=2_000.0,
            entry_perp_price=2_002.0,
            funding_collected=5.0,
            fees_paid=2.0,
        )
        s.add(pos)
        await s.flush()
        price_row = Price(
            market_id=mkt.id,
            ts=_utc(1),
            mark=mark,
        )
        s.add(price_row)
        return pos.id, strat.id


async def test_list_positions_mtm_fields_populated_when_price_exists(api_client, session_factory):
    mark = 1_900.0
    pos_id, strat_id = await _seed_position_with_price(session_factory, mark)

    resp = await api_client.get(f"/api/positions?strategy_id={strat_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    p = data[0]

    assert p["current_mark"] == pytest.approx(mark)
    # spot_units=0.5, entry_spot_price=2000 -> notional_at_entry=1000
    assert p["notional_at_entry"] == pytest.approx(1_000.0)
    # spot_value_now = 0.5 * 1900 = 950
    assert p["spot_value_now"] == pytest.approx(950.0)
    # perp_unrealized = |−0.5| * (2002 − 1900) = 0.5 * 102 = 51
    assert p["perp_unrealized"] == pytest.approx(51.0)
    # net_mtm = 950 − 1000 + 51 + 5 − 2 = 4
    assert p["net_mtm"] == pytest.approx(4.0)


async def test_list_positions_mtm_fields_none_when_no_price(api_client, session_factory):
    async with session_scope(session_factory) as s:
        strat = Strategy(name="mtm_noprice", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name="HL_np", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="SOL")
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strat.id,
            market_id=mkt.id,
            mode=PositionMode.PAPER,
            status=PositionStatus.OPEN,
            opened_at=_utc(),
            spot_units=10.0,
            perp_units=-10.0,
            entry_spot_price=100.0,
            entry_perp_price=100.5,
        )
        s.add(pos)
        strat_id = strat.id

    resp = await api_client.get(f"/api/positions?strategy_id={strat_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    p = data[0]

    assert p["current_mark"] is None
    assert p["spot_value_now"] is None
    assert p["perp_unrealized"] is None
    assert p["net_mtm"] is None
    # notional_at_entry is always computable
    assert p["notional_at_entry"] == pytest.approx(1_000.0)
