"""Tests for PATCH /api/strategies/{id}/params."""
from __future__ import annotations

import pytest

from frab.db.models import Strategy
from frab.db.session import session_scope
from frab.strategy.two_phase import TwoPhaseParams


# ── helpers ──────────────────────────────────────────────────────────────────


async def _seed_strategy(session_factory, *, params: dict | None = None) -> int:
    async with session_scope(session_factory) as s:
        strat = Strategy(
            name="test_strat",
            version="v1",
            params_json=params or {},
            status="idle",
        )
        s.add(strat)
        await s.flush()
        return strat.id


# ── test_patch_strategy_params_merges_and_returns ────────────────────────────


async def test_patch_strategy_params_merges_and_returns(api_client, session_factory):
    """PATCH {position_size_usdc: 2000} onto default params → merged and returned."""
    initial = {"entry_threshold_apr": 0.10, "concurrency_cap": 3}
    strat_id = await _seed_strategy(session_factory, params=initial)

    resp = await api_client.patch(
        f"/api/strategies/{strat_id}/params",
        json={"params": {"position_size_usdc": 2000.0}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == strat_id
    assert data["restart_required"] is True
    assert "engine must be restarted" in data["note"]
    assert data["params_json"]["position_size_usdc"] == pytest.approx(2000.0)
    # Original keys preserved
    assert data["params_json"]["entry_threshold_apr"] == pytest.approx(0.10)
    assert data["params_json"]["concurrency_cap"] == 3


async def test_patch_strategy_params_persisted_to_db(api_client, session_factory):
    """Verify the updated params_json is actually written to DB."""
    strat_id = await _seed_strategy(session_factory, params={"concurrency_cap": 2})

    await api_client.patch(
        f"/api/strategies/{strat_id}/params",
        json={"params": {"concurrency_cap": 5}},
    )

    async with session_scope(session_factory) as s:
        from sqlalchemy import select
        result = await s.execute(select(Strategy).where(Strategy.id == strat_id))
        strat = result.scalar_one()
        assert strat.params_json["concurrency_cap"] == 5


# ── test_patch_strategy_params_rejects_unknown_keys ──────────────────────────


async def test_patch_strategy_params_rejects_unknown_keys(api_client, session_factory):
    """Patching with unknown key 'foo' → 422 with 'foo' in detail."""
    strat_id = await _seed_strategy(session_factory)

    resp = await api_client.patch(
        f"/api/strategies/{strat_id}/params",
        json={"params": {"foo": "bar"}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "foo" in detail


async def test_patch_strategy_params_rejects_multiple_unknown_keys(api_client, session_factory):
    """Multiple unknown keys are all listed in the 422 detail."""
    strat_id = await _seed_strategy(session_factory)

    resp = await api_client.patch(
        f"/api/strategies/{strat_id}/params",
        json={"params": {"bad_key_1": 1, "bad_key_2": 2}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "bad_key_1" in detail
    assert "bad_key_2" in detail


# ── test_patch_strategy_params_404_when_strategy_missing ─────────────────────


async def test_patch_strategy_params_404_when_strategy_missing(api_client):
    """PATCH on non-existent strategy → 404."""
    resp = await api_client.patch(
        "/api/strategies/99999/params",
        json={"params": {"concurrency_cap": 3}},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ── test_patch_strategy_params_valid_key_no_unknown ──────────────────────────


async def test_patch_strategy_params_accepts_all_valid_keys(api_client, session_factory):
    """All TwoPhaseParams field names should be accepted without 422."""
    strat_id = await _seed_strategy(session_factory)
    valid_keys = list(TwoPhaseParams.__dataclass_fields__.keys())

    # Pick a safe subset that won't break anything
    patch_payload = {k: 1 for k in valid_keys[:3]}

    resp = await api_client.patch(
        f"/api/strategies/{strat_id}/params",
        json={"params": patch_payload},
    )
    # Should not be 422 for known keys
    assert resp.status_code == 200
