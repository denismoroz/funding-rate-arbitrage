"""Tests for GET /api/farb-positions, GET /api/farb-positions/{id}, and force-close endpoints."""
from __future__ import annotations

import time

import pytest

from frab.db.models import Exchange, FarbPosition as FarbPositionRow, Position, Price, Strategy
from frab.db.session import session_scope
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side
from frab.repo.farb_repo import FarbRepo


# ── helpers ──────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_ago(hours: float) -> int:
    return _now_ms() - int(hours * 3_600_000)


async def _seed_exchange(session_factory, *, name: str = "hyperliquid") -> int:
    async with session_scope(session_factory) as s:
        exc = Exchange(name=name, funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        return exc.id


async def _seed_strategy(session_factory) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name="test", version="v1", params_json={}, status="idle")
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_position(
    session_factory,
    *,
    exchange_id: int,
    coin: str = "BTC",
    instrument: str = Instrument.SPOT.value,
    side: str = Side.LONG.value,
    qty: float = 0.01,
    entry_price: float = 90_000.0,
    farb_position_id: int | None = None,
) -> int:
    async with session_scope(session_factory) as s:
        pos = Position(
            exchange_id=exchange_id,
            coin=coin,
            instrument=instrument,
            side=side,
            qty=qty,
            entry_price=entry_price,
            opened_at=_now_ms(),
            closed_at=None,
            status=PositionStatus.OPEN.value,
            farb_position_id=farb_position_id,
        )
        s.add(pos)
        await s.flush()
        return pos.id


async def _seed_farb_position(
    session_factory,
    *,
    strategy_id: int,
    coin: str = "BTC",
    state: str = FarbState.PRE_BREAKEVEN.value,
    state_data: dict | None = None,
    opened_at: int | None = None,
    closed_at: int | None = None,
    spot_position_id: int | None = None,
    perp_position_id: int | None = None,
    margin_position_id: int | None = None,
) -> int:
    async with session_scope(session_factory) as s:
        fp = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=state,
            state_data=state_data or {},
            opened_at=opened_at or _now_ms(),
            closed_at=closed_at,
            spot_position_id=spot_position_id,
            perp_position_id=perp_position_id,
            margin_position_id=margin_position_id,
        )
        s.add(fp)
        await s.flush()
        return fp.id


# ── test_list_farb_positions_filters_by_status_active ────────────────────────


async def test_list_farb_positions_filters_by_status_active(api_client, session_factory):
    """Seed PRE_BREAKEVEN + CHECK_MARGIN + CLOSED; query status=active → get non-terminal (all but CLOSED/FAILED)."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    pre_id = await _seed_farb_position(session_factory, strategy_id=strat_id, state=FarbState.PRE_BREAKEVEN.value)
    cm_id = await _seed_farb_position(
        session_factory, strategy_id=strat_id, state=FarbState.CHECK_MARGIN.value
    )
    await _seed_farb_position(
        session_factory, strategy_id=strat_id, state=FarbState.CLOSED.value,
        closed_at=_now_ms()
    )

    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}&status=active")
    assert resp.status_code == 200
    data = resp.json()
    # Both PRE_BREAKEVEN and CHECK_MARGIN are non-terminal; CLOSED is excluded
    ids = {item["id"] for item in data}
    assert pre_id in ids
    assert cm_id in ids
    assert len(data) == 2
    states = {item["state"] for item in data}
    assert "PRE_BREAKEVEN" in states
    assert "CHECK_MARGIN" in states


# ── test_list_farb_positions_includes_legs ────────────────────────────────────


async def test_list_farb_positions_includes_legs(api_client, session_factory):
    """Seed OPEN FP with all 3 legs; verify legs dict is fully populated."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    # Create legs first (no farb_position_id yet — circular FK)
    margin_id = await _seed_position(
        session_factory, exchange_id=exc_id,
        coin="USDC", instrument=Instrument.COLLATERAL.value,
        side=Side.NONE.value, qty=600.0, entry_price=1.0,
    )
    spot_id = await _seed_position(
        session_factory, exchange_id=exc_id,
        coin="BTC", instrument=Instrument.SPOT.value,
        side=Side.LONG.value, qty=0.01, entry_price=90_000.0,
    )
    perp_id = await _seed_position(
        session_factory, exchange_id=exc_id,
        coin="BTC", instrument=Instrument.PERP.value,
        side=Side.SHORT.value, qty=0.01, entry_price=90_000.0,
    )

    fp_id = await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        coin="BTC",
        state=FarbState.PRE_BREAKEVEN.value,
        spot_position_id=spot_id,
        perp_position_id=perp_id,
        margin_position_id=margin_id,
    )

    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    item = data[0]
    assert item["id"] == fp_id
    assert item["legs"]["spot"] is not None
    assert item["legs"]["spot"]["id"] == spot_id
    assert item["legs"]["spot"]["qty"] == pytest.approx(0.01)
    assert item["legs"]["spot"]["entry_price"] == pytest.approx(90_000.0)
    assert item["legs"]["perp"] is not None
    assert item["legs"]["perp"]["id"] == perp_id
    assert item["legs"]["collateral"] is not None
    assert item["legs"]["collateral"]["id"] == margin_id
    assert item["legs"]["collateral"]["qty"] == pytest.approx(600.0)


