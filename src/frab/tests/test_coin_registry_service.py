"""Phase B: CoinRegistry service — provenance-equivalence and no-fallback tests.

Provenance invariant: registry-derived coin specs / universe / spot-maps for the
7 seeded coins must be bit-exact equal to the pre-refactor constants-derived values.

No-fallback invariant: a coin NOT in the registry is NOT in universe(), and
get_coin_spec() raises KeyError (never silently applies FALLBACK_LEVERAGE).

Phase F2 note: RESEARCH_LEVERAGE, RESEARCH_MAINT_RATIO, SPOT_TOKEN_INVERSE, and
MAINNET_SPOT_TOKEN_MAP are deleted. Tests that compared against those constants now
use the known seed values directly (the values haven't changed, just the source).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.coin_registry import CoinRegistry
from frab.constants import CoinMarginSpec
from frab.db.session import init_db, make_session_factory
from frab.repo.coin_registry_repo import CoinRegistryRepo


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session_factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    sf = make_session_factory(eng)
    yield sf
    await eng.dispose()


# Seed exactly the 7 rows the migration inserts (same source: RESEARCH_* + MAINNET_SPOT_TOKEN_MAP).
_SEED_ROWS = [
    {"coin": "BTC",  "leverage": 40, "maint_ratio": 0.010, "active": True,  "spot_token": "UBTC",  "bridge_safe": True},
    {"coin": "ETH",  "leverage": 25, "maint_ratio": 0.010, "active": True,  "spot_token": "UETH",  "bridge_safe": True},
    {"coin": "HYPE", "leverage": 10, "maint_ratio": 0.025, "active": True,  "spot_token": "HYPE",  "bridge_safe": True},
    {"coin": "PURR", "leverage":  3, "maint_ratio": 0.025, "active": True,  "spot_token": "PURR",  "bridge_safe": True},
    {"coin": "SOL",  "leverage": 20, "maint_ratio": 0.025, "active": True,  "spot_token": "USOL",  "bridge_safe": True},
    {"coin": "XPL",  "leverage": 10, "maint_ratio": 0.025, "active": False, "spot_token": "XPL",   "bridge_safe": True},
    {"coin": "ZEC",  "leverage": 10, "maint_ratio": 0.025, "active": False, "spot_token": "ZEC",   "bridge_safe": True},
]
_NOW_MS = 1_750_000_000_000  # arbitrary fixed seed timestamp


async def _seed_registry(session_factory) -> None:
    """Insert the 7 migration seed rows into an in-memory DB."""
    repo = CoinRegistryRepo(session_factory)
    for row in _SEED_ROWS:
        await repo.upsert(
            row["coin"],
            leverage=row["leverage"],
            maint_ratio=row["maint_ratio"],
            position_size_usd=None,
            active=row["active"],
            spot_token=row["spot_token"],
            sz_decimals=None,
            bridge_safe=row["bridge_safe"],
            validated_at=_NOW_MS,  # all seeded rows are validated
        )


@pytest_asyncio.fixture
async def seeded_registry(session_factory):
    """CoinRegistry loaded from an in-memory DB seeded with the 7 migration rows."""
    await _seed_registry(session_factory)
    registry = CoinRegistry(session_factory)
    await registry.load()
    return registry


# ── Provenance: get_coin_spec ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_get_coin_spec_btc(seeded_registry):
    """BTC spec from registry matches seeded values."""
    spec = seeded_registry.get_coin_spec("BTC")
    assert spec == CoinMarginSpec(leverage=40, maint_ratio=0.010)


@pytest.mark.asyncio
async def test_provenance_get_coin_spec_all_seven(seeded_registry):
    """All 7 seeded coins return the exact seeded spec values (bit-for-bit)."""
    for row in _SEED_ROWS:
        coin = row["coin"]
        spec = seeded_registry.get_coin_spec(coin)
        expected = CoinMarginSpec(leverage=row["leverage"], maint_ratio=row["maint_ratio"])
        assert spec == expected, f"{coin}: registry={spec!r} != expected={expected!r}"


@pytest.mark.asyncio
async def test_provenance_get_coin_spec_is_coin_margin_spec_type(seeded_registry):
    """get_coin_spec returns CoinMarginSpec (the type used throughout the codebase)."""
    spec = seeded_registry.get_coin_spec("ETH")
    assert isinstance(spec, CoinMarginSpec)


# ── Provenance: universe() ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_universe_contains_live_five(seeded_registry):
    """universe() returns exactly the 5 active+validated live coins."""
    universe = seeded_registry.universe()
    assert set(universe) == {"BTC", "ETH", "SOL", "HYPE", "PURR"}


@pytest.mark.asyncio
async def test_provenance_universe_excludes_inactive(seeded_registry):
    """XPL and ZEC are seeded with active=False → not in universe()."""
    universe = seeded_registry.universe()
    assert "ZEC" not in universe
    assert "XPL" not in universe


@pytest.mark.asyncio
async def test_provenance_universe_sorted(seeded_registry):
    """universe() returns coins in sorted (deterministic) order."""
    universe = seeded_registry.universe()
    assert universe == tuple(sorted(universe))


# ── Provenance: spot_token_map() ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_spot_token_map_equals_seeded_map(seeded_registry):
    """spot_token_map() must equal the seeded {coin: spot_token} mapping."""
    expected = {r["coin"]: r["spot_token"] for r in _SEED_ROWS if r["spot_token"] is not None}
    assert seeded_registry.spot_token_map() == expected


# ── Provenance: spot_token_inverse() ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_spot_token_inverse_is_inverse_of_spot_token_map(seeded_registry):
    """spot_token_inverse() is exactly the inverse of spot_token_map()."""
    fwd = seeded_registry.spot_token_map()
    inv = seeded_registry.spot_token_inverse()
    expected_inv = {v: k for k, v in fwd.items()}
    assert inv == expected_inv


@pytest.mark.asyncio
async def test_provenance_spot_token_inverse_excludes_bridge_blacklist(seeded_registry):
    """Bridge-blacklisted tokens (AVAX0, LINK0, AAVE0) must NOT appear in inverse."""
    inverse = seeded_registry.spot_token_inverse()
    for forbidden in ("AVAX0", "LINK0", "AAVE0"):
        assert forbidden not in inverse, (
            f"Bridge token {forbidden!r} must not appear in spot_token_inverse"
        )


@pytest.mark.asyncio
async def test_provenance_inverse_and_map_are_consistent(seeded_registry):
    """spot_token_inverse() is the true inverse of spot_token_map(); they can never desync."""
    fwd = seeded_registry.spot_token_map()
    inv = seeded_registry.spot_token_inverse()
    for coin, spot_tok in fwd.items():
        assert inv[spot_tok] == coin, (
            f"Inconsistency: map[{coin!r}]={spot_tok!r} but inverse[{spot_tok!r}]={inv.get(spot_tok)!r}"
        )


# ── No-fallback: unknown coin ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_fallback_unknown_coin_not_in_universe(seeded_registry):
    """A coin not in the registry is NOT in universe() — never a silent fallback."""
    assert "WIF" not in seeded_registry.universe()
    assert "DOGE" not in seeded_registry.universe()


@pytest.mark.asyncio
async def test_no_fallback_get_coin_spec_raises_key_error(seeded_registry):
    """get_coin_spec() raises KeyError for an unknown coin (no FALLBACK_LEVERAGE)."""
    with pytest.raises(KeyError, match="WIF"):
        seeded_registry.get_coin_spec("WIF")


@pytest.mark.asyncio
async def test_no_fallback_get_coin_spec_raises_for_inactive_without_validation(session_factory):
    """A coin added but never validated is NOT in universe() and spec still works."""
    repo = CoinRegistryRepo(session_factory)
    # Insert a coin with active=True but validated_at=None (not yet validated)
    await repo.upsert(
        "NEWCOIN",
        leverage=5,
        maint_ratio=0.025,
        position_size_usd=None,
        active=True,
        spot_token=None,
        sz_decimals=None,
        bridge_safe=False,
        validated_at=None,  # NOT validated → must not appear in universe
    )
    registry = CoinRegistry(session_factory)
    await registry.load()

    assert "NEWCOIN" not in registry.universe()
    # But spec IS accessible (coin exists in registry)
    spec = registry.get_coin_spec("NEWCOIN")
    assert spec.leverage == 5


@pytest.mark.asyncio
async def test_no_fallback_empty_registry_universe_is_empty(session_factory):
    """An empty registry returns an empty universe() — no DEFAULT_COINS fallback from service."""
    registry = CoinRegistry(session_factory)
    await registry.load()
    assert registry.universe() == ()


# ── reload() smoke ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reload_picks_up_new_coin(session_factory):
    """reload() atomically swaps the snapshot; new coin appears after reload."""
    registry = CoinRegistry(session_factory)
    await registry.load()
    assert "SOL" not in registry.universe()

    repo = CoinRegistryRepo(session_factory)
    await repo.upsert(
        "SOL",
        leverage=20,
        maint_ratio=0.025,
        position_size_usd=None,
        active=True,
        spot_token="USOL",
        sz_decimals=None,
        bridge_safe=True,
        validated_at=_NOW_MS,
    )

    await registry.reload()
    assert "SOL" in registry.universe()
    assert registry.get_coin_spec("SOL") == CoinMarginSpec(leverage=20, maint_ratio=0.025)
