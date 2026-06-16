"""Tests for /api/xsmom/* routes."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.db.models import Exchange, Position, Strategy, XsmomPosition as XsmomPositionRow
from frab.db.models import XsmomScan as XsmomScanRow
from frab.db.session import session_scope
from frab.domain.enums import Instrument, PositionStatus, Side, XsmomState
from frab.repo.xsmom_repo import XsmomRepo, XsmomStateConflict


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


async def _seed_exchange(session_factory) -> int:
    async with session_scope(session_factory) as s:
        exc = Exchange(name="hl_xsmom_test", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        return exc.id


async def _seed_xsmom_strategy(session_factory, *, params: dict | None = None) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(
            name="xsmom",
            version="v1",
            params_json=params or {"budget_cap": 500.0, "universe": ["BTC", "ETH"], "leverage": 1},
            status="active",
        )
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_xsmom_position(
    session_factory,
    *,
    strategy_id: int,
    coin: str = "BTC",
    side: str = Side.LONG.value,
    state: str = XsmomState.OPENED.value,
    state_data: dict | None = None,
    opened_at: int | None = None,
    closed_at: int | None = None,
    perp_position_id: int | None = None,
) -> int:
    async with session_scope(session_factory) as s:
        row = XsmomPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            side=side,
            state=state,
            state_data=state_data or {},
            opened_at=opened_at or _now_ms(),
            closed_at=closed_at,
            perp_position_id=perp_position_id,
        )
        s.add(row)
        await s.flush()
        return row.id


async def _seed_position(
    session_factory,
    *,
    exchange_id: int,
    coin: str = "BTC",
    qty: float = 1.0,
    entry_price: float = 50_000.0,
    side: str = Side.LONG.value,
) -> int:
    async with session_scope(session_factory) as s:
        pos = Position(
            exchange_id=exchange_id,
            coin=coin,
            instrument=Instrument.PERP.value,
            side=side,
            qty=qty,
            entry_price=entry_price,
            opened_at=_now_ms(),
            closed_at=None,
            status=PositionStatus.OPEN.value,
        )
        s.add(pos)
        await s.flush()
        return pos.id


async def _seed_scan(
    session_factory,
    *,
    strategy_id: int,
    ts_ms: int | None = None,
    n_long: int = 2,
    n_short: int = 2,
) -> int:
    async with session_scope(session_factory) as s:
        scan = XsmomScanRow(
            strategy_id=strategy_id,
            ts_ms=ts_ms or _now_ms(),
            ranking_json=[{"coin": "BTC", "score": 0.9}],
            n_long=n_long,
            n_short=n_short,
            note=None,
        )
        s.add(scan)
        await s.flush()
        return scan.id


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_xsmom_app(session_factory, *, strategy_id: int, xsmom_strategy=None, xsmom_repo=None):
    """Create an app with xsmom state populated."""
    from frab.repo.farb_repo import FarbRepo
    farb_repo = FarbRepo(session_factory)
    app = create_app(session_factory, farb_repo=farb_repo)
    app.state.xsmom_strategy_id = strategy_id
    app.state.xsmom_strategy = xsmom_strategy
    app.state.xsmom_repo = xsmom_repo or XsmomRepo(session_factory)
    app.state.xsmom_exchange = None  # no live exchange — use DB fallback
    app.state.xsmom_loop = None
    return app


@pytest_asyncio.fixture
async def xsmom_strategy_id(session_factory):
    return await _seed_xsmom_strategy(session_factory)


@pytest_asyncio.fixture
async def xsmom_repo(session_factory):
    return XsmomRepo(session_factory)


@pytest_asyncio.fixture
async def xsmom_client(session_factory, xsmom_strategy_id, xsmom_repo):
    """API client with xsmom state wired (no live exchange)."""
    app = _make_xsmom_app(session_factory, strategy_id=xsmom_strategy_id, xsmom_repo=xsmom_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        yield c


@pytest_asyncio.fixture
async def unconfigured_client(session_factory):
    """API client WITHOUT xsmom state — for 503 tests."""
    from frab.repo.farb_repo import FarbRepo
    farb_repo = FarbRepo(session_factory)
    app = create_app(session_factory, farb_repo=farb_repo)
    # Deliberately do NOT set xsmom_strategy_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        yield c


# ── 503 when not configured ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,method", [
    ("/api/xsmom/summary", "GET"),
    ("/api/xsmom/positions", "GET"),
    ("/api/xsmom/positions/1/close", "POST"),
    ("/api/xsmom/positions/close-all", "POST"),
    ("/api/xsmom/rebalance", "POST"),
    ("/api/xsmom/scans", "GET"),
    ("/api/xsmom/params", "GET"),
    ("/api/xsmom/params", "PATCH"),
])
async def test_503_when_not_configured(unconfigured_client, path, method):
    """All xsmom endpoints return 503 when xsmom_strategy_id is absent."""
    resp = await getattr(unconfigured_client, method.lower())(
        path,
        **({"json": {"params": {}}} if method == "PATCH" else {}),
    )
    assert resp.status_code == 503
    assert "xsmom engine not configured" in resp.json()["detail"]


# ── GET /api/xsmom/positions — list ──────────────────────────────────────────

async def test_list_positions_returns_all(xsmom_client, session_factory, xsmom_strategy_id):
    """No status filter → returns all positions."""
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="ETH", state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="SOL", state=XsmomState.FAILED.value,
        closed_at=_now_ms(),
    )

    resp = await xsmom_client.get("/api/xsmom/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    coins = {d["coin"] for d in data}
    assert coins == {"BTC", "ETH", "SOL"}


async def test_list_positions_status_open(xsmom_client, session_factory, xsmom_strategy_id):
    """status=open returns only OPENED rows."""
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="ETH", state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )

    resp = await xsmom_client.get("/api/xsmom/positions?status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["coin"] == "BTC"
    assert data[0]["state"] == "OPENED"


async def test_list_positions_status_active(xsmom_client, session_factory, xsmom_strategy_id):
    """status=active returns non-terminal states (excludes CLOSED, FAILED)."""
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="ETH", state=XsmomState.NEW.value
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="SOL", state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="AVAX", state=XsmomState.FAILED.value,
        closed_at=_now_ms(),
    )

    resp = await xsmom_client.get("/api/xsmom/positions?status=active")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    coins = {d["coin"] for d in data}
    assert coins == {"BTC", "ETH"}


async def test_list_positions_status_closed(xsmom_client, session_factory, xsmom_strategy_id):
    """status=closed returns only CLOSED rows."""
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    closed_id = await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="ETH", state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )

    resp = await xsmom_client.get("/api/xsmom/positions?status=closed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == closed_id


async def test_list_positions_status_failed(xsmom_client, session_factory, xsmom_strategy_id):
    """status=failed returns only FAILED rows."""
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    failed_id = await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="SOL", state=XsmomState.FAILED.value,
        closed_at=_now_ms(),
    )

    resp = await xsmom_client.get("/api/xsmom/positions?status=failed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == failed_id


async def test_list_positions_unknown_status_422(xsmom_client, session_factory, xsmom_strategy_id):
    """Unknown status → 422."""
    resp = await xsmom_client.get("/api/xsmom/positions?status=bogus")
    assert resp.status_code == 422


async def test_list_positions_enrichment_shape(xsmom_client, session_factory, xsmom_strategy_id):
    """Positions include required fields in correct shape."""
    exc_id = await _seed_exchange(session_factory)
    perp_id = await _seed_position(
        session_factory, exchange_id=exc_id, coin="BTC", qty=0.1, entry_price=60_000.0
    )
    state_data = {
        "gross_funding_so_far": 5.0,
        "total_fees_paid": 2.0,
        "notional": 6_000.0,
        "required_margin": 200.0,
        "score": 0.75,
        "leverage": 1,
    }
    pos_id = await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="BTC",
        side=Side.LONG.value,
        state=XsmomState.OPENED.value,
        state_data=state_data,
        perp_position_id=perp_id,
    )

    resp = await xsmom_client.get("/api/xsmom/positions?status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    item = data[0]
    assert item["id"] == pos_id
    assert item["coin"] == "BTC"
    assert item["side"] == "long"
    assert item["state"] == "OPENED"
    assert item["funding_usdc"] == pytest.approx(5.0)
    assert item["fees_usdc"] == pytest.approx(2.0)
    assert item["notional"] == pytest.approx(6_000.0)
    assert item["required_margin"] == pytest.approx(200.0)
    assert item["score"] == pytest.approx(0.75)
    assert item["hours_held"] is not None
    assert item["perp_leg"] is not None
    assert item["perp_leg"]["qty"] == pytest.approx(0.1)
    assert item["perp_leg"]["entry_price"] == pytest.approx(60_000.0)


# ── POST /api/xsmom/positions/{id}/close ──────────────────────────────────────

async def test_close_position_200_when_opened(session_factory, xsmom_strategy_id, xsmom_repo):
    """POST close on OPENED position → 200, new_state='close'."""
    pos_id = await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="BTC",
        state=XsmomState.OPENED.value,
    )

    # Mock strategy.manual_close to call real repo
    xsmom_strategy_mock = MagicMock()

    async def _real_close(xsmom_position_id):
        return await xsmom_repo.transition(
            xsmom_position_id,
            from_state=XsmomState.OPENED,
            to_state=XsmomState.CLOSE,
            state_data={"exit_decision": "manual_close"},
        )

    xsmom_strategy_mock.manual_close = _real_close

    app = _make_xsmom_app(
        session_factory,
        strategy_id=xsmom_strategy_id,
        xsmom_strategy=xsmom_strategy_mock,
        xsmom_repo=xsmom_repo,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        resp = await c.post(f"/api/xsmom/positions/{pos_id}/close")

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == pos_id
    assert data["coin"] == "BTC"
    assert data["new_state"] == "close"
    assert "ts_ms" in data

    # Verify DB
    updated = await xsmom_repo.get(pos_id)
    assert updated is not None
    assert updated.state == XsmomState.CLOSE


async def test_close_position_409_when_not_opened(session_factory, xsmom_strategy_id, xsmom_repo):
    """POST close on CLOSED position → 409."""
    pos_id = await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="ETH",
        state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )

    xsmom_strategy_mock = MagicMock()
    app = _make_xsmom_app(
        session_factory,
        strategy_id=xsmom_strategy_id,
        xsmom_strategy=xsmom_strategy_mock,
        xsmom_repo=xsmom_repo,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        resp = await c.post(f"/api/xsmom/positions/{pos_id}/close")

    assert resp.status_code == 409
    assert "closed" in resp.json()["detail"].lower() or "opened" in resp.json()["detail"].lower()


async def test_close_position_404_when_missing(session_factory, xsmom_strategy_id, xsmom_repo):
    """POST close on non-existent id → 404."""
    xsmom_strategy_mock = MagicMock()
    app = _make_xsmom_app(
        session_factory,
        strategy_id=xsmom_strategy_id,
        xsmom_strategy=xsmom_strategy_mock,
        xsmom_repo=xsmom_repo,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        resp = await c.post("/api/xsmom/positions/99999/close")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ── POST /api/xsmom/positions/close-all ───────────────────────────────────────

async def test_close_all_returns_closed_list(session_factory, xsmom_strategy_id, xsmom_repo):
    """POST close-all closes all OPENED positions."""
    id1 = await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="BTC", state=XsmomState.OPENED.value
    )
    id2 = await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="ETH", state=XsmomState.OPENED.value
    )
    # Closed — should not be touched
    await _seed_xsmom_position(
        session_factory, strategy_id=xsmom_strategy_id, coin="SOL", state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
    )

    xsmom_strategy_mock = MagicMock()

    async def _real_close_all():
        return await xsmom_repo.list_in_state(xsmom_strategy_id, XsmomState.OPENED)

    # Simulate close_all: transition both
    async def _real_close_all_with_transitions():
        opened = await xsmom_repo.list_in_state(xsmom_strategy_id, XsmomState.OPENED)
        results = []
        for fp in opened:
            updated = await xsmom_repo.transition(
                fp.id,
                from_state=XsmomState.OPENED,
                to_state=XsmomState.CLOSE,
                state_data={**fp.state_data, "exit_decision": "close_all"},
            )
            results.append(updated)
        return results

    xsmom_strategy_mock.close_all = _real_close_all_with_transitions

    app = _make_xsmom_app(
        session_factory,
        strategy_id=xsmom_strategy_id,
        xsmom_strategy=xsmom_strategy_mock,
        xsmom_repo=xsmom_repo,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        resp = await c.post("/api/xsmom/positions/close-all")

    assert resp.status_code == 200
    data = resp.json()
    closed_ids = {item["id"] for item in data["closed"]}
    assert closed_ids == {id1, id2}
    assert "ts_ms" in data


# ── POST /api/xsmom/rebalance ─────────────────────────────────────────────────

async def test_rebalance_returns_summary(session_factory, xsmom_strategy_id, xsmom_repo):
    """POST /rebalance returns summary dict + ts_ms."""
    xsmom_strategy_mock = MagicMock()
    summary = {"kept": ["BTC"], "opened": ["ETH"], "dropped": [], "flipped": []}
    xsmom_strategy_mock.manual_rebalance = AsyncMock(return_value=summary)

    app = _make_xsmom_app(
        session_factory,
        strategy_id=xsmom_strategy_id,
        xsmom_strategy=xsmom_strategy_mock,
        xsmom_repo=xsmom_repo,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as c:
        resp = await c.post("/api/xsmom/rebalance")

    assert resp.status_code == 200
    data = resp.json()
    assert data["kept"] == ["BTC"]
    assert data["opened"] == ["ETH"]
    assert data["dropped"] == []
    assert data["flipped"] == []
    assert "ts_ms" in data

    # Verify manual_rebalance was called with now_ms kwarg
    xsmom_strategy_mock.manual_rebalance.assert_called_once()
    call_kwargs = xsmom_strategy_mock.manual_rebalance.call_args.kwargs
    assert "now_ms" in call_kwargs


# ── GET /api/xsmom/scans ──────────────────────────────────────────────────────

async def test_list_scans_returns_scans(xsmom_client, session_factory, xsmom_strategy_id):
    """GET /scans returns recorded scans most-recent-first."""
    ts1 = _now_ms() - 7200_000
    ts2 = _now_ms() - 3600_000
    await _seed_scan(session_factory, strategy_id=xsmom_strategy_id, ts_ms=ts1)
    await _seed_scan(session_factory, strategy_id=xsmom_strategy_id, ts_ms=ts2, n_long=3, n_short=3)

    resp = await xsmom_client.get("/api/xsmom/scans")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Most recent first
    assert data[0]["ts_ms"] > data[1]["ts_ms"]
    assert data[0]["ts_ms"] == ts2
    assert data[0]["n_long"] == 3


async def test_list_scans_limit(xsmom_client, session_factory, xsmom_strategy_id):
    """limit parameter is respected."""
    for i in range(5):
        await _seed_scan(
            session_factory, strategy_id=xsmom_strategy_id, ts_ms=_now_ms() - (5 - i) * 3600_000
        )

    resp = await xsmom_client.get("/api/xsmom/scans?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


# ── GET /api/xsmom/params ─────────────────────────────────────────────────────

async def test_get_params_returns_params_json(xsmom_client, session_factory, xsmom_strategy_id):
    """GET /params returns params dict and universe list."""
    resp = await xsmom_client.get("/api/xsmom/params")
    assert resp.status_code == 200
    data = resp.json()
    assert "params" in data
    assert "universe" in data
    assert data["universe"] == ["BTC", "ETH"]
    assert data["params"]["budget_cap"] == pytest.approx(500.0)


# ── PATCH /api/xsmom/params ──────────────────────────────────────────────────

async def test_patch_params_merges_valid_key(xsmom_client, session_factory, xsmom_strategy_id):
    """PATCH with valid key merges and returns params_json."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"budget_cap": 1000.0}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["restart_required"] is True
    # No engine loop wired in this fixture → no live reload.
    assert data["reloaded"] is False
    assert data["params_json"]["budget_cap"] == pytest.approx(1000.0)
    # Original keys preserved
    assert "universe" in data["params_json"]


