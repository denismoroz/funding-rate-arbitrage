import os

import pytest
from pydantic import ValidationError

from frab.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("FRAB_"):
            monkeypatch.delenv(k, raising=False)


def test_defaults():
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert s.hl_network == "mainnet"
    assert s.universe_tuple() == ()


def test_universe_tuple_parses_csv():
    s = Settings(
        hl_universe="purr, hype , btc",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert s.universe_tuple() == ("PURR", "HYPE", "BTC")


def test_universe_tuple_empty():
    creds = dict(hl_private_key="0x" + "a" * 64, hl_account_address="0x" + "b" * 40)
    assert Settings(hl_universe="", _env_file=None, **creds).universe_tuple() == ()
    assert Settings(hl_universe="   ", _env_file=None, **creds).universe_tuple() == ()


def test_testnet_requires_credentials():
    with pytest.raises(ValidationError):
        Settings(hl_network="testnet", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(hl_network="mainnet", _env_file=None)


def test_testnet_with_credentials_ok():
    s = Settings(
        hl_network="testnet",
        hl_private_key="0xabc",
        hl_account_address="0x" + "a" * 40,
        _env_file=None,
    )
    assert s.hl_network == "testnet"
    assert s.hl_account_address == "0x" + "a" * 40


def test_bad_address_rejected():
    with pytest.raises(ValidationError):
        Settings(hl_account_address="not-an-address", _env_file=None)


def test_secret_not_in_repr():
    s = Settings(
        hl_network="testnet",
        hl_private_key="supersecretkey123",
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert "supersecretkey123" not in repr(s)


def test_env_prefix_applied(monkeypatch):
    monkeypatch.setenv("FRAB_HL_NETWORK", "testnet")
    monkeypatch.setenv("FRAB_HL_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("FRAB_HL_ACCOUNT_ADDRESS", "0x" + "c" * 40)
    s = Settings(_env_file=None)
    assert s.hl_network == "testnet"
    assert s.hl_account_address == "0x" + "c" * 40


def test_dry_run_default_false():
    s = Settings(
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert s.dry_run is False


def test_dry_run_from_env(monkeypatch):
    monkeypatch.setenv("FRAB_DRY_RUN", "true")
    monkeypatch.setenv("FRAB_HL_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("FRAB_HL_ACCOUNT_ADDRESS", "0x" + "b" * 40)
    s = Settings(_env_file=None)
    assert s.dry_run is True


# ---------------------------------------------------------------------------
# B1 margin-policy settings tests
# ---------------------------------------------------------------------------

_CREDS = dict(
    hl_private_key="0x" + "a" * 64,
    hl_account_address="0x" + "b" * 40,
    _env_file=None,
)


def test_b1_defaults():
    """New B1 fields should have correct defaults."""
    s = Settings(**_CREDS)
    assert s.per_coin_params_json == ""
    assert s.budget_cap_usd == 1000.0
    assert s.margin_buffer_x == 3.0
    assert s.top_up_trigger == 2.0
    assert s.healthy_ratio == 3.0


def test_b1_budget_cap_override_via_env(monkeypatch):
    """FRAB_BUDGET_CAP_USD env var should be picked up."""
    monkeypatch.setenv("FRAB_BUDGET_CAP_USD", "500")
    monkeypatch.setenv("FRAB_HL_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("FRAB_HL_ACCOUNT_ADDRESS", "0x" + "b" * 40)
    s = Settings(_env_file=None)
    assert s.budget_cap_usd == 500.0


def test_b1_per_coin_params_empty_returns_none():
    """per_coin_params() must return None when per_coin_params_json is empty."""
    s = Settings(**_CREDS)
    assert s.per_coin_params() is None


def test_b1_per_coin_params_valid_json():
    """per_coin_params() returns parsed dict for valid JSON."""
    payload = '{"BTC": {"position_size_usd": 100.0, "leverage": 20, "maint_ratio": 0.01}}'
    s = Settings(per_coin_params_json=payload, **_CREDS)
    result = s.per_coin_params()
    assert result is not None
    assert "BTC" in result
    btc = result["BTC"]
    assert btc["position_size_usd"] == 100.0
    assert btc["leverage"] == 20
    assert btc["maint_ratio"] == 0.01


def test_b1_per_coin_params_invalid_json_raises():
    """per_coin_params() raises ValueError on malformed JSON."""
    s = Settings(per_coin_params_json="{not valid json", **_CREDS)
    with pytest.raises(ValueError, match="not valid JSON"):
        s.per_coin_params()


def test_b1_per_coin_params_missing_key_raises():
    """per_coin_params() raises ValueError when a required inner key is absent."""
    payload = '{"BTC": {"position_size_usd": 100.0, "maint_ratio": 0.01}}'
    s = Settings(per_coin_params_json=payload, **_CREDS)
    with pytest.raises(ValueError, match="leverage"):
        s.per_coin_params()


def test_b1_top_up_trigger_ge_healthy_ratio_raises():
    """top_up_trigger >= healthy_ratio must fail at instantiation."""
    with pytest.raises(ValidationError):
        Settings(top_up_trigger=3.0, healthy_ratio=3.0, **_CREDS)
    with pytest.raises(ValidationError):
        Settings(top_up_trigger=4.0, healthy_ratio=3.0, **_CREDS)


def test_b1_margin_buffer_x_out_of_range_raises():
    """margin_buffer_x > 10.0 should fail validation."""
    with pytest.raises(ValidationError):
        Settings(margin_buffer_x=10.1, **_CREDS)
    with pytest.raises(ValidationError):
        Settings(margin_buffer_x=0.5, **_CREDS)


def test_b1_budget_cap_zero_or_negative_raises():
    """budget_cap_usd <= 0 should fail validation."""
    with pytest.raises(ValidationError):
        Settings(budget_cap_usd=0.0, **_CREDS)
    with pytest.raises(ValidationError):
        Settings(budget_cap_usd=-100.0, **_CREDS)
