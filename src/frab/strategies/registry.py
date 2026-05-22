"""Strategy registry: maps strategy name → factory that builds (params, strategy)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from frab.exchanges.atomic import AtomicExecutor
from frab.strategies.base import Strategy
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.two_phase_dynamic import TwoPhaseDynamic, TwoPhaseDynamicParams

if TYPE_CHECKING:
    from frab.engine.margin_manager import MarginManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HotFieldSpec:
    """Constraints for one hot-swappable parameter."""
    type: Literal["float", "int"]
    label: str
    min_value: float | int | None = None
    max_value: float | int | None = None
    exclusive_min: bool = False   # if True, value > min (not >=)
    exclusive_max: bool = False
    description: str = ""


class StrategySpec(Protocol):
    """What server.py needs from a registered strategy."""
    name: str          # canonical name (== Strategy.name)
    version: str       # canonical version (== Strategy.version)

    def build(
        self,
        coins: tuple[str, ...],
        params_override: dict | None,
        executor: AtomicExecutor,
        dry_run: bool = False,
        margin_manager: MarginManager | None = None,
    ) -> tuple[Strategy, dict]:
        """Returns (strategy_instance, params_json_dict_for_db)."""

    def hot_param_schema(self) -> dict[str, HotFieldSpec]:
        """Return mapping of hot-swappable field name → spec."""

    def validate_hot_params(self, body: dict) -> dict:
        """Validate raw request dict against schema. Returns clean dict with correct types.
        Raises ValueError with human-readable message on bad input.
        """

    def apply_hot_params(self, live_strategy: Strategy, validated: dict) -> None:
        """Call live_strategy.update_hot_params(**validated)."""


class _StrategyASpec:
    name = "strategy_a"
    version = "v1"

    _HOT_SCHEMA: dict[str, HotFieldSpec] = {
        "entry_threshold": HotFieldSpec(
            type="float", label="Entry threshold (annual)",
            min_value=0.0, max_value=5.0, exclusive_min=True,
            description="OPEN when 12h-MA funding rate exceeds this value",
        ),
        "exit_threshold": HotFieldSpec(
            type="float", label="Exit threshold (annual)",
            min_value=-2.0, max_value=5.0,
            description="CLOSE when instantaneous funding rate drops below this",
        ),
        "min_hold_hours": HotFieldSpec(
            type="int", label="Min hold (hours)",
            min_value=0, max_value=720,
            description="Lock position from exit for this many hours after entry",
        ),
        "concurrency_cap": HotFieldSpec(
            type="int", label="Concurrency cap",
            min_value=1, max_value=20,
            description="Max simultaneous positions across all coins",
        ),
        "position_size_usdc": HotFieldSpec(
            type="float", label="Position size (USDC)",
            min_value=0.0, max_value=1_000_000, exclusive_min=True,
            description="Notional per leg (spot + perp)",
        ),
    }

    def build(self, coins, params_override, executor, dry_run: bool = False, margin_manager: MarginManager | None = None):
        kwargs: dict = {"coins": tuple(coins)}
        if params_override:
            allowed = {
                "entry_threshold", "exit_threshold", "min_hold_hours",
                "signal_window_hours", "concurrency_cap", "position_size_usdc",
            }
            for k, v in params_override.items():
                if k in allowed:
                    kwargs[k] = v
                else:
                    logger.warning("strategy_a: ignoring unknown param %r", k)
        params = StrategyAParams(**kwargs)
        strategy = StrategyA(params=params, executor=executor, dry_run=dry_run, margin_manager=margin_manager)
        params_json = {
            "coins": list(params.coins),
            "entry_threshold": params.entry_threshold,
            "exit_threshold": params.exit_threshold,
            "min_hold_hours": params.min_hold_hours,
            "signal_window_hours": params.signal_window_hours,
            "concurrency_cap": params.concurrency_cap,
            "position_size_usdc": params.position_size_usdc,
        }
        return strategy, params_json

    def hot_param_schema(self) -> dict[str, HotFieldSpec]:
        return self._HOT_SCHEMA

    def validate_hot_params(self, body: dict) -> dict:
        validated: dict = {}
        for key, spec in self._HOT_SCHEMA.items():
            if key not in body:
                raise ValueError(f"missing required hot param {key!r}")
            raw = body[key]
            try:
                if spec.type == "float":
                    val: float | int = float(raw)
                else:
                    val = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: expected {spec.type}, got {raw!r}")
            if spec.min_value is not None:
                if spec.exclusive_min and val <= spec.min_value:
                    raise ValueError(f"{key}: must be > {spec.min_value}")
                if not spec.exclusive_min and val < spec.min_value:
                    raise ValueError(f"{key}: must be >= {spec.min_value}")
            if spec.max_value is not None:
                if spec.exclusive_max and val >= spec.max_value:
                    raise ValueError(f"{key}: must be < {spec.max_value}")
                if not spec.exclusive_max and val > spec.max_value:
                    raise ValueError(f"{key}: must be <= {spec.max_value}")
            validated[key] = val
        # Cross-field validation
        if validated["exit_threshold"] >= validated["entry_threshold"]:
            raise ValueError("exit_threshold must be strictly less than entry_threshold")
        return validated

    def apply_hot_params(self, live_strategy: Strategy, validated: dict) -> None:
        live_strategy.update_hot_params(**validated)


class _TwoPhaseDynamicSpec:
    name = "two_phase_dynamic"
    version = "v1"

    _HOT_SCHEMA: dict[str, HotFieldSpec] = {
        "entry_threshold": HotFieldSpec(
            type="float", label="Entry threshold (annual)",
            min_value=0.0, max_value=5.0, exclusive_min=True,
            description="OPEN when 12h-MA funding rate exceeds this value",
        ),
        "base_min_hold_hours": HotFieldSpec(
            type="int", label="Base min hold (hours)",
            min_value=1, max_value=720,
            description="Floor for dynamic min_hold formula",
        ),
        "safety_mult": HotFieldSpec(
            type="float", label="Safety multiplier",
            min_value=0.0, max_value=50.0, exclusive_min=True,
            description="Multiplier on theoretical break-even hold time",
        ),
        "cap_min_hold_hours": HotFieldSpec(
            type="int", label="Max min hold (hours)",
            min_value=24, max_value=4320,
            description="Ceiling for dynamic min_hold (30d = 720)",
        ),
        "phase1_negative_patience": HotFieldSpec(
            type="int", label="Phase 1 negative patience (hours)",
            min_value=1, max_value=720,
            description="Phase 1: give up after N consecutive negative-rate hours",
        ),
        "phase1_breakeven_cap_hours": HotFieldSpec(
            type="int", label="Phase 1 breakeven cap (hours)",
            min_value=24, max_value=4320,
            description="Phase 1: exit if breakeven at current rate would take > N hours",
        ),
        "phase2_exit_threshold": HotFieldSpec(
            type="float", label="Phase 2 exit threshold (annual)",
            min_value=-2.0, max_value=5.0,
            description="Phase 2 (in profit): exit when smoothed rate drops below this",
        ),
        "concurrency_cap": HotFieldSpec(
            type="int", label="Concurrency cap",
            min_value=1, max_value=20,
            description="Max simultaneous positions across all coins",
        ),
        "position_size_usdc": HotFieldSpec(
            type="float", label="Position size (USDC)",
            min_value=0.0, max_value=1_000_000, exclusive_min=True,
            description="Notional per leg",
        ),
        "fee_round_trip_annual": HotFieldSpec(
            type="float", label="Fee round-trip (annualized)",
            min_value=0.0, max_value=200.0, exclusive_min=True,
            description="Calibration constant for breakeven formula (~18.4 for HL retail)",
        ),
    }

    def build(self, coins, params_override, executor, dry_run: bool = False, margin_manager: MarginManager | None = None):
        kwargs: dict = {"coins": tuple(coins)}
        if params_override:
            allowed = {
                "entry_threshold", "signal_window_hours",
                "base_min_hold_hours", "safety_mult", "cap_min_hold_hours",
                "phase1_negative_patience", "phase1_breakeven_cap_hours",
                "phase2_exit_threshold", "concurrency_cap", "position_size_usdc",
                "fee_round_trip_annual",
            }
            for k, v in params_override.items():
                if k in allowed:
                    kwargs[k] = v
                else:
                    logger.warning("two_phase_dynamic: ignoring unknown param %r", k)
        params = TwoPhaseDynamicParams(**kwargs)
        strategy = TwoPhaseDynamic(params=params, executor=executor, dry_run=dry_run, margin_manager=margin_manager)
        params_json = {
            "coins": list(params.coins),
            "entry_threshold": params.entry_threshold,
            "signal_window_hours": params.signal_window_hours,
            "base_min_hold_hours": params.base_min_hold_hours,
            "safety_mult": params.safety_mult,
            "cap_min_hold_hours": params.cap_min_hold_hours,
            "phase1_negative_patience": params.phase1_negative_patience,
            "phase1_breakeven_cap_hours": params.phase1_breakeven_cap_hours,
            "phase2_exit_threshold": params.phase2_exit_threshold,
            "concurrency_cap": params.concurrency_cap,
            "position_size_usdc": params.position_size_usdc,
            "fee_round_trip_annual": params.fee_round_trip_annual,
        }
        return strategy, params_json

    def hot_param_schema(self) -> dict[str, HotFieldSpec]:
        return self._HOT_SCHEMA

    def validate_hot_params(self, body: dict) -> dict:
        validated: dict = {}
        for key, spec in self._HOT_SCHEMA.items():
            if key not in body:
                raise ValueError(f"missing required hot param {key!r}")
            raw = body[key]
            try:
                if spec.type == "float":
                    val: float | int = float(raw)
                else:
                    val = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: expected {spec.type}, got {raw!r}")
            if spec.min_value is not None:
                if spec.exclusive_min and val <= spec.min_value:
                    raise ValueError(f"{key}: must be > {spec.min_value}")
                if not spec.exclusive_min and val < spec.min_value:
                    raise ValueError(f"{key}: must be >= {spec.min_value}")
            if spec.max_value is not None:
                if spec.exclusive_max and val >= spec.max_value:
                    raise ValueError(f"{key}: must be < {spec.max_value}")
                if not spec.exclusive_max and val > spec.max_value:
                    raise ValueError(f"{key}: must be <= {spec.max_value}")
            validated[key] = val
        return validated

    def apply_hot_params(self, live_strategy: Strategy, validated: dict) -> None:
        live_strategy.update_hot_params(**validated)


_REGISTRY: dict[str, StrategySpec] = {
    "strategy_a": _StrategyASpec(),
    "two_phase_dynamic": _TwoPhaseDynamicSpec(),
}


def get_strategy_spec(name: str) -> StrategySpec:
    """Return registered StrategySpec or raise KeyError with helpful message."""
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown strategy name {name!r}. Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def parse_params_override(params_json: str) -> dict | None:
    """Parse strategy_params_json env var into dict.

    Returns None if input is empty/whitespace.
    Returns None and logs warning if JSON is invalid or not a dict (fail-soft).
    """
    if not params_json or not params_json.strip():
        return None
    try:
        parsed = json.loads(params_json)
    except json.JSONDecodeError as e:
        logger.warning("strategy_params_json: invalid JSON (%s) — using defaults", e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("strategy_params_json: not a JSON object — using defaults")
        return None
    return parsed
