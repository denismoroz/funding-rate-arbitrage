"""Unit tests for server.py module-level helpers (no full app spin-up)."""
from __future__ import annotations

import os

import pytest

from frab.db.models import PositionMode
from frab.engine.fee_reconciler import FeeReconciler
from frab.exchanges.paper import PaperExecutor
from frab.server import (
    MAINNET_SPOT_TOKEN_MAP,
    _build_executor,
    _build_fee_reconciler,
    _build_params_override,
    _hl_info_url,
    _position_mode,
    _select_coins,
    _select_spot_token_map,
)
from frab.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("FRAB_"):
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# _select_coins
# ---------------------------------------------------------------------------

def test_select_coins_returns_default_when_settings_empty():
    s = Settings(_env_file=None)
    result = _select_coins(s, ("BTC", "ETH"))
    assert result == ("BTC", "ETH")


def test_select_coins_returns_settings_universe_when_set():
    s = Settings(hl_universe="PURR,HYPE", _env_file=None)
    result = _select_coins(s, ("BTC", "ETH"))
    assert result == ("PURR", "HYPE")


# ---------------------------------------------------------------------------
# _select_spot_token_map
# ---------------------------------------------------------------------------

def test_select_spot_token_map_mainnet_has_wrappeds():
    m = _select_spot_token_map("mainnet")
    assert m["BTC"] == "UBTC"
    assert m["ETH"] == "UETH"
    assert m["SOL"] == "USOL"
    assert m["AVAX"] == "AVAX0"
    assert m["LINK"] == "LINK0"
    assert m["AAVE"] == "AAVE0"
    assert "DOGE" not in m


def test_select_spot_token_map_testnet_empty():
    assert _select_spot_token_map("testnet") == {}


def test_select_spot_token_map_paper_empty():
    assert _select_spot_token_map("paper") == {}


# ---------------------------------------------------------------------------
# _position_mode
# ---------------------------------------------------------------------------

def test_position_mode_paper():
    s = Settings(_env_file=None)
    assert _position_mode(s) == PositionMode.PAPER


def test_position_mode_testnet_returns_live():
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert _position_mode(s) == PositionMode.LIVE


def test_position_mode_mainnet_returns_live():
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert _position_mode(s) == PositionMode.LIVE


# ---------------------------------------------------------------------------
# _hl_info_url
# ---------------------------------------------------------------------------

def test_hl_info_url_paper_uses_configured():
    s = Settings(_env_file=None)
    url = _hl_info_url(s)
    assert url == s.hl_api_url
    assert url.endswith("/info")


def test_hl_info_url_testnet():
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert _hl_info_url(s) == "https://api.hyperliquid-testnet.xyz/info"


def test_hl_info_url_mainnet():
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    assert _hl_info_url(s) == "https://api.hyperliquid.xyz/info"


# ---------------------------------------------------------------------------
# _build_executor
# ---------------------------------------------------------------------------

def test_build_executor_paper_returns_paper_executor(mocker):
    s = Settings(_env_file=None)
    md = mocker.MagicMock()
    result = _build_executor(s, market_data=md, spot_taker_bps=7.0, perp_taker_bps=2.5)
    assert isinstance(result, PaperExecutor)
    assert result._spot_taker_bps == 7.0
    assert result._perp_taker_bps == 2.5


def test_build_executor_testnet_returns_live_executor(mocker):
    mock_live = mocker.patch("frab.server.LiveHLExecutor")
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    _build_executor(s, market_data=mocker.MagicMock(), spot_taker_bps=7.0, perp_taker_bps=2.5)
    mock_live.assert_called_once_with(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="testnet",
        spot_token_map={},
        slippage=0.01,
    )


def test_build_executor_mainnet_uses_spot_token_map(mocker):
    mock_live = mocker.patch("frab.server.LiveHLExecutor")
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    _build_executor(s, market_data=mocker.MagicMock(), spot_taker_bps=7.0, perp_taker_bps=2.5)
    mock_live.assert_called_once_with(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="mainnet",
        spot_token_map=MAINNET_SPOT_TOKEN_MAP,
        slippage=0.01,
    )


