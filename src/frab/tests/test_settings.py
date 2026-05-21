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
