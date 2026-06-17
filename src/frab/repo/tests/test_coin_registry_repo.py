"""Unit tests for CoinRegistryRepo — CRUD round-trips + guard helpers."""
import pytest

from frab.db.models import FarbPosition as FarbPositionRow, Strategy
from frab.db.session import session_scope
from frab.domain.enums import FarbState
from frab.repo.coin_registry_repo import CoinEntry, CoinRegistryRepo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo(session_factory) -> CoinRegistryRepo:
    return CoinRegistryRepo(session_factory)


_BTC_DEFAULTS = dict(
    leverage=40,
    maint_ratio=0.01,
    position_size_usd=None,
    active=True,
    spot_token="UBTC",
    sz_decimals=None,
    bridge_safe=True,
    validated_at=1_700_000_000_000,
)


# ---------------------------------------------------------------------------
# 1. upsert (insert) + get round-trip
# ---------------------------------------------------------------------------

async def test_upsert_insert_and_get(session_factory):
    repo = _repo(session_factory)
    entry = await repo.upsert("BTC", **_BTC_DEFAULTS)

    assert entry.coin == "BTC"
    assert entry.leverage == 40
    assert entry.maint_ratio == 0.01
    assert entry.position_size_usd is None
    assert entry.active is True
    assert entry.spot_token == "UBTC"
    assert entry.sz_decimals is None
    assert entry.bridge_safe is True
    assert entry.validated_at == 1_700_000_000_000

    fetched = await repo.get("BTC")
    assert fetched is not None
    assert fetched == entry


async def test_upsert_update_replaces_fields(session_factory):
    repo = _repo(session_factory)
    await repo.upsert("BTC", **_BTC_DEFAULTS)

    # Update leverage and maint_ratio
    updated = await repo.upsert(
        "BTC",
        leverage=20,
        maint_ratio=0.02,
        position_size_usd=50.0,
        active=False,
        spot_token="UBTC",
        sz_decimals=4,
        bridge_safe=True,
        validated_at=1_800_000_000_000,
    )

    assert updated.leverage == 20
    assert updated.maint_ratio == 0.02
    assert updated.position_size_usd == 50.0
    assert updated.active is False
    assert updated.sz_decimals == 4
    assert updated.validated_at == 1_800_000_000_000


# ---------------------------------------------------------------------------
# 2. get returns None for unknown coin
# ---------------------------------------------------------------------------

async def test_get_returns_none_for_unknown_coin(session_factory):
    repo = _repo(session_factory)
    result = await repo.get("NONEXISTENT")
    assert result is None


# ---------------------------------------------------------------------------
# 3. list — returns all rows ordered by coin
# ---------------------------------------------------------------------------

async def test_list_returns_all_rows_ordered(session_factory):
    repo = _repo(session_factory)
    await repo.upsert("SOL", leverage=20, maint_ratio=0.025, active=True, bridge_safe=True,
                      spot_token="USOL", position_size_usd=None, sz_decimals=None, validated_at=None)
    await repo.upsert("BTC", **_BTC_DEFAULTS)
    await repo.upsert("ETH", leverage=25, maint_ratio=0.01, active=True, bridge_safe=True,
                      spot_token="UETH", position_size_usd=None, sz_decimals=None, validated_at=None)

    entries = await repo.list()

    assert [e.coin for e in entries] == ["BTC", "ETH", "SOL"]


async def test_list_empty_db(session_factory):
    repo = _repo(session_factory)
    entries = await repo.list()
    assert entries == []


# ---------------------------------------------------------------------------
# 4. set_active toggles the flag
# ---------------------------------------------------------------------------

async def test_set_active_false(session_factory):
    repo = _repo(session_factory)
    await repo.upsert("BTC", **_BTC_DEFAULTS)  # active=True

    updated = await repo.set_active("BTC", False)
    assert updated.active is False

    fetched = await repo.get("BTC")
    assert fetched is not None
    assert fetched.active is False


async def test_set_active_true(session_factory):
    repo = _repo(session_factory)
    await repo.upsert("BTC", **{**_BTC_DEFAULTS, "active": False})

    updated = await repo.set_active("BTC", True)
    assert updated.active is True


async def test_set_active_raises_for_unknown_coin(session_factory):
    repo = _repo(session_factory)
    with pytest.raises(KeyError, match="UNKNOWN"):
        await repo.set_active("UNKNOWN", True)


# ---------------------------------------------------------------------------
# 5. delete removes the row
# ---------------------------------------------------------------------------

async def test_delete_removes_row(session_factory):
    repo = _repo(session_factory)
    await repo.upsert("BTC", **_BTC_DEFAULTS)

    await repo.delete("BTC")

    assert await repo.get("BTC") is None


async def test_delete_raises_for_unknown_coin(session_factory):
    repo = _repo(session_factory)
    with pytest.raises(KeyError, match="MISSING"):
        await repo.delete("MISSING")


# ---------------------------------------------------------------------------
# 6. has_open_position guard
# ---------------------------------------------------------------------------

async def _seed_strategy(session_factory) -> int:
    """Insert a Strategy row and return its id."""
    async with session_scope(session_factory) as s:
        strat = Strategy(name="test_strategy", version="v1", params_json={"k": 3})
        s.add(strat)
        await s.flush()
        return strat.id


async def _seed_farb_position(session_factory, strategy_id: int, coin: str, state: FarbState) -> int:
    """Insert a FarbPosition row directly and return its id."""
    import time
    now_ms = int(time.time() * 1000)
    async with session_scope(session_factory) as s:
        row = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=state,
            state_data={},
            spot_position_id=None,
            perp_position_id=None,
            margin_position_id=None,
            opened_at=now_ms,
            closed_at=None,
        )
        s.add(row)
        await s.flush()
        return row.id


async def test_has_open_position_true_for_pre_breakeven(session_factory):
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "BTC", FarbState.PRE_BREAKEVEN)

    assert await repo.has_open_position("BTC") is True


async def test_has_open_position_true_for_post_breakeven(session_factory):
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "ETH", FarbState.POST_BREAKEVEN)

    assert await repo.has_open_position("ETH") is True


async def test_has_open_position_true_for_transient_opening_state(session_factory):
    """Any transient (non-terminal) state counts as open."""
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "SOL", FarbState.OPENING_LONG)

    assert await repo.has_open_position("SOL") is True


async def test_has_open_position_false_for_closed(session_factory):
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "BTC", FarbState.CLOSED)

    assert await repo.has_open_position("BTC") is False


async def test_has_open_position_false_for_failed(session_factory):
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "BTC", FarbState.FAILED)

    assert await repo.has_open_position("BTC") is False


async def test_has_open_position_false_when_no_positions(session_factory):
    repo = _repo(session_factory)

    assert await repo.has_open_position("BTC") is False


async def test_has_open_position_false_when_coin_differs(session_factory):
    """Position on ETH does not count for BTC."""
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "ETH", FarbState.PRE_BREAKEVEN)

    assert await repo.has_open_position("BTC") is False


async def test_has_open_position_mixed_closed_and_open(session_factory):
    """Closed position + open position for same coin → True."""
    repo = _repo(session_factory)
    strategy_id = await _seed_strategy(session_factory)
    await _seed_farb_position(session_factory, strategy_id, "BTC", FarbState.CLOSED)
    await _seed_farb_position(session_factory, strategy_id, "BTC", FarbState.PRE_BREAKEVEN)

    assert await repo.has_open_position("BTC") is True
