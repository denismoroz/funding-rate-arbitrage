"""Tests for GET /api/equity/margin."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from frab.db.models import Event as DbEvent, Strategy
from frab.db.session import session_scope
from frab.domain.exchange import Exchange as DomainExchange
from frab.domain.portfolio import Portfolio
from frab.domain.position import Position as DomainPosition
from frab.domain.wallet import WalletInfo
from frab.engine.margin_manager import MarginManager, PerCoinSpec


def _utc() -> datetime:
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_strategy(session_factory, *, name: str) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name=name, version="v1", params_json={}, status="running")
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_margin_event(
    session_factory,
    *,
    kind: str = "margin.top_up",
    level: str = "WARNING",
    coin: str | None = "BTC",
    amount: float = 4.2,
    ratio: float = 1.31,
) -> None:
    async with session_scope(session_factory) as s:
        ev = DbEvent(
            ts=_utc(),
            level=level,
            source="margin_watchdog",
            kind=kind,
            message=f"{kind} {coin}" if coin else kind,
            payload_json={"coin": coin, "amount_transferred": amount, "ratio": ratio},
        )
        s.add(ev)


def _build_manager() -> MarginManager:
    return MarginManager(
        per_coin_params={"BTC": PerCoinSpec(
            position_size_usd=100.0, leverage=5, maint_ratio=0.05,
        )},
        margin_buffer_x=1.5,
        top_up_trigger=1.5,
        healthy_ratio=2.0,
        budget_cap_usd=1000.0,
    )


def _fake_quote(mark: float):
    return SimpleNamespace(mark=mark)


def _fake_position(spot_qty: float, perp_qty: float, entry_spot: float, entry_perp: float):
    return SimpleNamespace(
        spot_qty=spot_qty, perp_qty=perp_qty,
        entry_spot_price=entry_spot, entry_perp_price=entry_perp,
        opened_at=_utc(), funding_collected=0.0, fees_paid=0.0,
    )


def _make_portfolio_service(
    *,
    perp_cash: float = 0.0,
    positions: tuple[DomainPosition, ...] = (),
) -> MagicMock:
    """Build a MagicMock portfolio_service whose current() returns a Portfolio."""
    wallet = WalletInfo(
        exchange=DomainExchange.HYPERLIQUID,
        available_usdc=0.0,
        reserved_usdc=perp_cash,
        total_value_usd=perp_cash,
    )
    portfolio = Portfolio(
        ts=_utc(),
        positions=positions,
        wallet_per_exchange={DomainExchange.HYPERLIQUID: wallet},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    ps = MagicMock()
    ps.current = AsyncMock(return_value=portfolio)
    return ps


def _make_domain_position(
    coin: str = "BTC",
    notional_usd: float = 100.0,
    margin_reserve_usd: float = 30.0,
) -> DomainPosition:
    return DomainPosition(
        exchange=DomainExchange.HYPERLIQUID,
        coin=coin,
        spot_qty=1.0,
        perp_qty=1.0,
        notional_usd=notional_usd,
        margin_reserve_usd=margin_reserve_usd,
        entry_spot_price=100.0,
        entry_perp_price=100.0,
        opened_at=_utc(),
    )


def _fake_strategy_with_position(mgr: MarginManager, mark: float = 100.0):
    from frab.engine.margin_manager import OpenPosition

    strat = MagicMock()
    strat._margin_manager = mgr
    strat._last_quotes = {"BTC": _fake_quote(mark)}
    strat._params = SimpleNamespace(concurrency_cap=3)
    strat.n_skipped_opens_capital = 0
    # _open_position_snapshots_for_manager returns OpenPosition list
    strat._open_position_snapshots_for_manager = MagicMock(return_value=[
        OpenPosition(
            coin="BTC", spot_units=1.0, short_size=1.0,
            entry_perp_price=100.0,
            required_margin=mgr.compute_required_margin_for_open("BTC"),
        )
    ])
    return strat


# ─── Test 1: no MarginManager → enabled=False, raw fields null ────────────────

async def test_margin_status_returns_disabled_when_no_manager(
    session_factory, api_client_with_executor
):
    strategy_id = await _seed_strategy(session_factory, name="no_mgr")
    strat = MagicMock()
    strat._margin_manager = None
    strat._params = SimpleNamespace(concurrency_cap=3)
    ps = _make_portfolio_service(perp_cash=0.0, positions=())
    client = await api_client_with_executor(
        None, strategy=strat, strategy_id=strategy_id, portfolio_service=ps,
    )

    resp = await client.get(f"/api/equity/margin?strategy_id={strategy_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["margin_manager_enabled"] is False
    assert data["top_up_trigger"] is None
    assert data["healthy_ratio"] is None
    assert data["margin_ratio"] is None
    assert data["budget_cap_usd"] is None
    assert data["n_open_positions"] == 0
    assert data["concurrency_cap"] == 3
    assert data["last_event"] is None


# ─── Test 2: 503 when engine not running for this strategy ─────────────────────

async def test_margin_status_503_when_engine_not_running(
    session_factory, api_client_with_executor
):
    strategy_id = await _seed_strategy(session_factory, name="not_running")
    client = await api_client_with_executor(None, strategy=None, strategy_id=None)
    resp = await client.get(f"/api/equity/margin?strategy_id={strategy_id}")
    assert resp.status_code == 503


# ─── Test 3: enabled + healthy ratio + no event ────────────────────────────────

async def test_margin_status_enabled_and_healthy(
    session_factory, api_client_with_executor
):
    mgr = _build_manager()
    strat = _fake_strategy_with_position(mgr, mark=100.0)
    strategy_id = await _seed_strategy(session_factory, name="healthy")
    ps = _make_portfolio_service(
        perp_cash=30.0,
        positions=(_make_domain_position(notional_usd=100.0, margin_reserve_usd=30.0),),
    )

    client = await api_client_with_executor(
        None, strategy=strat, strategy_id=strategy_id, portfolio_service=ps,
    )
    resp = await client.get(f"/api/equity/margin?strategy_id={strategy_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["margin_manager_enabled"] is True
    # mark=100, qty=1 → maint = 1*100*0.05 = 5, unrealized = 0
    assert data["total_maintenance"] == pytest.approx(5.0)
    assert data["perp_unrealized"] == pytest.approx(0.0)
    assert data["perp_cash"] == pytest.approx(30.0)
    assert data["effective_equity"] == pytest.approx(30.0)
    assert data["margin_ratio"] == pytest.approx(6.0)
    assert data["top_up_trigger"] == 1.5
    assert data["healthy_ratio"] == 2.0
    assert data["budget_cap_usd"] == 1000.0
    # budget_committed = spot_qty*entry + perp_cash = 100 + 30 = 130
    assert data["budget_committed"] == pytest.approx(130.0)
    assert data["n_open_positions"] == 1
    assert data["concurrency_cap"] == 3
    assert data["n_skipped_opens_capital"] == 0
    assert data["last_event"] is None


# ─── Test 4: enabled + adverse move shows ratio in TOP_UP band ─────────────────

async def test_margin_status_enabled_adverse_move(
    session_factory, api_client_with_executor
):
    mgr = _build_manager()
    strat = _fake_strategy_with_position(mgr, mark=122.0)
    strategy_id = await _seed_strategy(session_factory, name="adverse")
    ps = _make_portfolio_service(
        perp_cash=30.0,
        positions=(_make_domain_position(notional_usd=100.0, margin_reserve_usd=30.0),),
    )

    client = await api_client_with_executor(
        None, strategy=strat, strategy_id=strategy_id, portfolio_service=ps,
    )
    resp = await client.get(f"/api/equity/margin?strategy_id={strategy_id}")

    assert resp.status_code == 200
    data = resp.json()
    # mark=122, qty=1 → maint = 122*0.05 = 6.1; unrealized = 1*(100-122) = -22
    # effective = 30-22 = 8; ratio = 8/6.1 ≈ 1.31
    assert data["total_maintenance"] == pytest.approx(6.1)
    assert data["perp_unrealized"] == pytest.approx(-22.0)
    assert data["effective_equity"] == pytest.approx(8.0)
    assert data["margin_ratio"] == pytest.approx(8.0 / 6.1)


# ─── Test 5: last_event populated from DB ──────────────────────────────────────

async def test_margin_status_last_event_present(
    session_factory, api_client_with_executor
):
    mgr = _build_manager()
    strat = _fake_strategy_with_position(mgr, mark=100.0)
    strategy_id = await _seed_strategy(session_factory, name="with_event")
    await _seed_margin_event(
        session_factory, kind="margin.top_up", level="WARNING",
        coin="BTC", amount=4.2, ratio=1.31,
    )
    ps = _make_portfolio_service(
        perp_cash=30.0,
        positions=(_make_domain_position(notional_usd=100.0, margin_reserve_usd=30.0),),
    )

    client = await api_client_with_executor(
        None, strategy=strat, strategy_id=strategy_id, portfolio_service=ps,
    )
    resp = await client.get(f"/api/equity/margin?strategy_id={strategy_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["last_event"] is not None
    assert data["last_event"]["kind"] == "margin.top_up"
    assert data["last_event"]["level"] == "WARNING"
    assert data["last_event"]["coin"] == "BTC"
    assert data["last_event"]["amount_transferred"] == pytest.approx(4.2)
    assert data["last_event"]["ratio"] == pytest.approx(1.31)
