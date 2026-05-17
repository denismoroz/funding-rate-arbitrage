"""Tests for frab.strategies.registry."""
from __future__ import annotations

import pytest

from frab.strategies.registry import (
    _StrategyASpec,
    _StrategyCSpec,
    get_strategy_spec,
    parse_params_override,
)
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.strategy_c import StrategyC, StrategyCParams


# ---------------------------------------------------------------------------
# get_strategy_spec
# ---------------------------------------------------------------------------

def test_get_strategy_spec_strategy_a():
    spec = get_strategy_spec("strategy_a")
    assert isinstance(spec, _StrategyASpec)
    assert spec.name == "strategy_a"
    assert spec.version == "v1"


def test_get_strategy_spec_strategy_c():
    spec = get_strategy_spec("strategy_c")
    assert isinstance(spec, _StrategyCSpec)
    assert spec.name == "strategy_c"
    assert spec.version == "v1"


def test_get_strategy_spec_unknown_raises_key_error():
    with pytest.raises(KeyError) as exc_info:
        get_strategy_spec("unknown_strategy")
    msg = str(exc_info.value)
    assert "unknown_strategy" in msg
    assert "strategy_a" in msg
    assert "strategy_c" in msg


# ---------------------------------------------------------------------------
# parse_params_override
# ---------------------------------------------------------------------------

def test_parse_params_override_empty_string():
    assert parse_params_override("") is None


def test_parse_params_override_whitespace():
    assert parse_params_override("   ") is None


def test_parse_params_override_invalid_json():
    # Must not raise; returns None and logs warning
    result = parse_params_override("not json")
    assert result is None


def test_parse_params_override_valid_json_but_not_dict():
    # A JSON string is valid JSON but not a dict
    result = parse_params_override('"string"')
    assert result is None


def test_parse_params_override_valid_json_array():
    # A JSON array is valid JSON but not a dict
    result = parse_params_override('[1, 2, 3]')
    assert result is None


def test_parse_params_override_valid_dict():
    result = parse_params_override('{"entry_threshold": 0.15}')
    assert result == {"entry_threshold": 0.15}


def test_parse_params_override_complex_dict():
    result = parse_params_override('{"entry_threshold": 0.20, "concurrency_cap": 5}')
    assert result == {"entry_threshold": 0.20, "concurrency_cap": 5}


# ---------------------------------------------------------------------------
# _StrategyASpec.build
# ---------------------------------------------------------------------------

def test_strategy_a_spec_build_defaults(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyASpec()
    strategy, params_json = spec.build(coins=("BTC", "ETH"), params_override=None, executor=executor)

    assert isinstance(strategy, StrategyA)
    defaults = StrategyAParams(coins=("BTC", "ETH"))
    assert params_json["entry_threshold"] == defaults.entry_threshold
    assert params_json["exit_threshold"] == defaults.exit_threshold
    assert params_json["min_hold_hours"] == defaults.min_hold_hours
    assert params_json["signal_window_hours"] == defaults.signal_window_hours
    assert params_json["concurrency_cap"] == defaults.concurrency_cap
    assert params_json["position_size_usdc"] == defaults.position_size_usdc
    assert params_json["coins"] == ["BTC", "ETH"]


def test_strategy_a_spec_build_with_override(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyASpec()
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"entry_threshold": 0.25},
        executor=executor,
    )

    assert isinstance(strategy, StrategyA)
    assert strategy._params.entry_threshold == 0.25
    assert params_json["entry_threshold"] == 0.25


def test_strategy_a_spec_build_unknown_key_ignored(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyASpec()
    # Should not raise; unknown key is silently ignored with a warning
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"unknown_key": 999, "entry_threshold": 0.20},
        executor=executor,
    )

    assert isinstance(strategy, StrategyA)
    assert strategy._params.entry_threshold == 0.20
    assert "unknown_key" not in params_json


def test_strategy_a_spec_build_returns_correct_params_json_keys(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyASpec()
    _, params_json = spec.build(coins=("BTC", "ETH", "SOL"), params_override=None, executor=executor)

    expected_keys = {
        "coins", "entry_threshold", "exit_threshold", "min_hold_hours",
        "signal_window_hours", "concurrency_cap", "position_size_usdc",
    }
    assert set(params_json.keys()) == expected_keys


# ---------------------------------------------------------------------------
# _StrategyCSpec.build
# ---------------------------------------------------------------------------

def test_strategy_c_spec_build_defaults(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyCSpec()
    strategy, params_json = spec.build(coins=("BTC", "ETH"), params_override=None, executor=executor)

    assert isinstance(strategy, StrategyC)
    defaults = StrategyCParams(coins=("BTC", "ETH"))
    assert params_json["entry_threshold"] == defaults.entry_threshold
    assert params_json["signal_window_hours"] == defaults.signal_window_hours
    assert params_json["base_min_hold_hours"] == defaults.base_min_hold_hours
    assert params_json["safety_mult"] == defaults.safety_mult
    assert params_json["cap_min_hold_hours"] == defaults.cap_min_hold_hours
    assert params_json["phase1_negative_patience"] == defaults.phase1_negative_patience
    assert params_json["phase1_breakeven_cap_hours"] == defaults.phase1_breakeven_cap_hours
    assert params_json["phase2_exit_threshold"] == defaults.phase2_exit_threshold
    assert params_json["concurrency_cap"] == defaults.concurrency_cap
    assert params_json["position_size_usdc"] == defaults.position_size_usdc
    assert params_json["fee_round_trip_annual"] == defaults.fee_round_trip_annual
    assert params_json["coins"] == ["BTC", "ETH"]


def test_strategy_c_spec_build_with_override(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyCSpec()
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"entry_threshold": 0.15, "safety_mult": 3.0},
        executor=executor,
    )

    assert isinstance(strategy, StrategyC)
    assert strategy._params.entry_threshold == 0.15
    assert strategy._params.safety_mult == 3.0
    assert params_json["entry_threshold"] == 0.15
    assert params_json["safety_mult"] == 3.0


def test_strategy_c_spec_build_unknown_key_ignored(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyCSpec()
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"unknown_key": 999, "entry_threshold": 0.12},
        executor=executor,
    )

    assert isinstance(strategy, StrategyC)
    assert strategy._params.entry_threshold == 0.12
    assert "unknown_key" not in params_json


def test_strategy_c_spec_build_returns_correct_params_json_keys(mocker):
    executor = mocker.MagicMock()
    spec = _StrategyCSpec()
    _, params_json = spec.build(coins=("BTC",), params_override=None, executor=executor)

    expected_keys = {
        "coins", "entry_threshold", "signal_window_hours",
        "base_min_hold_hours", "safety_mult", "cap_min_hold_hours",
        "phase1_negative_patience", "phase1_breakeven_cap_hours",
        "phase2_exit_threshold", "concurrency_cap", "position_size_usdc",
        "fee_round_trip_annual",
    }
    assert set(params_json.keys()) == expected_keys
