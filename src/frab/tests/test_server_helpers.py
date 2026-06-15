"""Tests for Phase E server.py module-level helpers.

_pause_all_strategies  — pauses every strategy row; returns count.
_get_or_create_xsmom_strategy — creates paused row on first call; idempotent
                                 on second call (same id, loads params).
"""
from __future__ import annotations

import pytest

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.server import _pause_all_strategies, _get_or_create_xsmom_strategy
from frab.settings import Settings
from frab.strategy.xsmom.params import XsmomParams

# ── helpers ──────────────────────────────────────────────────────────────────

_CREDS = dict(
    hl_private_key="0x" + "a" * 64,
    hl_account_address="0x" + "b" * 40,
    _env_file=None,
)


def _make_settings(**kwargs) -> Settings:
    return Settings(**_CREDS, **kwargs)


async def _seed_strategy(session_factory, *, name: str, version: str, status: str) -> int:
    """Insert a Strategy row and return its id."""
    async with session_scope(session_factory) as s:
        row = StrategyRow(
            name=name,
            version=version,
            params_json={"k": 1},
            status=status,
        )
        s.add(row)
        await s.flush()
        return row.id


async def _get_status(session_factory, strategy_id: int) -> str:
    async with session_scope(session_factory) as s:
        row = await s.get(StrategyRow, strategy_id)
        return row.status


# ── _pause_all_strategies ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_all_strategies_returns_count(session_factory):
    """Two active rows → both paused, count = 2."""
    id1 = await _seed_strategy(session_factory, name="s1", version="v1", status="active")
    id2 = await _seed_strategy(session_factory, name="s2", version="v1", status="active")

    n = await _pause_all_strategies(session_factory)

    assert n == 2
    assert await _get_status(session_factory, id1) == "paused"
    assert await _get_status(session_factory, id2) == "paused"


@pytest.mark.asyncio
async def test_pause_all_strategies_already_paused(session_factory):
    """Already-paused rows are also affected; rowcount is still correct."""
    id1 = await _seed_strategy(session_factory, name="s1", version="v1", status="paused")

    n = await _pause_all_strategies(session_factory)

    assert n == 1
    assert await _get_status(session_factory, id1) == "paused"


@pytest.mark.asyncio
async def test_pause_all_strategies_empty_db(session_factory):
    """No rows → returns 0 without error."""
    n = await _pause_all_strategies(session_factory)
    assert n == 0


# ── _get_or_create_xsmom_strategy ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_or_create_xsmom_creates_paused_row(session_factory):
    """First call creates a row with status='paused'."""
    settings = _make_settings(
        xsmom_budget_cap=750.0,
        xsmom_leverage=2,
        xsmom_universe="BTC,ETH,SOL",
    )

    strategy_id, params = await _get_or_create_xsmom_strategy(session_factory, settings)

    assert isinstance(strategy_id, int)
    assert strategy_id > 0
    assert isinstance(params, XsmomParams)
    assert params.budget_cap == 750.0
    assert params.leverage == 2
    assert params.universe == ("BTC", "ETH", "SOL")

    # Row must be paused
    status = await _get_status(session_factory, strategy_id)
    assert status == "paused"


@pytest.mark.asyncio
async def test_get_or_create_xsmom_idempotent(session_factory):
    """Second call returns the same id (does not create a duplicate row)."""
    settings = _make_settings(xsmom_budget_cap=500.0)

    id1, params1 = await _get_or_create_xsmom_strategy(session_factory, settings)
    id2, params2 = await _get_or_create_xsmom_strategy(session_factory, settings)

    assert id1 == id2
    assert params1.budget_cap == params2.budget_cap


@pytest.mark.asyncio
async def test_get_or_create_xsmom_loads_existing_params(session_factory):
    """Second call loads XsmomParams from the DB (not re-applying settings defaults)."""
    settings = _make_settings(xsmom_budget_cap=300.0, xsmom_leverage=3)

    id1, _ = await _get_or_create_xsmom_strategy(session_factory, settings)

    # Mutate the row params directly to simulate an out-of-band edit
    async with session_scope(session_factory) as s:
        row = await s.get(StrategyRow, id1)
        row.params_json = {**row.params_json, "budget_cap": 999.0}

    # Second call should load 999.0 from DB, not settings default 300.0
    id2, params2 = await _get_or_create_xsmom_strategy(session_factory, settings)
    assert id2 == id1
    assert params2.budget_cap == 999.0
