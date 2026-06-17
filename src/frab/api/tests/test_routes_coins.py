"""Tests for /api/coins/* — Coin Registry settings API (Phase D).

Covers:
- GET /coins returns seeded rows.
- POST /coins adds a coin (mocked CoinDiscovery, no network), active=False.
- PATCH /coins/{coin} risk fields succeed when no open position.
- PATCH /coins/{coin} leverage/maint_ratio change with an open FarbPosition → 409.
- PATCH /coins/{coin} market-fact fields in body → 422.
- POST /coins/{coin}/active enable requires validated_at; disable always ok.
- DELETE /coins/{coin} with open position → 409; without → 204.
- Reload test: after successful mutation, app.state.coin_registry reflects the
  change without a process restart.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.coin_registry import CoinRegistry
from frab.db.models import Exchange, FarbPosition as FarbPositionRow, Strategy
from frab.db.session import session_scope
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side
from frab.repo.coin_registry_repo import CoinEntry, CoinRegistryRepo


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


async def _seed_coin(
    session_factory,
    *,
    coin: str = "BTC",
    leverage: int = 40,
    maint_ratio: float = 0.01,
    position_size_usd: float | None = None,
    active: bool = True,
    spot_token: str | None = "UBTC",
    sz_decimals: int | None = 5,
    bridge_safe: bool = True,
    validated_at: int | None = None,
) -> None:
    """Seed a coin_registry row via the repo."""
    repo = CoinRegistryRepo(session_factory)
    await repo.upsert(
        coin,
        leverage=leverage,
        maint_ratio=maint_ratio,
        position_size_usd=position_size_usd,
        active=active,
        spot_token=spot_token,
        sz_decimals=sz_decimals,
        bridge_safe=bridge_safe,
        validated_at=validated_at if validated_at is not None else _now_ms(),
    )


async def _seed_exchange(session_factory) -> int:
    async with session_scope(session_factory) as s:
        exc = Exchange(name="hyperliquid", funding_interval_h=1, spot_taker_bps=2.5, perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        return exc.id


async def _seed_strategy(session_factory) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(name="two_phase", version="v2", params_json={}, status="active")
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_open_farb_position(
    session_factory,
    *,
    strategy_id: int,
    coin: str = "BTC",
    state: FarbState = FarbState.PRE_BREAKEVEN,
) -> int:
    """Seed a non-terminal FarbPosition (simulates an open arb trade)."""
    async with session_scope(session_factory) as s:
        fp = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=state.value,
            state_data={},
            opened_at=_now_ms(),
            closed_at=None,
        )
        s.add(fp)
        await s.flush()
        return fp.id


def _make_fake_discovery(entry: CoinEntry) -> AsyncMock:
    """Return an AsyncMock for CoinDiscovery with validate_and_register returning entry."""
    mock = AsyncMock()
    mock.validate_and_register = AsyncMock(return_value=entry)
    return mock


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def coin_registry(session_factory) -> CoinRegistry:
    registry = CoinRegistry(session_factory)
    await registry.load()
    return registry


def _make_app(session_factory, *, coin_registry=None, coin_discovery=None):
    """Build a test FastAPI app with optional registry/discovery on app.state."""
    from frab.repo.farb_repo import FarbRepo
    farb_repo = FarbRepo(session_factory)
    app = create_app(session_factory, farb_repo=farb_repo)
    if coin_registry is not None:
        app.state.coin_registry = coin_registry
    if coin_discovery is not None:
        app.state.coin_discovery = coin_discovery
    return app


@pytest_asyncio.fixture
async def api_client(session_factory, coin_registry):
    """API client with a real CoinRegistry on app.state (for reload tests)."""
    app = _make_app(session_factory, coin_registry=coin_registry)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        yield client, app


# ── GET /coins ────────────────────────────────────────────────────────────────

async def test_list_coins_empty(api_client):
    client, _ = api_client
    resp = await client.get("/api/coins")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_coins_returns_seeded_rows(api_client, session_factory):
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC", leverage=40)
    await _seed_coin(session_factory, coin="ETH", leverage=20)

    resp = await client.get("/api/coins")
    assert resp.status_code == 200
    coins = {row["coin"]: row for row in resp.json()}
    assert "BTC" in coins
    assert "ETH" in coins
    assert coins["BTC"]["leverage"] == 40
    assert coins["ETH"]["leverage"] == 20
    assert coins["BTC"]["spot_token"] == "UBTC"


# ── POST /coins ───────────────────────────────────────────────────────────────

async def test_post_coins_adds_coin_active_false(session_factory, coin_registry):
    """POST /coins calls CoinDiscovery and creates row with active=False."""
    fake_entry = CoinEntry(
        coin="SOL",
        leverage=20,
        maint_ratio=0.025,
        position_size_usd=None,
        active=False,
        spot_token="USOL",
        sz_decimals=2,
        bridge_safe=True,
        validated_at=_now_ms(),
    )
    discovery = _make_fake_discovery(fake_entry)
    app = _make_app(session_factory, coin_registry=coin_registry, coin_discovery=discovery)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.post(
            "/api/coins",
            json={"coin": "SOL", "leverage": 20, "maint_ratio": 0.025},
        )

    assert resp.status_code == 201, resp.json()
    data = resp.json()
    assert data["coin"] == "SOL"
    assert data["active"] is False
    assert data["spot_token"] == "USOL"
    assert data["validated_at"] is not None

    # Verify CoinDiscovery.validate_and_register was called with correct args
    discovery.validate_and_register.assert_awaited_once_with(
        "SOL",
        leverage=20,
        maint_ratio=0.025,
        position_size_usd=None,
        active=False,
    )


async def test_post_coins_discovery_failure_returns_422(session_factory, coin_registry):
    """If HL discovery raises ValueError, POST /coins returns 422."""
    discovery = AsyncMock()
    discovery.validate_and_register = AsyncMock(
        side_effect=ValueError("Coin 'FAKECOIN' not found in HL perp meta")
    )
    app = _make_app(session_factory, coin_registry=coin_registry, coin_discovery=discovery)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.post(
            "/api/coins",
            json={"coin": "FAKECOIN", "leverage": 5, "maint_ratio": 0.025},
        )

    assert resp.status_code == 422
    assert "not found in HL perp meta" in resp.json()["detail"]


async def test_post_coins_no_discovery_returns_503(session_factory):
    """POST /coins without coin_discovery or exchange on app.state → 503."""
    app = _make_app(session_factory)  # no discovery, no exchange

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        resp = await client.post(
            "/api/coins",
            json={"coin": "SOL", "leverage": 20, "maint_ratio": 0.025},
        )

    assert resp.status_code == 503


# ── PATCH /coins/{coin} ───────────────────────────────────────────────────────

async def test_patch_coin_risk_fields_no_open_position(api_client, session_factory):
    """PATCH risk fields succeeds when there is no open FarbPosition."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC", leverage=40, maint_ratio=0.01)

    resp = await client.patch(
        "/api/coins/BTC",
        json={"leverage": 30, "maint_ratio": 0.015},
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["leverage"] == 30
    assert data["maint_ratio"] == pytest.approx(0.015)
    # Market-fact fields preserved
    assert data["spot_token"] == "UBTC"
    assert data["sz_decimals"] == 5


async def test_patch_coin_leverage_with_open_position_returns_409(api_client, session_factory):
    """PATCH leverage with an open FarbPosition → 409 Conflict."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC", leverage=40)
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"leverage": 20},
    )
    assert resp.status_code == 409
    assert "non-terminal FarbPosition" in resp.json()["detail"]
    assert "leverage" in resp.json()["detail"]


async def test_patch_coin_maint_ratio_with_open_position_returns_409(api_client, session_factory):
    """PATCH maint_ratio with an open FarbPosition → 409 Conflict."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="ETH", leverage=20, maint_ratio=0.025, spot_token="UETH")
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="ETH")

    resp = await client.patch(
        "/api/coins/ETH",
        json={"maint_ratio": 0.05},
    )
    assert resp.status_code == 409
    assert "non-terminal FarbPosition" in resp.json()["detail"]


