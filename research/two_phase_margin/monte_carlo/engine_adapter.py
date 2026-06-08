"""
engine_adapter.py — Thin wrapper around research/two_phase_margin.py::simulate().

This is the ONLY point of contact between the Monte-Carlo harness and the
production engine.  It loads the engine strictly by file path (not by module
name) to avoid the two_phase_margin.py / two_phase_margin/ package name collision
described in PLAN.md §Architecture rule 2.

=== Contract of simulate() (as of commit 9e17c8a, 2026-06-08) ===

Signature:
    simulate(
        coins: list[str],
        params: TwoPhaseParams,
        margin_buffer_x: float | None = None,   # overrides params.margin_buffer_factor
        position_size: float | None = None,      # overrides params.position_size_usdc
        restrict_start: pd.Timestamp | None = None,
        restrict_end: pd.Timestamp | None = None,
        neg_overrides_min_hold: bool = False,
        _dfs_override: dict | None = None,       # MC adapter path (T1 addition)
    ) -> dict

Return dict keys:
    "equity"          pd.Series  — hourly equity of the full portfolio (spot_cash +
                                   perp_cash + spot_value + unrealised_pnl), indexed
                                   by hourly pd.Timestamp (UTC).  This is the SAME
                                   series from which max_dd_pct is computed inside
                                   simulate() via:
                                       peak = eq.cummax()
                                       drawdowns = (eq - peak) / peak
                                       max_dd_pct = float((-drawdowns.min()) * 100)
                                   Note: equity = TOTAL portfolio (budget_cap_usdc
                                   scale), NOT just deployed capital.  max_dd_pct is
                                   therefore a % of total portfolio, not deployed.
                                   The adapter exposes this series as RunResult.equity
                                   for metrics.summarize(); be aware that
                                   metrics.max_dd() will return a fraction (e.g. 7.82e-4
                                   for 0.0782%) while simulate()'s max_dd_pct is in %.
    "annual_pct"      float      — simple linear APR in percent:
                                   (end/start - 1) / period_years * 100
                                   NOT CAGR.  metrics.annualized_return() uses CAGR,
                                   so the two will differ slightly on multi-year windows.
    "max_dd_pct"      float      — as above, in PERCENT (multiply by 0.01 to get fraction)
    "sharpe"          float      — annualised Sharpe on hourly pct_change returns
    "sortino"         float
    "final_equity"    float
    "total_funding"   float
    "total_fees"      float
    "n_liquidations"  int
    "n_top_ups"       int
    "n_forced_closes" int
    "n_skipped_opens" int
    "n_phase1_neg_exits"     int
    "n_phase1_cap_exits"     int
    "n_phase1_negstop_exits" int
    "n_phase2_exits"         int
    "n_minhold_guard"        int
    "period_start"    str  (YYYY-MM-DD)
    "period_end"      str  (YYYY-MM-DD)
    "n_hours"         int
    "per_coin"        dict[coin → dict with n_opens, funding_gross, fees_paid, …]

add_signals():
    Computes a rolling-mean of fundingRate over params.signal_window_hours and
    annualises it (× 8760) into a "signal" column.  Must be called after
    restrict_start/restrict_end clipping and BEFORE simulate() runs the main loop.
    run_on_dfs() calls it explicitly (simulate() also calls it internally when using
    the _dfs_override path — the call is idempotent since it just overwrites "signal").

=== Metric scale notes ===

simulate() max_dd_pct  → PERCENT   e.g. 0.0782 means 0.0782%
metrics.max_drawdown() → FRACTION  e.g. 0.000782 means 0.0782%
To compare: raw["max_dd_pct"] / 100 ≈ metrics.max_dd(result.equity)
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Locate repo root and load the engine by file path (PLAN.md rule 2)
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
# engine_adapter.py lives at:
#   <REPO>/research/two_phase_margin/monte_carlo/engine_adapter.py
# two_phase_margin.py lives at:
#   <REPO>/research/two_phase_margin.py
_REPO_ROOT = _THIS_FILE.parents[3]  # up 3 levels: monte_carlo → two_phase_margin → research → REPO
_ENGINE_PATH = _REPO_ROOT / "research" / "two_phase_margin.py"


def _load_engine():
    """Load research/two_phase_margin.py as module 'tpm_engine' (by file path)."""
    if "tpm_engine" in sys.modules:
        return sys.modules["tpm_engine"]
    spec = importlib.util.spec_from_file_location("tpm_engine", _ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load engine from {_ENGINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tpm_engine"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Eagerly load so import errors surface at import time, not at call time.
_engine = _load_engine()

# ---------------------------------------------------------------------------
# Import metrics from the monte_carlo package
# ---------------------------------------------------------------------------
# research/two_phase_margin/ must be on sys.path so `monte_carlo` resolves to
# the package (same trick as test_metrics.py).
_RESEARCH_TPM = _THIS_FILE.parents[1]  # research/two_phase_margin/
if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

from monte_carlo import metrics as _metrics  # noqa: E402


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Result of a single simulation run through the engine adapter.

    equity: hourly equity pd.Series (same series from which max_dd_pct is derived
            inside simulate()).  Index: UTC hourly timestamps; values: portfolio
            equity in USDC (total portfolio scale — includes idle cash).
    metrics: output of metrics.summarize(equity) — all values in FRACTION/CAGR
             conventions (see module docstring for scale notes vs. simulate()).
    raw:    the full dict returned by simulate() — use raw["annual_pct"],
            raw["max_dd_pct"] etc. for production-engine-canonical values.
    """

    equity: pd.Series
    metrics: dict
    raw: Any