async def test_patch_params_live_reload_when_loop_present(
    session_factory, xsmom_strategy_id, xsmom_repo
):
    """When app.state.xsmom_loop is set, PATCH live-reloads the engine."""

    class _FakeLoop:
        def __init__(self) -> None:
            self.reloaded = False

        async def reload_params_from_db(self) -> None:
            self.reloaded = True

    fake_loop = _FakeLoop()
    app = _make_xsmom_app(session_factory, strategy_id=xsmom_strategy_id, xsmom_repo=xsmom_repo)
    app.state.xsmom_loop = fake_loop
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.patch("/api/xsmom/params", json={"params": {"budget_cap": 1000.0}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["reloaded"] is True
    assert data["restart_required"] is False
    assert fake_loop.reloaded is True


async def test_patch_params_unknown_key_422(xsmom_client):
    """Unknown param key → 422."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"foo_unknown": 99}})
    assert resp.status_code == 422
    assert "foo_unknown" in resp.json()["detail"]


async def test_patch_params_invalid_n_positions_odd_422(xsmom_client):
    """n_positions must be even → 422 on odd value."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"n_positions": 3}})
    assert resp.status_code == 422
    assert "n_positions" in resp.json()["detail"].lower() or "even" in resp.json()["detail"].lower()


async def test_patch_params_invalid_n_positions_zero_422(xsmom_client):
    """n_positions must be positive → 422 on 0."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"n_positions": 0}})
    assert resp.status_code == 422


async def test_patch_params_n_positions_null_accepted(xsmom_client):
    """n_positions=null (None) is accepted (auto mode)."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"n_positions": None}})
    assert resp.status_code == 200
    assert resp.json()["params_json"]["n_positions"] is None


async def test_patch_params_valid_even_n_positions(xsmom_client):
    """n_positions=6 (positive even) is accepted."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"n_positions": 6}})
    assert resp.status_code == 200
    assert resp.json()["params_json"]["n_positions"] == 6


async def test_patch_params_invalid_budget_cap_422(xsmom_client):
    """budget_cap <= 0 → 422."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"budget_cap": -100.0}})
    assert resp.status_code == 422


async def test_patch_params_empty_universe_422(xsmom_client):
    """Empty universe list → 422."""
    resp = await xsmom_client.patch("/api/xsmom/params", json={"params": {"universe": []}})
    assert resp.status_code == 422


async def test_patch_params_universe_accepted(xsmom_client):
    """Valid universe list is accepted."""
    resp = await xsmom_client.patch(
        "/api/xsmom/params", json={"params": {"universe": ["BTC", "ETH", "SOL"]}}
    )
    assert resp.status_code == 200
    assert resp.json()["params_json"]["universe"] == ["BTC", "ETH", "SOL"]


async def test_patch_params_persisted_to_db(xsmom_client, session_factory, xsmom_strategy_id):
    """Verify params are written to the DB."""
    await xsmom_client.patch("/api/xsmom/params", json={"params": {"leverage": 2}})

    async with session_scope(session_factory) as s:
        from sqlalchemy import select
        result = await s.execute(select(Strategy).where(Strategy.id == xsmom_strategy_id))
        strat = result.scalar_one()
        assert strat.params_json["leverage"] == 2


# ── POST /api/xsmom/equity/reset ──────────────────────────────────────────────

async def test_reset_equity_sets_baseline(xsmom_client, session_factory, xsmom_strategy_id):
    """POST /equity/reset returns int baseline; GET /params surfaces it under params."""
    resp = await xsmom_client.post("/api/xsmom/equity/reset")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["equity_baseline_ms"], int)
    baseline = data["equity_baseline_ms"]

    params_resp = await xsmom_client.get("/api/xsmom/params")
    assert params_resp.status_code == 200
    params = params_resp.json()["params"]
    assert params["equity_baseline_ms"] == baseline


# ── GET /api/xsmom/summary ────────────────────────────────────────────────────

async def test_summary_with_open_positions(xsmom_client, session_factory, xsmom_strategy_id):
    """Summary reflects open long/short positions from DB."""
    await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="BTC",
        side=Side.LONG.value,
        state=XsmomState.OPENED.value,
        state_data={"notional": 1000.0},
    )
    await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="ETH",
        side=Side.SHORT.value,
        state=XsmomState.OPENED.value,
        state_data={"notional": 800.0},
    )
    # CLOSED — should not appear in totals
    await _seed_xsmom_position(
        session_factory,
        strategy_id=xsmom_strategy_id,
        coin="SOL",
        side=Side.LONG.value,
        state=XsmomState.CLOSED.value,
        closed_at=_now_ms(),
        state_data={"notional": 500.0},
    )

    resp = await xsmom_client.get("/api/xsmom/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["n_long"] == 1
    assert data["n_short"] == 1
    assert data["long_total"] == pytest.approx(1000.0)
    assert data["short_total"] == pytest.approx(800.0)
    assert data["cash"] == 0.0  # no live exchange


async def test_summary_empty_when_no_positions(xsmom_client):
    """Summary with no OPENED positions → all zeros."""
    resp = await xsmom_client.get("/api/xsmom/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["n_long"] == 0
    assert data["n_short"] == 0
    assert data["long_total"] == pytest.approx(0.0)
    assert data["short_total"] == pytest.approx(0.0)


# ── POST /api/xsmom/params/preview ───────────────────────────────────────────

async def test_preview_returns_sizing_breakdown(xsmom_client):
    """POST /params/preview returns a valid sizing breakdown dict (wallet=None branch)."""
    body = {
        "budget_cap": 1000.0,
        "n_positions": None,
        "universe": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP"],
    }
    resp = await xsmom_client.post("/api/xsmom/params/preview", json=body)
    assert resp.status_code == 200
    data = resp.json()

    # All expected keys are present
    required_keys = {
        "reserve", "effective", "book", "per_side", "long", "short",
        "k_requested", "k", "per_leg", "min_leg", "min_leg_ok", "free", "wallet",
    }
    assert required_keys.issubset(data.keys())

    # Numeric values are sensible
    assert data["reserve"] == pytest.approx(80.0)       # 8% of 1000
    assert data["effective"] == pytest.approx(1000.0)   # wallet=None → budget_cap
    assert data["book"] == pytest.approx(920.0)
    assert data["per_side"] == pytest.approx(460.0)
    assert data["k_requested"] == 2                      # 6 coins → auto tercile k=2
    assert data["k"] == 2
    assert data["per_leg"] == pytest.approx(230.0)
    assert data["min_leg_ok"] is True
    assert data["wallet"] is None                        # no live exchange in test
    assert data["free"] is None


async def test_preview_503_when_not_configured(unconfigured_client):
    """POST /params/preview returns 503 when xsmom is not configured."""
    body = {"budget_cap": 500.0, "n_positions": None, "universe": ["BTC"]}
    resp = await unconfigured_client.post("/api/xsmom/params/preview", json=body)
    assert resp.status_code == 503


async def test_preview_small_budget_min_leg_not_ok(xsmom_client):
    """Small budget yields min_leg_ok=False."""
    body = {
        "budget_cap": 30.0,
        "n_positions": None,
        "universe": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP"],
    }
    resp = await xsmom_client.post("/api/xsmom/params/preview", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_leg_ok"] is False
    assert data["per_leg"] < data["min_leg"]


async def test_preview_wallet_none_when_no_exchange(xsmom_client):
    """Without a live exchange, wallet and free are null."""
    body = {"budget_cap": 500.0, "n_positions": 4, "universe": ["BTC", "ETH", "SOL", "ADA"]}
    resp = await xsmom_client.post("/api/xsmom/params/preview", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet"] is None
    assert data["free"] is None