async def test_patch_coin_position_size_no_guard(api_client, session_factory):
    """PATCH position_size_usd-only is NOT guarded — allowed even with open position."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC", leverage=40)
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"position_size_usd": 500.0},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["position_size_usd"] == pytest.approx(500.0)
    # leverage unchanged
    assert resp.json()["leverage"] == 40


async def test_patch_coin_market_fact_fields_rejected(api_client, session_factory):
    """PATCH with market-fact fields in body → 422."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"spot_token": "EVIL", "leverage": 40},
    )
    assert resp.status_code == 422
    assert "Market-fact fields are not editable" in resp.json()["detail"]
    assert "spot_token" in resp.json()["detail"]


async def test_patch_coin_validated_at_rejected(api_client, session_factory):
    """PATCH with validated_at in body → 422."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"validated_at": 1234567890},
    )
    assert resp.status_code == 422
    assert "Market-fact fields" in resp.json()["detail"]


async def test_patch_coin_active_rejected(api_client, session_factory):
    """PATCH with 'active' in body → 422 (must use POST /active, which gates on validated_at)."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"active": True},
    )
    assert resp.status_code == 422
    assert "active" in resp.json()["detail"]
    assert "/active" in resp.json()["detail"]


async def test_patch_coin_not_found(api_client):
    """PATCH non-existent coin → 404."""
    client, _ = api_client
    resp = await client.patch(
        "/api/coins/GHOST",
        json={"leverage": 5},
    )
    assert resp.status_code == 404


