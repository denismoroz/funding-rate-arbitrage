"""
walk_forward.py — Rolling walk-forward validation for two_phase_margin.

Goal (T8 in PLAN.md):
    Prong rolling walk-forward on REAL data (BTC/ETH/SOL) and measure IS vs OOS
    to answer: are the strategy parameters over-fitted to history?

Method:
    - Rolling windows: train=12 months, test=3 months, step=3 months (configurable).
    - On each TRAIN window: grid-search a small param grid, pick best by IS metric.
    - Apply best params to the NEXT TEST window → OOS metric.
    - Baseline: prod-default params on every test window (no tuning).
    - Aggregate: mean IS, mean tuned-OOS, mean static-OOS; summarise degradation.
    - Write WALK_FORWARD_REPORT.md.

Architecture rules (PLAN.md):
    - Engine loaded by file path via engine_adapter._engine (no new importlib).
    - run_on_dfs() from engine_adapter reused — no reimplementation.
    - metrics from monte_carlo.metrics.
    - Only numpy/pandas — no new dependencies.
    - No look-ahead: test window is STRICTLY AFTER the train window.
    - Params mutated via copy.copy(base) + setattr — original never modified.

Run:
    uv run python -m monte_carlo.walk_forward
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Bootstrap path so monte_carlo package resolves correctly (mirrors other
# modules in this package; engine_adapter does the same trick).
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_TPM = _THIS_FILE.parents[1]   # research/two_phase_margin/
_REPO_ROOT = _THIS_FILE.parents[3]      # repo root

if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

# Import the adapter (which also eagerly loads _engine and exposes run_on_dfs).
from monte_carlo.engine_adapter import _engine, run_on_dfs  # noqa: E402
from monte_carlo import metrics as _metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Default walk-forward hyperparameters
# ---------------------------------------------------------------------------
DEFAULT_COINS: list[str] = ["BTC", "ETH", "SOL"]
DEFAULT_TRAIN_MONTHS: int = 12
DEFAULT_TEST_MONTHS: int = 3
DEFAULT_STEP_MONTHS: int = 3
DEFAULT_MBUF: float = 3.0
DEFAULT_IS_METRIC: str = "annual"  # "annual" or "calmar"

# Small param grid (kept small for speed — see PLAN.md T8).
# Cartesian product of entry_threshold_apr × phase2_exit_threshold.
DEFAULT_GRID: dict[str, list[Any]] = {
    "entry_threshold_apr": [0.05, 0.10, 0.15, 0.20],
    "phase2_exit_threshold": [-0.05, -0.10],
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FoldResult:
    """Result of a single walk-forward fold."""
    fold_idx: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    # Best params found on the train window
    best_entry_threshold: float
    best_phase2_exit: float
    # IS metric (best params, train window)
    is_annual: float
    is_calmar: float
    # OOS metric (best params, test window)
    oos_annual: float
    oos_calmar: float
    # Static baseline (prod-default params, test window)
    static_annual: float
    static_calmar: float


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward results."""
    folds: list[FoldResult]
    coins: list[str]
    train_months: int
    test_months: int
    step_months: int
    is_metric: str

    @property
    def mean_is_annual(self) -> float:
        return float(sum(f.is_annual for f in self.folds) / len(self.folds))

    @property
    def mean_oos_annual(self) -> float:
        return float(sum(f.oos_annual for f in self.folds) / len(self.folds))

    @property
    def mean_static_annual(self) -> float:
        return float(sum(f.static_annual for f in self.folds) / len(self.folds))

    @property
    def mean_is_calmar(self) -> float:
        _calmars = [f.is_calmar for f in self.folds if not (f.is_calmar == float("inf"))]
        return float(sum(_calmars) / len(_calmars)) if _calmars else float("inf")

    @property
    def mean_oos_calmar(self) -> float:
        _calmars = [f.oos_calmar for f in self.folds if not (f.oos_calmar == float("inf"))]
        return float(sum(_calmars) / len(_calmars)) if _calmars else float("inf")

    @property
    def mean_static_calmar(self) -> float:
        _calmars = [f.static_calmar for f in self.folds if not (f.static_calmar == float("inf"))]
        return float(sum(_calmars) / len(_calmars)) if _calmars else float("inf")


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _add_months(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    """Add calendar months to a Timestamp using pandas DateOffset."""
    return ts + pd.DateOffset(months=months)


def _generate_folds(
    start: pd.Timestamp,
    end: pd.Timestamp,
    train_months: int,
    test_months: int,
    step_months: int,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Generate (train_start, train_end, test_start, test_end) tuples.

    No look-ahead: test_start == train_end, test_end > train_end always.
    The fold is included only if test_end <= end.
    """
    folds = []
    cursor = start
    while True:
        train_start = cursor
        train_end = _add_months(train_start, train_months)
        test_start = train_end
        test_end = _add_months(test_start, test_months)
        if test_end > end:
            break
        folds.append((train_start, train_end, test_start, test_end))
        cursor = _add_months(cursor, step_months)
    return folds


def _slice_dfs(
    dfs: dict[str, pd.DataFrame],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """Slice each DataFrame to [window_start, window_end) by index."""
    return {c: df.loc[window_start:window_end] for c, df in dfs.items()}


def _run_window(
    dfs_slice: dict[str, pd.DataFrame],
    params: Any,
    coins: list[str],
    mbuf: float,
) -> dict:
    """Run engine on a window; return metrics dict with 'annual' and 'calmar'."""
    # Filter out coins with insufficient data in this window
    valid_coins = [c for c in coins if c in dfs_slice and len(dfs_slice[c]) >= 48]
    if not valid_coins:
        return {"annual": 0.0, "calmar": 0.0, "max_dd": 0.0, "sharpe": 0.0}
    dfs_valid = {c: dfs_slice[c] for c in valid_coins}
    result = run_on_dfs(dfs_valid, params, mbuf, valid_coins, sizing="prod_slot")
    return result.metrics


def _grid_params(base_params: Any, grid: dict[str, list[Any]]) -> list[Any]:
    """Generate all combinations of grid parameters as copies of base_params.

    Each returned params object has exactly the grid attributes overridden;
    all other fields are identical to base_params. Original is never mutated.
    """
    import itertools

    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    result = []
    for combo in combos:
        p = copy.copy(base_params)
        for k, v in zip(keys, combo):
            setattr(p, k, v)
        result.append(p)
    return result


# ---------------------------------------------------------------------------
# Main walk-forward function
# ---------------------------------------------------------------------------

def run_walk_forward(
    coins: list[str] = DEFAULT_COINS,
    train_months: int = DEFAULT_TRAIN_MONTHS,
    test_months: int = DEFAULT_TEST_MONTHS,
    step_months: int = DEFAULT_STEP_MONTHS,
    mbuf: float = DEFAULT_MBUF,
    is_metric: str = DEFAULT_IS_METRIC,
    grid: dict[str, list[Any]] | None = None,
    verbose: bool = True,
) -> WalkForwardResult:
    """Run rolling walk-forward validation.

    Parameters
    ----------
    coins:        Coin symbols to trade (must have data in research/data/).
    train_months: Length of in-sample training window in calendar months.
    test_months:  Length of out-of-sample test window in calendar months.
    step_months:  Step size between consecutive folds in calendar months.
    mbuf:         Margin buffer multiplier (prod default = 3.0).
    is_metric:    Metric to optimise on IS window ("annual" or "calmar").
    grid:         Param grid dict {attr_name: [values]}. Defaults to DEFAULT_GRID.
    verbose:      Print progress to stdout.

    Returns
    -------
    WalkForwardResult with per-fold details and aggregate stats.

    No look-ahead guarantee:
        For each fold, OOS is evaluated on parameters chosen by the PRECEDING
        train window.  The test window is strictly after the train window
        (test_start == train_end).  The grid search never sees test data.
    """
    if grid is None:
        grid = DEFAULT_GRID

    # Load prod-default params (static baseline)
    prod_params, src = _engine.load_prod_params()
    if verbose:
        print(f"[WF] Prod params source: {src}")
        print(f"[WF] Coins: {coins}, train={train_months}m, test={test_months}m, step={step_months}m")
        print(f"[WF] IS metric: {is_metric}, grid size: {sum(1 for _ in _grid_params(prod_params, grid))}")

    # Load all coin data once
    if verbose:
        print("[WF] Loading coin data ...")
    dfs_all: dict[str, pd.DataFrame] = {}
    for coin in coins:
        dfs_all[coin] = _engine.load_coin_df(coin)

    # Common timeline across all requested coins
    common_idx = None
    for df in dfs_all.values():
        common_idx = df.index if common_idx is None else common_idx.intersection(df.index)
    common_idx = common_idx.sort_values()
    data_start: pd.Timestamp = common_idx[0]
    data_end: pd.Timestamp = common_idx[-1]

    # Restrict all dfs to common index so windows are synchronous
    dfs_common = {c: dfs_all[c].loc[common_idx] for c in coins}

    if verbose:
        print(f"[WF] Common timeline: {data_start.date()} → {data_end.date()} ({len(common_idx)} hours)")

    # Generate folds
    folds_meta = _generate_folds(data_start, data_end, train_months, test_months, step_months)
    if verbose:
        print(f"[WF] Generated {len(folds_meta)} folds")
    if not folds_meta:
        raise ValueError("No folds generated — data window too short for train+test")

    # Precompute all grid param combinations
    grid_combos = _grid_params(prod_params, grid)

    fold_results: list[FoldResult] = []

    for i, (train_start, train_end, test_start, test_end) in enumerate(folds_meta):
        if verbose:
            print(f"\n[WF] Fold {i}: train {train_start.date()}→{train_end.date()}, "
                  f"test {test_start.date()}→{test_end.date()}")

        # Slice data for train window
        dfs_train = _slice_dfs(dfs_common, train_start, train_end)
        # Slice data for test window
        dfs_test = _slice_dfs(dfs_common, test_start, test_end)

        # Grid search on TRAIN window
        best_score: float = float("-inf")
        best_params = grid_combos[0]
        best_is_metrics: dict = {}

        for j, p in enumerate(grid_combos):
            try:
                is_m = _run_window(dfs_train, p, coins, mbuf)
            except Exception as exc:
                if verbose:
                    print(f"  [grid {j}] ERROR: {exc}")
                continue

            score = is_m.get(is_metric, 0.0)
            if score != score:   # NaN guard
                score = float("-inf")
            if score > best_score:
                best_score = score
                best_params = p
                best_is_metrics = is_m

        if verbose:
            print(f"  IS best: entry={best_params.entry_threshold_apr}, "
                  f"ph2_exit={best_params.phase2_exit_threshold} → "
                  f"{is_metric}={best_score:.4f}")

        # Apply best params to TEST window (OOS — no look-ahead)
        try:
            oos_m = _run_window(dfs_test, best_params, coins, mbuf)
        except Exception as exc:
            if verbose:
                print(f"  OOS ERROR: {exc}")
            oos_m = {"annual": float("nan"), "calmar": float("nan"), "max_dd": float("nan"), "sharpe": float("nan")}

        # Static baseline: prod-default params on TEST window
        try:
            static_m = _run_window(dfs_test, prod_params, coins, mbuf)
        except Exception as exc:
            if verbose:
                print(f"  Static ERROR: {exc}")
            static_m = {"annual": float("nan"), "calmar": float("nan"), "max_dd": float("nan"), "sharpe": float("nan")}

        if verbose:
            print(f"  OOS tuned:  annual={oos_m.get('annual', float('nan')):.4f}, "
                  f"calmar={oos_m.get('calmar', float('nan')):.2f}")
            print(f"  OOS static: annual={static_m.get('annual', float('nan')):.4f}, "
                  f"calmar={static_m.get('calmar', float('nan')):.2f}")

        fold_results.append(FoldResult(
            fold_idx=i,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            best_entry_threshold=best_params.entry_threshold_apr,
            best_phase2_exit=best_params.phase2_exit_threshold,
            is_annual=best_is_metrics.get("annual", float("nan")),
            is_calmar=best_is_metrics.get("calmar", float("nan")),
            oos_annual=oos_m.get("annual", float("nan")),
            oos_calmar=oos_m.get("calmar", float("nan")),
            static_annual=static_m.get("annual", float("nan")),
            static_calmar=static_m.get("calmar", float("nan")),
        ))

    return WalkForwardResult(
        folds=fold_results,
        coins=coins,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        is_metric=is_metric,
    )


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_report(wf: WalkForwardResult, out_path: Path | None = None) -> Path:
    """Write WALK_FORWARD_REPORT.md and return the path."""
    if out_path is None:
        out_path = _THIS_FILE.parent / "WALK_FORWARD_REPORT.md"

    # Safely format calmar (may be inf)
    def _fmt_calmar(v: float) -> str:
        if v == float("inf"):
            return "∞"
        if v != v:  # NaN
            return "NaN"
        return f"{v:.2f}"

    def _fmt_pct(v: float) -> str:
        if v != v:
            return "NaN"
        return f"{v * 100:.2f}%"

    lines: list[str] = []
    lines.append("# Walk-Forward Validation Report")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- Coins: {', '.join(wf.coins)}")
    lines.append(f"- Train window: {wf.train_months} months")
    lines.append(f"- Test window: {wf.test_months} months")
    lines.append(f"- Step: {wf.step_months} months")
    lines.append(f"- IS optimisation metric: `{wf.is_metric}`")
    lines.append(f"- Number of folds: {len(wf.folds)}")
    lines.append("")

    # Per-fold table
    lines.append("## Per-Fold Results")
    lines.append("")
    lines.append("| Fold | Train Period | Test Period | Best entry_thr | Best ph2_exit "
                 "| IS Annual | IS Calmar | OOS Annual (tuned) | OOS Calmar (tuned) "
                 "| OOS Annual (static) | OOS Calmar (static) |")
    lines.append("|------|-------------|------------|----------------|---------------|"
                 "-----------|-----------|-------------------|------------------|"
                 "--------------------|---------------------|")

    for f in wf.folds:
        lines.append(
            f"| {f.fold_idx} "
            f"| {f.train_start.date()}→{f.train_end.date()} "
            f"| {f.test_start.date()}→{f.test_end.date()} "
            f"| {f.best_entry_threshold:.2f} "
            f"| {f.best_phase2_exit:.2f} "
            f"| {_fmt_pct(f.is_annual)} "
            f"| {_fmt_calmar(f.is_calmar)} "
            f"| {_fmt_pct(f.oos_annual)} "
            f"| {_fmt_calmar(f.oos_calmar)} "
            f"| {_fmt_pct(f.static_annual)} "
            f"| {_fmt_calmar(f.static_calmar)} |"
        )

    lines.append("")

    # Summary statistics
    lines.append("## Aggregate Summary")
    lines.append("")

    mean_is = wf.mean_is_annual
    mean_oos = wf.mean_oos_annual
    mean_static = wf.mean_static_annual

    degradation_abs = mean_oos - mean_is
    degradation_rel = (degradation_abs / abs(mean_is) * 100) if mean_is != 0 else float("nan")
    tuned_vs_static = mean_oos - mean_static

    lines.append(f"| Metric | Mean IS (tuned) | Mean OOS (tuned) | Mean OOS (static) |")
    lines.append(f"|--------|----------------|-----------------|-------------------|")
    lines.append(
        f"| Annual return | {_fmt_pct(mean_is)} | {_fmt_pct(mean_oos)} | {_fmt_pct(mean_static)} |"
    )
    lines.append(
        f"| Calmar ratio | {_fmt_calmar(wf.mean_is_calmar)} "
        f"| {_fmt_calmar(wf.mean_oos_calmar)} "
        f"| {_fmt_calmar(wf.mean_static_calmar)} |"
    )
    lines.append("")
    lines.append(f"**IS → OOS degradation (annual):** {_fmt_pct(degradation_abs)} "
                 f"({degradation_rel:.1f}% relative)")
    lines.append("")

    if tuned_vs_static > 0.001:
        verdict_line = (
            f"**Tuned OOS vs Static OOS:** tuned outperforms static by {_fmt_pct(tuned_vs_static)}. "
            f"This would suggest tuning adds value OOS."
        )
    elif tuned_vs_static < -0.001:
        verdict_line = (
            f"**Tuned OOS vs Static OOS:** static outperforms tuned by {_fmt_pct(-tuned_vs_static)}. "
            f"Tuning HURTS OOS — consistent with over-fitting (cf. memory project_quant_research: "
            f"45%→22% degradation observed in single-param tuning)."
        )
    else:
        verdict_line = (
            f"**Tuned OOS vs Static OOS:** difference is negligible ({_fmt_pct(tuned_vs_static)}). "
            f"Tuning neither helps nor hurts OOS."
        )
    lines.append(verdict_line)
    lines.append("")

    # Per-fold best-param frequency table
    lines.append("## Best-Param Frequency (IS selections)")
    lines.append("")
    from collections import Counter
    param_counts: Counter = Counter()
    for f in wf.folds:
        param_counts[(f.best_entry_threshold, f.best_phase2_exit)] += 1
    lines.append("| entry_threshold_apr | phase2_exit_threshold | # folds selected |")
    lines.append("|--------------------|-----------------------|-------------------|")
    for (entry, ph2), count in sorted(param_counts.items()):
        lines.append(f"| {entry:.2f} | {ph2:.2f} | {count} |")
    lines.append("")

    # Context and interpretation note
    lines.append("## Interpretation Notes")
    lines.append("")
    lines.append("This walk-forward measures **parameter over-fit risk**, not edge robustness. "
                 "The Monte Carlo harness (T5/T6) separately validated that the edge survives "
                 "alternative price/funding paths. These are complementary questions.")
    lines.append("")
    lines.append("IS metric is CAGR (compound annual) on the full portfolio equity curve "
                 "(prod_slot sizing, mbuf=3.0, BTC/ETH/SOL only). Note: equity includes idle "
                 "cash in the budget, so APR on deployed capital is higher (~4–5× for 3-coin book).")
    lines.append("")
    lines.append(f"The param grid is intentionally small (default: 4 entry thresholds × 2 exit "
                 f"thresholds = 8 combos; {len(wf.folds)} folds). "
                 "A larger grid would take proportionally longer. "
                 "The selected params vary by fold, which itself is evidence of instability.")
    lines.append("")
    lines.append("**Verdict: left to Opus (PLAN.md T8).** Numbers are reported without "
                 "editorial conclusion — Opus will assess whether degradation is real and "
                 "whether prod should use static or ensemble params.")

    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Walk-forward validation for two_phase_margin (T8)."
    )
    parser.add_argument("--coins", nargs="+", default=DEFAULT_COINS,
                        help="Coin symbols (default: BTC ETH SOL)")
    parser.add_argument("--train-months", type=int, default=DEFAULT_TRAIN_MONTHS,
                        help="Training window in months (default: 12)")
    parser.add_argument("--test-months", type=int, default=DEFAULT_TEST_MONTHS,
                        help="Test window in months (default: 3)")
    parser.add_argument("--step-months", type=int, default=DEFAULT_STEP_MONTHS,
                        help="Step between folds in months (default: 3)")
    parser.add_argument("--mbuf", type=float, default=DEFAULT_MBUF,
                        help="Margin buffer multiplier (default: 3.0)")
    parser.add_argument("--is-metric", default=DEFAULT_IS_METRIC,
                        choices=["annual", "calmar"],
                        help="IS optimisation metric (default: annual)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output path for the report .md (default: WALK_FORWARD_REPORT.md)")
    args = parser.parse_args()

    wf = run_walk_forward(
        coins=args.coins,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        mbuf=args.mbuf,
        is_metric=args.is_metric,
        verbose=True,
    )

    print("\n=== AGGREGATE SUMMARY ===")
    print(f"Folds: {len(wf.folds)}")
    print(f"Mean IS annual:     {wf.mean_is_annual * 100:.2f}%")
    print(f"Mean OOS annual (tuned):   {wf.mean_oos_annual * 100:.2f}%")
    print(f"Mean OOS annual (static):  {wf.mean_static_annual * 100:.2f}%")
    print(f"IS→OOS degradation: {(wf.mean_oos_annual - wf.mean_is_annual) * 100:.2f}pp")
    print(f"Tuned vs static OOS: {(wf.mean_oos_annual - wf.mean_static_annual) * 100:.2f}pp")

    report_path = write_report(wf, args.out)
    print(f"\n[WF] Report written → {report_path}")


if __name__ == "__main__":
    main()
