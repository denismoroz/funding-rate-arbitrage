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
    """B1 margin-policy fields should have correct defaults."""
    s = Settings(**_CREDS)
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


# ---------------------------------------------------------------------------
# PR4 forced_close_trigger tests
# ---------------------------------------------------------------------------

def test_forced_close_trigger_default_is_1_5():
    """forced_close_trigger default must be 1.5."""
    s = Settings(**_CREDS)
    assert s.forced_close_trigger == 1.5


def test_forced_close_trigger_at_1_0_or_below_raises():
    """forced_close_trigger <= 1.0 must fail validation."""
    with pytest.raises(ValidationError):
        Settings(forced_close_trigger=1.0, **_CREDS)
    with pytest.raises(ValidationError):
        Settings(forced_close_trigger=0.5, **_CREDS)


def test_forced_close_trigger_above_top_up_raises():
    """forced_close_trigger >= top_up_trigger must fail validation."""
    with pytest.raises(ValidationError):
        # forced_close_trigger=2.5, top_up_trigger=2.0, healthy_ratio=3.0 → violates invariant
        Settings(forced_close_trigger=2.5, top_up_trigger=2.0, healthy_ratio=3.0, **_CREDS)
    with pytest.raises(ValidationError):
        # forced_close_trigger == top_up_trigger
        Settings(forced_close_trigger=2.0, top_up_trigger=2.0, healthy_ratio=3.0, **_CREDS)


# ---------------------------------------------------------------------------
# Phase E: xsmom settings tests
# ---------------------------------------------------------------------------

def test_xsmom_defaults():
    """xsmom fields must have correct defaults.

    universe/budget/leverage are NOT settings — they are strategy params edited via
    the UI and stored in the xsmom Strategy row's params_json. Only creds + local_mode
    are env-level settings here.
    """
    s = Settings(**_CREDS)
    assert s.xsmom_hl_private_key is None
    assert s.xsmom_hl_account_address is None
    assert s.local_mode is False
    assert s.has_xsmom_credentials() is False


def test_has_xsmom_credentials_false_when_missing():
    s = Settings(**_CREDS)
    assert s.has_xsmom_credentials() is False


def test_has_xsmom_credentials_false_when_only_key():
    s = Settings(xsmom_hl_private_key="0x" + "a" * 64, **_CREDS)
    assert s.has_xsmom_credentials() is False


def test_has_xsmom_credentials_true_when_both_set():
    s = Settings(
        xsmom_hl_private_key="0x" + "a" * 64,
        xsmom_hl_account_address="0x" + "c" * 40,
        **_CREDS,
    )
    assert s.has_xsmom_credentials() is True


def test_xsmom_bad_account_address_rejected():
    """xsmom_hl_account_address must be a valid Ethereum address."""
    with pytest.raises(ValidationError):
        Settings(xsmom_hl_account_address="not-an-address", **_CREDS)


def test_xsmom_fields_from_env(monkeypatch):
    monkeypatch.setenv("FRAB_HL_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("FRAB_HL_ACCOUNT_ADDRESS", "0x" + "b" * 40)
    monkeypatch.setenv("FRAB_XSMOM_HL_PRIVATE_KEY", "0x" + "d" * 64)
    monkeypatch.setenv("FRAB_XSMOM_HL_ACCOUNT_ADDRESS", "0x" + "e" * 40)
    monkeypatch.setenv("FRAB_LOCAL_MODE", "true")
    s = Settings(_env_file=None)
    assert s.local_mode is True
    assert s.has_xsmom_credentials() is True


def test_xsmom_creds_not_required_for_frab_only_boot():
    """Settings is valid without xsmom creds — xsmom engine is optional."""
    s = Settings(**_CREDS)
    assert s.has_xsmom_credentials() is False  # should not raise
