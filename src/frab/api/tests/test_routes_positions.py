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
    Signal,
    Strategy,
)
from frab.db.models import Decision
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


# ── Slippage & breakeven tests ────────────────────────────────────────────────


_slip_counter = 0


async def _seed_slip_position(
    session_factory,
    *,
    entry_spot: float,
    entry_perp: float,
    spot_units: float,
    exit_spot: float | None = None,
    exit_perp: float | None = None,
    fees_paid: float = 0.0,
    funding_collected: float = 0.0,
    mark: float | None = None,
    signal_value: float | None = None,
    status: PositionStatus = PositionStatus.OPEN,
) -> tuple[int, int]:
    """Seed a position (and optionally a Price + Signal) for slip/BE tests."""
    global _slip_counter
    _slip_counter += 1
    tag = _slip_counter
    async with session_scope(session_factory) as s:
        strat = Strategy(name=f"slip_strat_{tag}", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        exc = Exchange(name=f"HL_slip_{tag}", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        mkt = Market(exchange_id=exc.id, coin="SLIP")
        s.add(mkt)
        await s.flush()
        pos = Position(
            strategy_id=strat.id,
            market_id=mkt.id,
            mode=PositionMode.PAPER,
            status=status,
            opened_at=_utc(),
            closed_at=_utc(1) if status == PositionStatus.CLOSED else None,
            spot_units=spot_units,
            perp_units=-spot_units,
            entry_spot_price=entry_spot,
            entry_perp_price=entry_perp,
            exit_spot_price=exit_spot,
            exit_perp_price=exit_perp,
            fees_paid=fees_paid,
            funding_collected=funding_collected,
        )
        s.add(pos)
        await s.flush()
        if mark is not None:
            s.add(Price(market_id=mkt.id, ts=_utc(1), mark=mark))
        if signal_value is not None:
            s.add(
                Signal(
                    strategy_id=strat.id,
                    market_id=mkt.id,
                    ts=_utc(1),
                    signal_value=signal_value,
                    regime_pass=True,
                    action=Decision.NONE,
                )
            )
        return pos.id, strat.id


async def test_list_positions_slippage_cost_computed(api_client, session_factory):
    # Open position: entry_spot=101, entry_perp=99, spot_units=10
    # slippage_cost = (101 - 99) * 10 = 20.0
    pos_id, strat_id = await _seed_slip_position(
        session_factory,
        entry_spot=101.0,
        entry_perp=99.0,
        spot_units=10.0,
        mark=100.0,
    )

    resp = await api_client.get(f"/api/positions?strategy_id={strat_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["slippage_cost"] == pytest.approx(20.0)

    # Closed position additionally pays close-side slippage:
    # exit_perp=101, exit_spot=99 => (101-99)*10 = 20
    # total = 20 + 20 = 40.0
    pos_id2, strat_id2 = await _seed_slip_position(
        session_factory,
        entry_spot=101.0,
        entry_perp=99.0,
        spot_units=10.0,
        exit_spot=99.0,
        exit_perp=101.0,
        status=PositionStatus.CLOSED,
    )

    resp2 = await api_client.get(f"/api/positions?strategy_id={strat_id2}&status=closed")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2) == 1
    assert data2[0]["slippage_cost"] == pytest.approx(40.0)


async def test_list_positions_breakeven_date_projection(api_client, session_factory):
    # fees_paid=2.1, funding_collected=0, slippage from entry_spot=101/entry_perp=99 => 20
    # remaining = 2.1 + 20 - 0 = 22.1
    # signal_value = 0.10 (10% annual), mark=100, perp_units=10
    # hourly_income = 10 * 100 * 0.10 / 8760 ≈ 0.011416
    # hours_to_be = 22.1 / 0.011416 ≈ 1936 h
    _, strat_id = await _seed_slip_position(
        session_factory,
        entry_spot=101.0,
        entry_perp=99.0,
        spot_units=10.0,
        fees_paid=2.1,
        funding_collected=0.0,
        mark=100.0,
        signal_value=0.10,
    )

    before = datetime.now(UTC)
    resp = await api_client.get(f"/api/positions?strategy_id={strat_id}&status=open")
    after = datetime.now(UTC)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    p = data[0]

    assert p["breakeven_at"] is not None
    be = datetime.fromisoformat(p["breakeven_at"])

    remaining = 2.1 + 20.0 - 0.0
    hourly_income = 10.0 * 100.0 * 0.10 / 8760
    hours_to_be = remaining / hourly_income
    expected_low = before + timedelta(hours=hours_to_be)
    expected_high = after + timedelta(hours=hours_to_be)

    # Allow a few seconds of tolerance for test execution time
    assert expected_low - timedelta(seconds=5) <= be <= expected_high + timedelta(seconds=5)


async def test_list_positions_breakeven_at_none_when_signal_zero_or_negative(
    api_client, session_factory
):
    # signal_value = -0.05 → breakeven_at should be None
    _, strat_id = await _seed_slip_position(
        session_factory,
        entry_spot=101.0,
        entry_perp=99.0,
        spot_units=10.0,
        fees_paid=2.1,
        funding_collected=0.0,
        mark=100.0,
        signal_value=-0.05,
    )

    resp = await api_client.get(f"/api/positions?strategy_id={strat_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["breakeven_at"] is None
