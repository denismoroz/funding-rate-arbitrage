"""Phase F1 — XSMOM maint_ratio provenance test.

Verifies that for ALL 34 XSMOM coins, registry.get_coin_spec(coin).maint_ratio
equals the value that old settings.get_coin_spec(coin).maint_ratio returned:
  - BTC=0.01, ETH=0.01, SOL=0.025 (from RESEARCH_MAINT_RATIO)
  - all other 31 coins → 0.05 (FALLBACK_MAINT_RATIO)

This test seeds an in-memory DB with all 38 rows (7 FRAB + 31 new XSMOM)
matching exactly what the F1 migration inserts.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.coin_registry import CoinRegistry
from frab.constants import (
    FALLBACK_MAINT_RATIO,
    RESEARCH_MAINT_RATIO,
)
from frab.db.session import init_db, make_session_factory
from frab.repo.coin_registry_repo import CoinRegistryRepo
from frab.strategy.xsmom.params import DEFAULT_XSMOM_UNIVERSE

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session_factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    sf = make_session_factory(eng)
    yield sf
    await eng.dispose()


# Replicate the 7 FRAB seed rows (from migration 294489218bcb).
_FRAB_SEED = [
    {"coin": "BTC",  "leverage": 40, "maint_ratio": 0.010, "active": True,  "spot_token": "UBTC",  "bridge_safe": True},
    {"coin": "ETH",  "leverage": 25, "maint_ratio": 0.010, "active": True,  "spot_token": "UETH",  "bridge_safe": True},
    {"coin": "HYPE", "leverage": 10, "maint_ratio": 0.025, "active": True,  "spot_token": "HYPE",  "bridge_safe": True},
    {"coin": "PURR", "leverage":  3, "maint_ratio": 0.025, "active": True,  "spot_token": "PURR",  "bridge_safe": True},
    {"coin": "SOL",  "leverage": 20, "maint_ratio": 0.025, "active": True,  "spot_token": "USOL",  "bridge_safe": True},
    {"coin": "XPL",  "leverage": 10, "maint_ratio": 0.025, "active": False, "spot_token": "XPL",   "bridge_safe": True},
    {"coin": "ZEC",  "leverage": 10, "maint_ratio": 0.025, "active": False, "spot_token": "ZEC",   "bridge_safe": True},
]

# Replicate the 31 XSMOM seed rows (from migration f1a2b3c4d5e6).
_XSMOM_NEW_SEED = [
    {"coin": "AAVE",   "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ADA",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "APT",    "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ARB",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "ATOM",   "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "AVAX",   "sz_decimals": 2, "spot_token": "UAVAX", "bridge_safe": True},
    {"coin": "BCH",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "BNB",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "CRV",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "DOGE",   "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "DOT",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "EIGEN",  "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ENA",    "sz_decimals": 0, "spot_token": "UENA",  "bridge_safe": True},
    {"coin": "HMSTR",  "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "INJ",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "JTO",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "JUP",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "LINK",   "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "LTC",    "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "NEAR",   "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "PENDLE", "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "PYTH",   "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "SUI",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "TAO",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "TON",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "TRX",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "UNI",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "WLD",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "XLM",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "XRP",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "ZRO",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
]

_NOW_MS = 1_750_000_000_000  # arbitrary fixed seed timestamp


async def _seed_full_registry(session_factory) -> None:
    """Seed all 38 rows (7 FRAB + 31 XSMOM) into an in-memory DB."""
    repo = CoinRegistryRepo(session_factory)
    # 7 FRAB rows
    for row in _FRAB_SEED:
        await repo.upsert(
            row["coin"],
            leverage=row["leverage"],
            maint_ratio=row["maint_ratio"],
            position_size_usd=None,
            active=row["active"],
            spot_token=row["spot_token"],
            sz_decimals=None,
            bridge_safe=row["bridge_safe"],
            validated_at=_NOW_MS,
        )
    # 31 new XSMOM rows (leverage=3, maint_ratio=0.05, active=False)
    for row in _XSMOM_NEW_SEED:
        await repo.upsert(
            row["coin"],
            leverage=3,
            maint_ratio=0.05,
            position_size_usd=None,
            active=False,
            spot_token=row["spot_token"],
            sz_decimals=row["sz_decimals"],
            bridge_safe=row["bridge_safe"],
            validated_at=_NOW_MS,
        )


@pytest_asyncio.fixture
async def full_registry(session_factory):
    """CoinRegistry loaded with all 38 rows."""
    await _seed_full_registry(session_factory)
    registry = CoinRegistry(session_factory)
    await registry.load()
    return registry


# ── Provenance: all 34 XSMOM coins ───────────────────────────────────────────

def _old_settings_maint_ratio(coin: str) -> float:
    """Reproduce exactly what settings.get_coin_spec(coin).maint_ratio returned.

    Old logic in Settings.get_coin_spec():
      1. If per_coin_params_json override → use that (not applicable here).
      2. If coin in RESEARCH_MAINT_RATIO → use RESEARCH_MAINT_RATIO[coin].
      3. Else → FALLBACK_MAINT_RATIO (0.05).
    """
    if coin in RESEARCH_MAINT_RATIO:
        return RESEARCH_MAINT_RATIO[coin]
    return FALLBACK_MAINT_RATIO


@pytest.mark.asyncio
async def test_xsmom_provenance_maint_ratio_all_34_coins(full_registry):
    """For every coin in DEFAULT_XSMOM_UNIVERSE:
    registry.get_coin_spec(coin).maint_ratio == old settings.get_coin_spec(coin).maint_ratio.

    Critical pairs:
      BTC=0.01, ETH=0.01, SOL=0.025 (in RESEARCH_MAINT_RATIO)
      all other 31 → 0.05 (FALLBACK_MAINT_RATIO)
    """
    mismatches = []
    for coin in DEFAULT_XSMOM_UNIVERSE:
        expected = _old_settings_maint_ratio(coin)
        actual = full_registry.get_coin_spec(coin).maint_ratio
        if actual != expected:
            mismatches.append(
                f"{coin}: registry={actual!r} != old_settings={expected!r}"
            )

    assert not mismatches, (
        f"Provenance FAIL — {len(mismatches)} coin(s) have wrong maint_ratio:\n"
        + "\n".join(mismatches)
    )


@pytest.mark.asyncio
async def test_xsmom_provenance_btc_eth_sol_research_values(full_registry):
    """BTC/ETH/SOL use RESEARCH_MAINT_RATIO values (0.01/0.01/0.025)."""
    assert full_registry.get_coin_spec("BTC").maint_ratio == 0.01
    assert full_registry.get_coin_spec("ETH").maint_ratio == 0.01
    assert full_registry.get_coin_spec("SOL").maint_ratio == 0.025


@pytest.mark.asyncio
async def test_xsmom_provenance_new_31_coins_fallback_maint_ratio(full_registry):
    """All 31 new XSMOM-only coins have maint_ratio=0.05 (FALLBACK_MAINT_RATIO)."""
    new_coins = [r["coin"] for r in _XSMOM_NEW_SEED]
    for coin in new_coins:
        actual = full_registry.get_coin_spec(coin).maint_ratio
        assert actual == FALLBACK_MAINT_RATIO, (
            f"{coin}: expected {FALLBACK_MAINT_RATIO!r} got {actual!r}"
        )


@pytest.mark.asyncio
async def test_xsmom_provenance_registry_has_all_34_xsmom_coins(full_registry):
    """registry.get_coin_spec(coin) does not raise for any DEFAULT_XSMOM_UNIVERSE coin."""
    missing = []
    for coin in DEFAULT_XSMOM_UNIVERSE:
        try:
            full_registry.get_coin_spec(coin)
        except KeyError:
            missing.append(coin)
    assert not missing, f"Coins missing from registry: {missing}"


@pytest.mark.asyncio
async def test_xsmom_new_coins_not_in_frab_universe(full_registry):
    """The 31 new XSMOM coins are active=False → never in registry.universe() (FRAB stays unchanged)."""
    universe = set(full_registry.universe())
    for row in _XSMOM_NEW_SEED:
        assert row["coin"] not in universe, (
            f"{row['coin']} should NOT be in FRAB universe (active=False)"
        )


@pytest.mark.asyncio
async def test_frab_universe_unchanged_after_xsmom_seed(full_registry):
    """The 5 live FRAB coins are still in universe() after adding 31 XSMOM rows."""
    universe = set(full_registry.universe())
    for coin in ("BTC", "ETH", "SOL", "HYPE", "PURR"):
        assert coin in universe, f"{coin} should still be in FRAB universe"
