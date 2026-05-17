"""Tests for frab.strategies.registry."""
from __future__ import annotations

import pytest

from frab.strategies.registry import (
    _StrategyASpec,
    _TwoPhaseDynamicSpec,
    get_strategy_spec,
    parse_params_override,
)
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.two_phase_dynamic import TwoPhaseDynamic, TwoPhaseDynamicParams


# ---------------------------------------------------------------------------
# get_strategy_spec
# ---------------------------------------------------------------------------

def test_get_strategy_spec_strategy_a():
    spec = get_strategy_spec("strategy_a")
    assert isinstance(spec, _StrategyASpec)
    assert spec.name == "strategy_a"
    assert spec.version == "v1"


def test_get_strategy_spec_two_phase_dynamic():
    spec = get_strategy_spec("two_phase_dynamic")
    assert isinstance(spec, _TwoPhaseDynamicSpec)
    assert spec.name == "two_phase_dynamic"
    assert spec.version == "v1"


def test_get_strategy_spec_unknown_raises_key_error():
    with pytest.raises(KeyError) as exc_info:
        get_strategy_spec("unknown_strategy")
    msg = str(exc_info.value)
    assert "unknown_strategy" in msg
    assert "strategy_a" in msg
    assert "two_phase_dynamic" in msg


# ---------------------------------------------------------------------------
# parse_params_override
# ---------------------------------------------------------------------------

def test_parse_params_override_empty_string():
    assert parse_params_override("") is None


def test_parse_params_override_whitespace():
    assert parse_params_override("   ") is None


def test_parse_params_override_invalid_json():
    result = parse_params_override("not json")
    assert result is None


def test_parse_params_override_valid_json_but_not_dict():
    result = parse_params_override('"string"')
    assert result is None


def test_parse_params_override_valid_json_array():
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
# _TwoPhaseDynamicSpec.build
# ---------------------------------------------------------------------------

def test_two_phase_dynamic_spec_build_defaults(mocker):
    executor = mocker.MagicMock()
    spec = _TwoPhaseDynamicSpec()
    strategy, params_json = spec.build(coins=("BTC", "ETH"), params_override=None, executor=executor)

    assert isinstance(strategy, TwoPhaseDynamic)
    defaults = TwoPhaseDynamicParams(coins=("BTC", "ETH"))
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


def test_two_phase_dynamic_spec_build_with_override(mocker):
    executor = mocker.MagicMock()
    spec = _TwoPhaseDynamicSpec()
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"entry_threshold": 0.15, "safety_mult": 3.0},
        executor=executor,
    )

    assert isinstance(strategy, TwoPhaseDynamic)
    assert strategy._params.entry_threshold == 0.15
    assert strategy._params.safety_mult == 3.0
    assert params_json["entry_threshold"] == 0.15
    assert params_json["safety_mult"] == 3.0


def test_two_phase_dynamic_spec_build_unknown_key_ignored(mocker):
    executor = mocker.MagicMock()
    spec = _TwoPhaseDynamicSpec()
    strategy, params_json = spec.build(
        coins=("BTC",),
        params_override={"unknown_key": 999, "entry_threshold": 0.12},
        executor=executor,
    )

    assert isinstance(strategy, TwoPhaseDynamic)
    assert strategy._params.entry_threshold == 0.12
    assert "unknown_key" not in params_json


def test_two_phase_dynamic_spec_build_returns_correct_params_json_keys(mocker):
    executor = mocker.MagicMock()
    spec = _TwoPhaseDynamicSpec()
    _, params_json = spec.build(coins=("BTC",), params_override=None, executor=executor)

    expected_keys = {
        "coins", "entry_threshold", "signal_window_hours",
        "base_min_hold_hours", "safety_mult", "cap_min_hold_hours",
        "phase1_negative_patience", "phase1_breakeven_cap_hours",
        "phase2_exit_threshold", "concurrency_cap", "position_size_usdc",
        "fee_round_trip_annual",
    }
    assert set(params_json.keys()) == expected_keys


# ---------------------------------------------------------------------------
# _StrategyASpec.validate_hot_params
# ---------------------------------------------------------------------------

_VALID_A_BODY = {
    "entry_threshold": 0.50,
    "exit_threshold": -0.10,
    "min_hold_hours": 24,
    "concurrency_cap": 5,
    "position_size_usdc": 500.0,
}


def test_strategy_a_validate_hot_params_valid():
    spec = _StrategyASpec()
    result = spec.validate_hot_params(_VALID_A_BODY)
    assert result["entry_threshold"] == pytest.approx(0.50)
    assert result["exit_threshold"] == pytest.approx(-0.10)
    assert result["min_hold_hours"] == 24
    assert result["concurrency_cap"] == 5
    assert result["position_size_usdc"] == pytest.approx(500.0)


