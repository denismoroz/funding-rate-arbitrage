"""Strategy registry: maps strategy name → factory that builds (params, strategy)."""
from __future__ import annotations

import json
import logging
from typing import Protocol

from frab.exchanges.base import Executor
from frab.strategies.base import Strategy
from frab.strategies.strategy_a import StrategyA, StrategyAParams
from frab.strategies.two_phase_dynamic import TwoPhaseDynamic, TwoPhaseDynamicParams

logger = logging.getLogger(__name__)


class StrategySpec(Protocol):
    """What server.py needs from a registered strategy."""
    name: str          # canonical name (== Strategy.name)
    version: str       # canonical version (== Strategy.version)

    def build(
        self,
        coins: tuple[str, ...],
        params_override: dict | None,
        executor: Executor,
    ) -> tuple[Strategy, dict]:
        """Returns (strategy_instance, params_json_dict_for_db)."""


class _StrategyASpec:
    name = "strategy_a"
    version = "v1"

    def build(self, coins, params_override, executor):
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
        strategy = StrategyA(params=params, executor=executor)
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


class _TwoPhaseDynamicSpec:
    name = "two_phase_dynamic"
    version = "v1"

    def build(self, coins, params_override, executor):
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
        strategy = TwoPhaseDynamic(params=params, executor=executor)
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