async def test_patch_coin_same_leverage_with_open_position_allowed(api_client, session_factory):
    """PATCH with same leverage value (no actual change) is NOT guarded.

    The guard only fires when the value is actually changing.
    """
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC", leverage=40)
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="BTC")

    resp = await client.patch(
        "/api/coins/BTC",
        json={"leverage": 40},  # same value — no change
    )
    assert resp.status_code == 200, resp.json()


# ── POST /coins/{coin}/active ─────────────────────────────────────────────────

async def test_set_active_enable_with_validated_at_ok(api_client, session_factory):
    """Enabling a coin that has validated_at set → 200."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC", active=False, validated_at=_now_ms())

    resp = await client.post("/api/coins/BTC/active", json={"active": True})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["active"] is True


async def test_set_active_enable_without_validated_at_returns_409(api_client, session_factory):
    """Enabling a coin with validated_at=NULL → 409."""
    client, _ = api_client
    repo = CoinRegistryRepo(session_factory)
    await repo.upsert(
        "BTC",
        leverage=40,
        maint_ratio=0.01,
        position_size_usd=None,
        active=False,
        spot_token=None,
        sz_decimals=None,
        bridge_safe=False,
        validated_at=None,   # not validated
    )

    resp = await client.post("/api/coins/BTC/active", json={"active": True})
    assert resp.status_code == 409
    assert "validated_at is NULL" in resp.json()["detail"]


async def test_set_active_disable_always_allowed(api_client, session_factory):
    """Disabling is always allowed (no validated_at requirement, no position guard)."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC", active=True)
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="BTC")

    resp = await client.post("/api/coins/BTC/active", json={"active": False})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["active"] is False


async def test_set_active_coin_not_found(api_client):
    """POST /active on non-existent coin → 404."""
    client, _ = api_client
    resp = await client.post("/api/coins/GHOST/active", json={"active": True})
    assert resp.status_code == 404


# ── DELETE /coins/{coin} ──────────────────────────────────────────────────────

async def test_delete_coin_no_open_position(api_client, session_factory):
    """DELETE with no open position → 204."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC")

    resp = await client.delete("/api/coins/BTC")
    assert resp.status_code == 204

    # Confirm it is gone
    resp2 = await client.get("/api/coins")
    assert not any(r["coin"] == "BTC" for r in resp2.json())


async def test_delete_coin_with_open_position_returns_409(api_client, session_factory):
    """DELETE with a non-terminal FarbPosition → 409."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC")
    await _seed_open_farb_position(session_factory, strategy_id=strategy_id, coin="BTC")

    resp = await client.delete("/api/coins/BTC")
    assert resp.status_code == 409
    assert "non-terminal FarbPosition" in resp.json()["detail"]


async def test_delete_coin_with_closed_position_allowed(api_client, session_factory):
    """DELETE is allowed when only CLOSED/FAILED positions exist (terminal = no open)."""
    client, _ = api_client
    strategy_id = await _seed_strategy(session_factory)
    await _seed_coin(session_factory, coin="BTC")
    # Seed a CLOSED position (terminal — not guarded)
    await _seed_open_farb_position(
        session_factory,
        strategy_id=strategy_id,
        coin="BTC",
        state=FarbState.CLOSED,
    )

    resp = await client.delete("/api/coins/BTC")
    assert resp.status_code == 204