def test_strategy_a_validate_hot_params_missing_field():
    spec = _StrategyASpec()
    body = {k: v for k, v in _VALID_A_BODY.items() if k != "min_hold_hours"}
    with pytest.raises(ValueError, match="missing required hot param"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_bad_type():
    spec = _StrategyASpec()
    body = {**_VALID_A_BODY, "entry_threshold": "not_a_number"}
    with pytest.raises(ValueError, match="entry_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_entry_threshold_zero():
    spec = _StrategyASpec()
    # exclusive_min: must be > 0
    body = {**_VALID_A_BODY, "entry_threshold": 0.0}
    with pytest.raises(ValueError, match="entry_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_entry_threshold_too_large():
    spec = _StrategyASpec()
    body = {**_VALID_A_BODY, "entry_threshold": 6.0}
    with pytest.raises(ValueError, match="entry_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_exit_threshold_too_low():
    spec = _StrategyASpec()
    body = {**_VALID_A_BODY, "exit_threshold": -3.0}
    with pytest.raises(ValueError, match="exit_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_position_size_zero():
    spec = _StrategyASpec()
    # exclusive_min: must be > 0
    body = {**_VALID_A_BODY, "position_size_usdc": 0.0}
    with pytest.raises(ValueError, match="position_size_usdc"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_exit_ge_entry_cross_field():
    spec = _StrategyASpec()
    # exit == entry: cross-field violation
    body = {**_VALID_A_BODY, "exit_threshold": 0.50, "entry_threshold": 0.50}
    with pytest.raises(ValueError, match="exit_threshold must be strictly less than entry_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_exit_above_entry_cross_field():
    spec = _StrategyASpec()
    body = {**_VALID_A_BODY, "exit_threshold": 0.60, "entry_threshold": 0.50}
    with pytest.raises(ValueError, match="exit_threshold must be strictly less than entry_threshold"):
        spec.validate_hot_params(body)


def test_strategy_a_validate_hot_params_type_coercion():
    spec = _StrategyASpec()
    # Strings that represent numbers should be coerced
    body = {
        "entry_threshold": "0.50",
        "exit_threshold": "-0.10",
        "min_hold_hours": "24",
        "concurrency_cap": "5",
        "position_size_usdc": "500.0",
    }
    result = spec.validate_hot_params(body)
    assert result["min_hold_hours"] == 24
    assert isinstance(result["min_hold_hours"], int)
    assert result["concurrency_cap"] == 5
    assert isinstance(result["concurrency_cap"], int)


# ---------------------------------------------------------------------------
# _TwoPhaseDynamicSpec.validate_hot_params
# ---------------------------------------------------------------------------

_VALID_TPD_BODY = {
    "entry_threshold": 0.20,
    "base_min_hold_hours": 48,
    "safety_mult": 6.0,
    "cap_min_hold_hours": 360,
    "phase1_negative_patience": 48,
    "phase1_breakeven_cap_hours": 360,
    "phase2_exit_threshold": -0.05,
    "concurrency_cap": 5,
    "position_size_usdc": 500.0,
    "fee_round_trip_annual": 18.396,
}


def test_two_phase_dynamic_validate_hot_params_valid():
    spec = _TwoPhaseDynamicSpec()
    result = spec.validate_hot_params(_VALID_TPD_BODY)
    assert result["entry_threshold"] == pytest.approx(0.20)
    assert result["base_min_hold_hours"] == 48
    assert result["safety_mult"] == pytest.approx(6.0)
    assert result["cap_min_hold_hours"] == 360
    assert result["phase1_negative_patience"] == 48
    assert result["phase1_breakeven_cap_hours"] == 360
    assert result["phase2_exit_threshold"] == pytest.approx(-0.05)
    assert result["concurrency_cap"] == 5
    assert result["position_size_usdc"] == pytest.approx(500.0)
    assert result["fee_round_trip_annual"] == pytest.approx(18.396)


def test_two_phase_dynamic_validate_hot_params_missing_field():
    spec = _TwoPhaseDynamicSpec()
    body = {k: v for k, v in _VALID_TPD_BODY.items() if k != "fee_round_trip_annual"}
    with pytest.raises(ValueError, match="fee_round_trip_annual"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_bad_type():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "safety_mult": "oops"}
    with pytest.raises(ValueError, match="safety_mult"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_entry_threshold_zero():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "entry_threshold": 0.0}
    with pytest.raises(ValueError, match="entry_threshold"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_concurrency_cap_too_large():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "concurrency_cap": 99}
    with pytest.raises(ValueError, match="concurrency_cap"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_position_size_zero():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "position_size_usdc": 0.0}
    with pytest.raises(ValueError, match="position_size_usdc"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_base_min_hold_below_one():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "base_min_hold_hours": 0}
    with pytest.raises(ValueError, match="base_min_hold_hours"):
        spec.validate_hot_params(body)


def test_two_phase_dynamic_validate_hot_params_fee_zero():
    spec = _TwoPhaseDynamicSpec()
    body = {**_VALID_TPD_BODY, "fee_round_trip_annual": 0.0}
    with pytest.raises(ValueError, match="fee_round_trip_annual"):
        spec.validate_hot_params(body)


# ---------------------------------------------------------------------------
# apply_hot_params delegates correctly
# ---------------------------------------------------------------------------

def test_strategy_a_apply_hot_params(mocker):
    spec = _StrategyASpec()
    mock_strategy = mocker.MagicMock()
    validated = dict(_VALID_A_BODY)
    spec.apply_hot_params(mock_strategy, validated)
    mock_strategy.update_hot_params.assert_called_once_with(**validated)


def test_two_phase_dynamic_apply_hot_params(mocker):
    spec = _TwoPhaseDynamicSpec()
    mock_strategy = mocker.MagicMock()
    validated = dict(_VALID_TPD_BODY)
    spec.apply_hot_params(mock_strategy, validated)
    mock_strategy.update_hot_params.assert_called_once_with(**validated)