# ── test_list_farb_positions_computes_hours_held ──────────────────────────────


async def test_list_farb_positions_computes_hours_held(api_client, session_factory):
    """Seed FP opened 5 hours ago; verify hours_held ≈ 5.0."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    opened_at = _ms_ago(5.0)
    await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        state=FarbState.PRE_BREAKEVEN.value,
        opened_at=opened_at,
    )

    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["hours_held"] == pytest.approx(5.0, abs=0.05)


# ── test_list_farb_positions_computes_unrealized_pnl ─────────────────────────


async def test_list_farb_positions_computes_unrealized_pnl(api_client, session_factory):
    """Seed OPEN FP with known entry prices + a Price row; verify unrealized_pnl_usdc."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    perp_entry = 90_000.0
    spot_entry = 89_900.0
    qty = 0.01

    spot_id = await _seed_position(
        session_factory, exchange_id=exc_id,
        coin="BTC", instrument=Instrument.SPOT.value,
        side=Side.LONG.value, qty=qty, entry_price=spot_entry,
    )
    perp_id = await _seed_position(
        session_factory, exchange_id=exc_id,
        coin="BTC", instrument=Instrument.PERP.value,
        side=Side.SHORT.value, qty=qty, entry_price=perp_entry,
    )

    await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        coin="BTC",
        state=FarbState.PRE_BREAKEVEN.value,
        spot_position_id=spot_id,
        perp_position_id=perp_id,
    )

    # Insert a Price row: mark=91_000, spot=90_500
    mark = 91_000.0
    spot_price = 90_500.0
    async with session_scope(session_factory) as s:
        s.add(Price(
            exchange_id=exc_id,
            coin="BTC",
            ts_ms=_now_ms(),
            mark=mark,
            spot=spot_price,
        ))

    # Manual calculation:
    # perp short: qty * (entry - mark) = 0.01 * (90000 - 91000) = -10.0
    # spot long:  qty * (spot - entry) = 0.01 * (90500 - 89900) = 6.0
    # total = -4.0
    expected_pnl = qty * (perp_entry - mark) + qty * (spot_price - spot_entry)

    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["unrealized_pnl_usdc"] == pytest.approx(expected_pnl, abs=1e-6)


# ── test_get_farb_position_by_id_404 ─────────────────────────────────────────


