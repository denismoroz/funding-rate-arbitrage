"""Unit tests for server.py module-level helpers (no full app spin-up)."""
from __future__ import annotations

import os

import pytest

from frab.db.models import PositionMode
from frab.engine.fee_reconciler import FeeReconciler
from frab.exchanges.atomic import AtomicExecutor
from frab.server import (
    MAINNET_SPOT_TOKEN_MAP,
    _build_executor,
    _build_fee_reconciler,
    _build_margin_manager,
    _build_params_override,
    _hl_info_url,
    _position_mode,
    _select_coins,
    _select_spot_token_map,
    _validate_spot_pairs,
)
from frab.settings import Settings
from frab.strategies.registry import _StrategyASpec, _TwoPhaseDynamicSpec


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("FRAB_"):
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# _select_coins
# ---------------------------------------------------------------------------

_CREDS = dict(hl_private_key="0x" + "a" * 64, hl_account_address="0x" + "b" * 40)


def test_select_coins_returns_default_when_settings_empty():
    s = Settings(_env_file=None, **_CREDS)
    result = _select_coins(s, ("BTC", "ETH"))
    assert result == ("BTC", "ETH")


def test_select_coins_returns_settings_universe_when_set():
    s = Settings(hl_universe="PURR,HYPE", _env_file=None, **_CREDS)
    result = _select_coins(s, ("BTC", "ETH"))
    assert result == ("PURR", "HYPE")


# ---------------------------------------------------------------------------
# _select_spot_token_map
# ---------------------------------------------------------------------------

def test_select_spot_token_map_mainnet_has_wrappeds():
    """Only Unit Network bridges with proven 1:1 + liquidity (BTC/ETH/SOL)."""
    m = _select_spot_token_map("mainnet")
    assert m["BTC"] == "UBTC"
    assert m["ETH"] == "UETH"
    assert m["SOL"] == "USOL"
    # AVAX, LINK, AAVE, DOGE excluded: either no 1:1 bridge or no liquidity.
    for coin in ("AVAX", "LINK", "AAVE", "DOGE"):
        assert coin not in m


def test_select_spot_token_map_testnet_empty():
    assert _select_spot_token_map("testnet") == {}


# ---------------------------------------------------------------------------
# _position_mode
# ---------------------------------------------------------------------------

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

def test_build_executor_testnet_returns_live_executor(mocker):
    mock_live = mocker.patch("frab.server.LiveHLExecutor")
    s = Settings(
        hl_network="testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="0x" + "b" * 40,
        _env_file=None,
    )
    _build_executor(s, market_data=mocker.MagicMock())
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
    _build_executor(s, market_data=mocker.MagicMock())
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
    _build_executor(s, market_data=mocker.MagicMock())
    _, kwargs = mock_live.call_args
    assert kwargs["slippage"] == 0.025


# ---------------------------------------------------------------------------
# _build_params_override — env-driven risk caps for live mode
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _build_margin_manager — legacy returns None; configured returns MarginManager
# ---------------------------------------------------------------------------

def test_build_margin_manager_returns_none_when_per_coin_params_empty():
    s = Settings(_env_file=None, **_CREDS)
    assert _build_margin_manager(s) is None


def test_build_margin_manager_constructs_from_per_coin_params_json():
    per_coin = (
        '{"BTC": {"position_size_usd": 100.0, "leverage": 20, "maint_ratio": 0.01},'
        ' "DOGE": {"position_size_usd": 50.0, "leverage": 5, "maint_ratio": 0.05}}'
    )
    s = Settings(
        per_coin_params_json=per_coin,
        budget_cap_usd=500.0,
        margin_buffer_x=3.0,
        top_up_trigger=2.0,
        healthy_ratio=3.0,
        _env_file=None,
        **_CREDS,
    )
    mgr = _build_margin_manager(s)
    assert mgr is not None
    assert mgr.margin_buffer_x == 3.0
    assert mgr.top_up_trigger == 2.0
    assert mgr.healthy_ratio == 3.0
    assert mgr.budget_cap_usd == 500.0
    # Footprint sanity for BTC: spot=100, perp = 100/20*3 = 15
    spot, perp = mgr.compute_pair_footprint("BTC")
    assert spot == 100.0
    assert perp == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# _build_fee_reconciler — always returns FeeReconciler
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _validate_spot_pairs
# ---------------------------------------------------------------------------