def run_on_dfs(
    dfs: dict[str, pd.DataFrame],
    params: Any,
    mbuf: float,
    coins: list[str],
    position_size: float = 100.0,
    sizing: str = "prod_slot",
) -> RunResult:
    """Run the two_phase engine on the provided dfs (real or synthetic).

    Arguments:
        dfs:           dict[coin → DataFrame] with at least columns [close, fundingRate]
                       and a hourly UTC DatetimeIndex.  The adapter is agnostic to
                       whether these are real or synthetic.
        params:        TwoPhaseParams instance (or duck-typed equivalent with same attrs).
        mbuf:          margin buffer multiplier (e.g. 3.0 for U-prod config).
        coins:         list of coin symbols to trade (must be keys in dfs).
        position_size: per-position size in USDC (used only when sizing="flat").
        sizing:        "prod_slot" (default for MC) — prod-accurate sizing: per-coin
                       notional = slot / (1 + mbuf / lev_c) so footprint = slot = budget/K
                       exactly for every open position.
                       "flat" — legacy behaviour: fixed notional = position_size for every
                       coin (original research-sweep mode).  Pass sizing="flat" for
                       regression anchor tests that must reproduce TWOPHASE_MARGIN_aggregate.csv.

    Returns:
        RunResult(equity, metrics, raw).

    Implementation notes:
    - add_signals() is called explicitly here (before simulate) to apply the
      rolling-mean window from params.signal_window_hours.  simulate() will call it
      again internally via the _dfs_override path — this is idempotent.
    - simulate() is called with _dfs_override=dfs so it skips its internal
      load_coin_df loop and uses the supplied DataFrames directly.
    - No logic from simulate() is reimplemented here — this is a pure pass-through.
    """
    # Apply signals (rolling fundingRate MA → annualised signal column).
    # We operate on copies so the caller's dfs are not mutated.
    dfs_with_signals: dict[str, pd.DataFrame] = {}
    for coin in coins:
        if coin not in dfs:
            continue
        dfs_with_signals[coin] = dfs[coin].copy()
    _engine.add_signals(dfs_with_signals, params.signal_window_hours)

    # Call the engine via the _dfs_override path (additive parameter added in T1).
    raw = _engine.simulate(
        coins,
        params,
        margin_buffer_x=mbuf,
        position_size=position_size,
        _dfs_override=dfs_with_signals,
        sizing=sizing,
    )

    # Extract the equity curve (already in the return dict since engine always computes it).
    equity: pd.Series = raw["equity"]

    # Summarise with metrics from T0.
    summary = _metrics.summarize(equity)

    return RunResult(equity=equity, metrics=summary, raw=raw)