async def test_get_farb_position_by_id_404(api_client):
    """Request non-existent id → 404."""
    resp = await api_client.get("/api/farb-positions/99999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ── test_get_farb_position_by_id_returns_correct_shape ───────────────────────


async def test_get_farb_position_by_id_returns_correct_shape(api_client, session_factory):
    """GET /api/farb-positions/{id} returns same shape as list item."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    fp_id = await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        coin="ETH",
        state=FarbState.PRE_BREAKEVEN.value,
        state_data={"target_signal_apr": 0.25, "consec_negative_hours": 3},
    )

    resp = await api_client.get(f"/api/farb-positions/{fp_id}")
    assert resp.status_code == 200
    item = resp.json()

    assert item["id"] == fp_id
    assert item["coin"] == "ETH"
    assert item["state"] == "PRE_BREAKEVEN"
    assert item["target_signal_apr"] == pytest.approx(0.25)
    assert item["consec_negative_hours"] == 3
    assert "legs" in item
    assert "hours_held" in item
    assert item["legs"]["spot"] is None
    assert item["legs"]["perp"] is None
    assert item["legs"]["collateral"] is None


# ── test_list_farb_positions_status_open ─────────────────────────────────────


async def test_list_farb_positions_status_open(api_client, session_factory):
    """status=open returns only ACTIVE_STATES positions (PRE_BREAKEVEN, POST_BREAKEVEN)."""
    exc_id = await _seed_exchange(session_factory)
    strat_id = await _seed_strategy(session_factory)

    pre_id = await _seed_farb_position(
        session_factory, strategy_id=strat_id, state=FarbState.PRE_BREAKEVEN.value
    )
    post_id = await _seed_farb_position(
        session_factory, strategy_id=strat_id, state=FarbState.POST_BREAKEVEN.value
    )
    await _seed_farb_position(
        session_factory, strategy_id=strat_id, state=FarbState.CHECK_MARGIN.value
    )

    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}&status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    ids = {item["id"] for item in data}
    assert pre_id in ids
    assert post_id in ids


# ── test_list_farb_positions_unknown_status_422 ───────────────────────────────


async def test_list_farb_positions_unknown_status_422(api_client, session_factory):
    """Unknown status value → 422."""
    strat_id = await _seed_strategy(session_factory)
    resp = await api_client.get(f"/api/farb-positions?strategy_id={strat_id}&status=bogus")
    assert resp.status_code == 422


# ── force-close single FP ─────────────────────────────────────────────────────


async def test_close_open_fp_returns_closing_short(api_client, session_factory, farb_repo):
    """POST /api/farb-positions/{id}/close on PRE_BREAKEVEN FP → 200, new_state=CLOSING_SHORT."""
    strat_id = await _seed_strategy(session_factory)
    fp_id = await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        coin="SOL",
        state=FarbState.PRE_BREAKEVEN.value,
        state_data={"gross_funding_so_far": 10.0},
    )

    resp = await api_client.post(f"/api/farb-positions/{fp_id}/close")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == fp_id
    assert data["coin"] == "SOL"
    assert data["new_state"] == "CLOSING_SHORT"
    assert "ts_ms" in data

    # Verify the DB row is now in CLOSING_SHORT with exit markers in state_data
    updated = await farb_repo.get(fp_id)
    assert updated is not None
    assert updated.state == FarbState.CLOSING_SHORT
    assert updated.state_data["exit_decision"] == "forced"
    assert "exit_requested_at_ms" in updated.state_data
    # Original state_data fields are preserved
    assert updated.state_data["gross_funding_so_far"] == pytest.approx(10.0)


async def test_close_non_open_fp_returns_409(api_client, session_factory):
    """POST /api/farb-positions/{id}/close on CHECK_MARGIN FP → 409."""
    strat_id = await _seed_strategy(session_factory)
    fp_id = await _seed_farb_position(
        session_factory,
        strategy_id=strat_id,
        coin="ETH",
        state=FarbState.CHECK_MARGIN.value,
    )

    resp = await api_client.post(f"/api/farb-positions/{fp_id}/close")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "CHECK_MARGIN" in detail or "check_margin" in detail


async def test_close_nonexistent_fp_returns_404(api_client):
    """POST /api/farb-positions/{id}/close on nonexistent id → 404."""
    resp = await api_client.post("/api/farb-positions/99999/close")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ── force-close all open FPs ──────────────────────────────────────────────────


async def test_close_all_closes_open_and_skips_non_open(api_client, session_factory, farb_repo):
    """POST /api/farb-positions/close-all with 2 active (PRE/POST_BREAKEVEN) + 1 CHECK_MARGIN.

    Behaviour: the two active positions are transitioned; the CHECK_MARGIN one is
    not touched (list_active only returns PRE_BREAKEVEN/POST_BREAKEVEN rows).
    """
    strat_id = await _seed_strategy(session_factory)

    open_id1 = await _seed_farb_position(
        session_factory, strategy_id=strat_id, coin="BTC", state=FarbState.PRE_BREAKEVEN.value
    )
    open_id2 = await _seed_farb_position(
        session_factory, strategy_id=strat_id, coin="ETH", state=FarbState.POST_BREAKEVEN.value
    )
    # CHECK_MARGIN — not returned by list_active, so not attempted
    await _seed_farb_position(
        session_factory, strategy_id=strat_id, coin="SOL", state=FarbState.CHECK_MARGIN.value
    )

    resp = await api_client.post(f"/api/farb-positions/close-all?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()

    closed_ids = [entry["id"] for entry in data["closed_ids"]]
    assert set(closed_ids) == {open_id1, open_id2}
    assert data["failed"] == []
    assert "ts_ms" in data

    # Both active FPs are now CLOSING_SHORT in the DB
    for fp_id in (open_id1, open_id2):
        updated = await farb_repo.get(fp_id)
        assert updated.state == FarbState.CLOSING_SHORT
        assert updated.state_data["exit_decision"] == "forced"


async def test_close_all_with_no_open_returns_empty(api_client, session_factory):
    """POST /api/farb-positions/close-all with 0 OPEN positions → 200, closed_ids: []."""
    strat_id = await _seed_strategy(session_factory)
    await _seed_farb_position(
        session_factory, strategy_id=strat_id, coin="BTC", state=FarbState.CLOSED.value,
        closed_at=_now_ms(),
    )

    resp = await api_client.post(f"/api/farb-positions/close-all?strategy_id={strat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["closed_ids"] == []
    assert data["failed"] == []