_SPOTMETA_HAPPY = {
    "tokens": [
        {"index": 0, "name": "USDC"},
        {"index": 197, "name": "UBTC"},
        {"index": 200, "name": "UETH"},
        {"index": 210, "name": "USOL"},
        {"index": 220, "name": "UAVAX"},
        {"index": 99, "name": "LINK0"},
    ],
    "universe": [
        {"index": 142, "name": "@142", "tokens": [197, 0], "isCanonical": False},
        {"index": 151, "name": "@151", "tokens": [200, 0], "isCanonical": False},
        {"index": 156, "name": "@156", "tokens": [210, 0], "isCanonical": False},
        {"index": 306, "name": "@306", "tokens": [220, 0], "isCanonical": False},
        {"index": 213, "name": "@213", "tokens": [99, 0], "isCanonical": False},
    ],
}


async def test_validate_spot_pairs_happy_path(mocker):
    md = mocker.MagicMock()
    md._post = mocker.AsyncMock(return_value=_SPOTMETA_HAPPY)
    await _validate_spot_pairs(md, ("BTC", "ETH", "SOL"))  # no raise


async def test_validate_spot_pairs_missing_map_entry_raises(mocker):
    md = mocker.MagicMock()
    md._post = mocker.AsyncMock(return_value=_SPOTMETA_HAPPY)
    with pytest.raises(RuntimeError, match="no map entry"):
        await _validate_spot_pairs(md, ("BTC", "DOGE"))


async def test_validate_spot_pairs_base_token_not_on_hl_raises(mocker):
    """A coin mapped to a wrapped token that HL doesn't list as USDC pair.

    Constructs a malformed map (BTC→UBOGUS) to simulate the case where the
    server-side MAINNET_SPOT_TOKEN_MAP gets out of sync with HL spotMeta.
    """
    md = mocker.MagicMock()
    spotmeta_no_ubtc = {
        "tokens": [
            {"index": 0, "name": "USDC"},
            {"index": 200, "name": "UETH"},
        ],
        "universe": [
            {"index": 151, "name": "@151", "tokens": [200, 0], "isCanonical": False},
        ],
    }
    md._post = mocker.AsyncMock(return_value=spotmeta_no_ubtc)
    # BTC is in the map (BTC→UBTC) but the spotMeta we mock doesn't include UBTC.
    with pytest.raises(RuntimeError, match="not on HL"):
        await _validate_spot_pairs(md, ("BTC",))


async def test_validate_spot_pairs_missing_usdc_raises(mocker):
    md = mocker.MagicMock()
    md._post = mocker.AsyncMock(return_value={"tokens": [], "universe": []})
    with pytest.raises(RuntimeError, match="USDC token not found"):
        await _validate_spot_pairs(md, ("BTC",))


# ---------------------------------------------------------------------------
# dry_run wiring through spec.build
# ---------------------------------------------------------------------------

def test_strategy_a_spec_build_dry_run_true(mocker):
    """_StrategyASpec.build with dry_run=True wires _dry_run=True on the strategy."""
    executor = mocker.MagicMock(spec=AtomicExecutor)
    spec = _StrategyASpec()
    strategy, _ = spec.build(coins=("BTC",), params_override=None, executor=executor, dry_run=True)
    assert strategy._dry_run is True


def test_strategy_a_spec_build_dry_run_default_false(mocker):
    """_StrategyASpec.build with no dry_run arg leaves _dry_run=False."""
    executor = mocker.MagicMock(spec=AtomicExecutor)
    spec = _StrategyASpec()
    strategy, _ = spec.build(coins=("BTC",), params_override=None, executor=executor)
    assert strategy._dry_run is False


def test_two_phase_dynamic_spec_build_dry_run_true(mocker):
    """_TwoPhaseDynamicSpec.build with dry_run=True wires _dry_run=True on the strategy."""
    executor = mocker.MagicMock(spec=AtomicExecutor)
    spec = _TwoPhaseDynamicSpec()
    strategy, _ = spec.build(coins=("BTC",), params_override=None, executor=executor, dry_run=True)
    assert strategy._dry_run is True


def test_two_phase_dynamic_spec_build_dry_run_default_false(mocker):
    """_TwoPhaseDynamicSpec.build with no dry_run arg leaves _dry_run=False."""
    executor = mocker.MagicMock(spec=AtomicExecutor)
    spec = _TwoPhaseDynamicSpec()
    strategy, _ = spec.build(coins=("BTC",), params_override=None, executor=executor)
    assert strategy._dry_run is False