async def test_delete_coin_not_found(api_client):
    """DELETE non-existent coin → 404."""
    client, _ = api_client
    resp = await client.delete("/api/coins/GHOST")
    assert resp.status_code == 404


# ── Cache-reload test ─────────────────────────────────────────────────────────

async def test_registry_reloads_after_patch(api_client, session_factory):
    """After a successful PATCH, app.state.coin_registry reflects the change.

    This test proves that the in-memory CoinRegistry snapshot is reloaded by
    the mutation endpoint — no process restart needed to see the new value.
    """
    client, app = api_client
    await _seed_coin(session_factory, coin="BTC", leverage=40, maint_ratio=0.01)
    # Reload registry so it picks up the seeded row
    await app.state.coin_registry.reload()

    # Before: leverage == 40
    spec_before = app.state.coin_registry.get_coin_spec("BTC")
    assert spec_before.leverage == 40

    # Mutate via API
    resp = await client.patch("/api/coins/BTC", json={"leverage": 25})
    assert resp.status_code == 200, resp.json()

    # After: registry in-memory snapshot reflects the new value without restart
    spec_after = app.state.coin_registry.get_coin_spec("BTC")
    assert spec_after.leverage == 25, (
        f"Expected leverage=25 after PATCH + reload, got {spec_after.leverage}. "
        "The registry was not reloaded by the mutation endpoint."
    )


async def test_registry_reloads_after_add(session_factory, coin_registry):
    """After POST /coins, app.state.coin_registry.universe() includes the new coin
    once it is activated (validated_at is set by CoinDiscovery).

    Proves no-stale-snapshot: mutation → reload happens → accessor reflects change.

    The mock CoinDiscovery both returns a CoinEntry AND persists the row to the
    repo (simulating the real validate_and_register behavior), so the reload can
    find the row in the DB.
    """
    repo = CoinRegistryRepo(session_factory)
    validated_at = _now_ms()

    async def _fake_validate_and_register(coin, *, leverage, maint_ratio, position_size_usd, active):
        # Persist to DB so reload() can find the row
        return await repo.upsert(
            coin,
            leverage=leverage,
            maint_ratio=maint_ratio,
            position_size_usd=position_size_usd,
            active=active,
            spot_token="USOL",
            sz_decimals=2,
            bridge_safe=True,
            validated_at=validated_at,
        )

    discovery = AsyncMock()
    discovery.validate_and_register = AsyncMock(side_effect=_fake_validate_and_register)

    app = _make_app(session_factory, coin_registry=coin_registry, coin_discovery=discovery)

    # Universe before: empty
    assert "SOL" not in coin_registry.universe()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        # Add coin (active=False)
        resp = await client.post(
            "/api/coins",
            json={"coin": "SOL", "leverage": 20, "maint_ratio": 0.025},
        )
        assert resp.status_code == 201, resp.json()

        # coin_registry is reloaded by POST → spec is available even though active=False
        spec = coin_registry.get_coin_spec("SOL")
        assert spec.leverage == 20

        # Not in universe yet (active=False)
        assert "SOL" not in coin_registry.universe()

        # Activate it
        resp2 = await client.post("/api/coins/SOL/active", json={"active": True})
        assert resp2.status_code == 200, resp2.json()

    # After activation + reload: coin appears in universe
    assert "SOL" in coin_registry.universe()


async def test_registry_reloads_after_delete(api_client, session_factory):
    """After DELETE, the coin is absent from app.state.coin_registry."""
    client, app = api_client
    await _seed_coin(session_factory, coin="BTC")
    await app.state.coin_registry.reload()

    # Coin present before
    spec = app.state.coin_registry.get_coin_spec("BTC")
    assert spec.leverage == 40

    # Delete it
    resp = await client.delete("/api/coins/BTC")
    assert resp.status_code == 204

    # Registry no longer has it
    with pytest.raises(KeyError):
        app.state.coin_registry.get_coin_spec("BTC")


# ── Case-normalisation ────────────────────────────────────────────────────────

async def test_patch_lowercase_coin_is_normalised(api_client, session_factory):
    """PATCH /coins/btc (lowercase) normalises to BTC."""
    client, _ = api_client
    await _seed_coin(session_factory, coin="BTC", leverage=40)

    resp = await client.patch("/api/coins/btc", json={"leverage": 15})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["coin"] == "BTC"
    assert resp.json()["leverage"] == 15
