"""Phase F2: CoinRegistry is the single source of coin specs.

Tests that were previously exercising Settings.get_coin_spec() / per_coin_params_json /
hl_universe are now replaced by registry-sourced tests.

Removed:
- test_returns_research_default_when_no_override — tested Settings.get_coin_spec() which is deleted
- test_returns_override_when_env_set — tested per_coin_params_json env layer which is deleted
- test_falls_back_for_unknown_coin — tested FALLBACK_LEVERAGE which is deleted (no-fallback policy)
- test_research_table_has_seven_coins — tested RESEARCH_LEVERAGE constant which is deleted
- test_research_and_maint_keys_match — tested RESEARCH_* constants which are deleted

Kept intent: coin spec resolution must be correct and sourced from the registry.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.coin_registry import CoinRegistry, RegistryAwareSettings
from frab.constants import CoinMarginSpec
from frab.db.session import init_db, make_session_factory
from frab.repo.coin_registry_repo import CoinRegistryRepo
from frab.settings import Settings


_CREDS = dict(
    hl_private_key="0x" + "a" * 64,
    hl_account_address="0x" + "b" * 40,
    _env_file=None,
)

_SEED_ROWS = [
    {"coin": "BTC",  "leverage": 40, "maint_ratio": 0.010, "active": True,  "spot_token": "UBTC"},
    {"coin": "ETH",  "leverage": 25, "maint_ratio": 0.010, "active": True,  "spot_token": "UETH"},
    {"coin": "SOL",  "leverage": 20, "maint_ratio": 0.025, "active": True,  "spot_token": "USOL"},
]

_NOW_MS = 1_750_000_000_000


@pytest_asyncio.fixture
async def session_factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    sf = make_session_factory(eng)
    yield sf
    await eng.dispose()


@pytest_asyncio.fixture
async def registry(session_factory):
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
            bridge_safe=True,
            validated_at=_NOW_MS,
        )
    reg = CoinRegistry(session_factory)
    await reg.load()
    return reg


@pytest.mark.asyncio
async def test_registry_returns_correct_spec_btc(registry):
    """BTC spec from registry is the seeded value."""
    spec = registry.get_coin_spec("BTC")
    assert spec == CoinMarginSpec(leverage=40, maint_ratio=0.010)


@pytest.mark.asyncio
async def test_registry_returns_correct_type(registry):
    """get_coin_spec returns a CoinMarginSpec instance."""
    spec = registry.get_coin_spec("ETH")
    assert isinstance(spec, CoinMarginSpec)


@pytest.mark.asyncio
async def test_registry_raises_for_unknown_coin(registry):
    """Unknown coin raises KeyError — no silent fallback (design decision 3)."""
    with pytest.raises(KeyError, match="WIF"):
        registry.get_coin_spec("WIF")


@pytest.mark.asyncio
async def test_registry_aware_settings_delegates_get_coin_spec(registry):
    """RegistryAwareSettings.get_coin_spec() delegates to the registry."""
    settings = Settings(**_CREDS)
    ras = RegistryAwareSettings(settings, registry)
    spec = ras.get_coin_spec("SOL")
    assert spec == CoinMarginSpec(leverage=20, maint_ratio=0.025)


@pytest.mark.asyncio
async def test_registry_aware_settings_raises_for_unknown_coin(registry):
    """RegistryAwareSettings.get_coin_spec() raises KeyError for unknown coins."""
    settings = Settings(**_CREDS)
    ras = RegistryAwareSettings(settings, registry)
    with pytest.raises(KeyError):
        ras.get_coin_spec("UNKNOWN")