def test_build_executor_live_uses_hl_live_slippage_setting(mocker):
    mock_live = mocker.patch("frab.server.LiveHLExecutor")
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        hl_live_slippage=0.025,
        _env_file=None,
    )
    _build_executor(s, market_data=mocker.MagicMock(), spot_taker_bps=7.0, perp_taker_bps=2.5)
    _, kwargs = mock_live.call_args
    assert kwargs["slippage"] == 0.025


# ---------------------------------------------------------------------------
# _build_params_override — env-driven risk caps for live mode
# ---------------------------------------------------------------------------

def test_build_params_override_paper_returns_empty_dict():
    s = Settings(hl_network="paper", _env_file=None)
    assert _build_params_override(s) == {}


def test_build_params_override_live_sets_position_size_and_cap():
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        hl_position_size_usd=12.0,
        hl_max_open_positions=1,
        _env_file=None,
    )
    override = _build_params_override(s)
    assert override["position_size_usdc"] == 12.0
    assert override["concurrency_cap"] == 1


def test_build_params_override_live_preserves_strategy_params_json():
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        hl_position_size_usd=15.0,
        hl_max_open_positions=2,
        strategy_params_json='{"entry_threshold": 0.5}',
        _env_file=None,
    )
    override = _build_params_override(s)
    # User-supplied strategy_params_json values survive…
    assert override["entry_threshold"] == 0.5
    # …alongside HL-driven caps.
    assert override["position_size_usdc"] == 15.0
    assert override["concurrency_cap"] == 2


def test_build_params_override_paper_ignores_hl_caps():
    """Paper mode should not stamp position_size_usdc / concurrency_cap from HL settings."""
    s = Settings(
        hl_network="paper",
        hl_position_size_usd=99.0,
        hl_max_open_positions=99,
        _env_file=None,
    )
    override = _build_params_override(s)
    assert "position_size_usdc" not in override
    assert "concurrency_cap" not in override


# ---------------------------------------------------------------------------
# _build_fee_reconciler — live wires FeeReconciler, paper wires None
# ---------------------------------------------------------------------------


def test_build_fee_reconciler_paper_returns_none(mocker):
    """Paper mode: _build_fee_reconciler returns None."""
    s = Settings(hl_network="paper", _env_file=None)
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
    )
    assert result is None


def test_build_fee_reconciler_testnet_returns_reconciler(mocker):
    """Testnet (live) mode: _build_fee_reconciler returns a FeeReconciler."""
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
    )
    assert isinstance(result, FeeReconciler)


def test_build_fee_reconciler_mainnet_returns_reconciler(mocker):
    """Mainnet (live) mode: _build_fee_reconciler returns a FeeReconciler."""
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
    )
    assert isinstance(result, FeeReconciler)


def test_build_fee_reconciler_uses_account_address(mocker):
    """FeeReconciler is constructed with the configured hl_account_address."""
    account = "0x" + "c" * 40
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address=account,
        _env_file=None,
    )
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
    )
    assert isinstance(result, FeeReconciler)
    assert result._user_address == account


def test_build_fee_reconciler_forwards_strategy_and_id(mocker):
    """strategy and strategy_id are stored on the returned FeeReconciler."""
    s = Settings(
        hl_network="mainnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    mock_strategy = mocker.MagicMock()
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
        strategy=mock_strategy,
        strategy_id=42,
    )
    assert isinstance(result, FeeReconciler)
    assert result._strategy is mock_strategy
    assert result._strategy_id == 42


def test_build_fee_reconciler_strategy_defaults_to_none(mocker):
    """Omitting strategy/strategy_id leaves them as None (backwards-compat)."""
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    result = _build_fee_reconciler(
        s,
        session_factory=mocker.MagicMock(),
        market_data=mocker.MagicMock(),
        bus=mocker.MagicMock(),
    )
    assert isinstance(result, FeeReconciler)
    assert result._strategy is None
    assert result._strategy_id is None
