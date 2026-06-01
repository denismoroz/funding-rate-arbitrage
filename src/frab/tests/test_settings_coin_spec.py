import pytest

from frab.constants import (
    CoinMarginSpec,
    FALLBACK_LEVERAGE,
    FALLBACK_MAINT_RATIO,
    RESEARCH_LEVERAGE,
    RESEARCH_MAINT_RATIO,
)
from frab.settings import Settings


_CREDS = dict(
    hl_private_key="0x" + "a" * 64,
    hl_account_address="0x" + "b" * 40,
    _env_file=None,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith("FRAB_"):
            monkeypatch.delenv(k, raising=False)


def test_returns_research_default_when_no_override():
    s = Settings(**_CREDS)
    spec = s.get_coin_spec("BTC")
    assert spec.leverage == 40
    assert spec.maint_ratio == 0.01


def test_returns_override_when_env_set():
    s = Settings(
        per_coin_params_json='{"BTC": {"leverage": 10, "maint_ratio": 0.02}}',
        **_CREDS,
    )
    spec = s.get_coin_spec("BTC")
    assert spec.leverage == 10
    assert spec.maint_ratio == 0.02


def test_falls_back_for_unknown_coin():
    s = Settings(**_CREDS)
    spec = s.get_coin_spec("WIF")
    assert spec.leverage == FALLBACK_LEVERAGE
    assert spec.maint_ratio == FALLBACK_MAINT_RATIO


def test_research_table_has_seven_coins():
    assert set(RESEARCH_LEVERAGE.keys()) == {"BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "HYPE", "PURR", "ZEC", "XPL"}


def test_research_and_maint_keys_match():
    assert set(RESEARCH_LEVERAGE.keys()) == set(RESEARCH_MAINT_RATIO.keys())


def test_returns_correct_type():
    s = Settings(**_CREDS)
    spec = s.get_coin_spec("ETH")
    assert isinstance(spec, CoinMarginSpec)
