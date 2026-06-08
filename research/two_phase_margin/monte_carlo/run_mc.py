"""
run_mc.py — Monte-Carlo orchestrator CLI (T5).

Column-name contract / Rule-5 denominator GAP note
---------------------------------------------------
The columns ``annual``, ``max_dd``, ``calmar``, ``sharpe`` (from metrics.summarize)
are computed on RunResult.equity, which is the TOTAL PORTFOLIO equity curve
(budget_cap_usdc scale, ~$1 000 idle cash included).  This means:

  * max_dd  — correct: it measures the largest relative drawdown of the full
    account, which is the right risk metric for a margin account.
  * annual  — understated on a per-deployed-capital basis: the idle cash
    dilutes the return.  With $345 deployed / $1 000 budget, the "real" APR
    on occupied capital is ~2.9× larger than what ``annual`` shows.

The raw engine columns ``annual_pct_raw`` / ``max_dd_pct_raw`` are identical
in scale (also on full-portfolio equity) but use the linear APR / percent
conventions from simulate() rather than the CAGR / fraction conventions of
metrics.py.  They are preserved here so T6/T7 can cross-check.

``total_funding``, ``total_fees``, ``final_equity`` are raw USDC P&L
accumulators from simulate(); T6/T7 can use them to reconstruct occupied-
capital APR without re-running the engine:

    occ_capital_apr ≈ total_funding / avg_deployed_usdc / horizon_years

The column ``annual_pct_raw`` (linear, %) from simulate() divided by the
occupied-capital fraction gives the true per-capital APR; this reconstruction
is left to T6.

Usage
-----
    uv run python research/two_phase_margin/monte_carlo/run_mc.py \\
        --n 200 \\
        --horizon-days 365 \\
        --seed 42 \\
        --generator parametric \\
        --coins BTC,ETH,SOL,HYPE,PURR \\
        --mbuf 3.0 \\
        --params prod \\
        --jobs 4 \\
        --out-dir research/two_phase_margin/monte_carlo/results

Output
------
    results/mc_{generator}_{timestamp}.csv  — one row per path
    Prints a brief summary to stdout (median/5th/95th pct of annual & max_dd,
    P(annual<0)).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# sys.path: make `monte_carlo` importable (same trick as engine_adapter / tests)
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
# run_mc.py lives at: <REPO>/research/two_phase_margin/monte_carlo/run_mc.py
_MONTE_CARLO_DIR = _THIS_FILE.parent                # …/monte_carlo/
_RESEARCH_TPM = _MONTE_CARLO_DIR.parent             # …/two_phase_margin/
_REPO_ROOT = _RESEARCH_TPM.parents[1]               # repo root

if str(_RESEARCH_TPM) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_TPM))

# ---------------------------------------------------------------------------
# Lazy imports (generators / adapter load the engine — keep at module level for
# subprocess workers, but only after sys.path is fixed)
# ---------------------------------------------------------------------------

from monte_carlo.engine_adapter import _engine, run_on_dfs  # noqa: E402

_DEFAULT_CALIB_DIR = _MONTE_CARLO_DIR / "calibration"
_DEFAULT_DATA_DIR = _REPO_ROOT / "research" / "data"
_DEFAULT_OUT_DIR = _MONTE_CARLO_DIR / "results"
_DEFAULT_COINS = ["BTC", "ETH", "SOL", "HYPE", "PURR"]

# Output columns (in declaration order — documented for T6/T7)
_COLUMNS = [
    "path_seed",
    "generator",
    "horizon_h",
    "coins",
    "mbuf",
    # metrics.summarize (CAGR/fraction, full-portfolio equity — see Rule-5 note)
    "annual",
    "max_dd",
    "calmar",
    "sharpe",
    # simulate() raw scalars (linear APR / percent, full-portfolio — same denominator)
    "annual_pct_raw",
    "max_dd_pct_raw",
    # P&L decomposition for occupied-capital reconstruction (T6/T7)
    "total_funding",
    "total_fees",
    "final_equity",
    # Exit counters
    "n_liquidations",
    "n_forced_closes",
    "n_phase1_neg_exits",
    "n_phase1_cap_exits",
    "n_phase1_negstop_exits",
    "n_phase2_exits",
]


# ---------------------------------------------------------------------------
# Per-path worker (must be top-level for multiprocessing.Pool pickling)
# ---------------------------------------------------------------------------

def _run_single_path(args: tuple) -> dict[str, Any]:
    """Run one MC path and return a result dict.

    This function is called by multiprocessing workers and must be picklable
    (top-level, no closures).  All state needed is passed through ``args``.

    Arguments (packed into a single tuple for pool.map compatibility):
        path_seed    int
        generator    str   "parametric" | "bootstrap"
        horizon_h    int
        coins        list[str]
        mbuf         float
        params_mode  str   "prod" | "defaults"
        calib_dir    str   path for parametric
        data_dir     str   path for bootstrap
    """
    (
        path_seed,
        generator,
        horizon_h,
        coins,
        mbuf,
        params_mode,
        calib_dir,
        data_dir,
    ) = args

    # ── Params ───────────────────────────────────────────────────────────────
    if params_mode == "prod":
        base_params, _ = _engine.load_prod_params()
        params = _engine.TwoPhaseParams(
            coins=coins,
            entry_threshold_apr=base_params.entry_threshold_apr,
            phase2_exit_threshold=base_params.phase2_exit_threshold,
            base_min_hold_hours=base_params.base_min_hold_hours,
            cap_min_hold_hours=base_params.cap_min_hold_hours,
            safety_mult=base_params.safety_mult,
            signal_window_hours=base_params.signal_window_hours,
            concurrency_cap=base_params.concurrency_cap,
            position_size_usdc=100.0,     # research-standard (same as sweep)
            budget_cap_usdc=1000.0,       # research-standard
            margin_buffer_factor=mbuf,
            phase1_negative_patience=base_params.phase1_negative_patience,
            phase1_breakeven_cap_hours=base_params.phase1_breakeven_cap_hours,
            # neg_stop defaults match prod (added 2026-06-08, commit 9e17c8a)
        )
    else:
        # "defaults" — TwoPhaseParams() with overridden coins/mbuf
        params = _engine.TwoPhaseParams(
            coins=coins,
            margin_buffer_factor=mbuf,
            position_size_usdc=100.0,
            budget_cap_usdc=1000.0,
        )

    # ── Generate dfs ─────────────────────────────────────────────────────────
    if generator == "parametric":
        from monte_carlo.generators.parametric import generate  # noqa: PLC0415
        dfs = generate(
            calib_dir=calib_dir,
            horizon_h=horizon_h,
            seed=path_seed,
            coins=coins,
        )
    elif generator == "bootstrap":
        from monte_carlo.generators.bootstrap import generate  # noqa: PLC0415
        dfs = generate(
            data_dir=data_dir,
            horizon_h=horizon_h,
            seed=path_seed,
            coins=coins,
        )
    else:
        raise ValueError(f"Unknown generator: {generator!r}")

    # ── Run engine ────────────────────────────────────────────────────────────
    result = run_on_dfs(dfs, params, mbuf=mbuf, coins=coins, position_size=100.0)

    raw = result.raw
    m = result.metrics

    return {
        "path_seed": path_seed,
        "generator": generator,
        "horizon_h": horizon_h,
        "coins": ",".join(coins),
        "mbuf": mbuf,
        # metrics.summarize — CAGR/fraction, full-portfolio equity (Rule-5 GAP)
        "annual": m["annual"],
        "max_dd": m["max_dd"],
        "calmar": m["calmar"],
        "sharpe": m["sharpe"],
        # simulate() raw — linear APR in percent, full-portfolio equity
        "annual_pct_raw": raw.get("annual_pct", float("nan")),
        "max_dd_pct_raw": raw.get("max_dd_pct", float("nan")),
        # P&L decomposition — for T6/T7 occupied-capital APR reconstruction
        "total_funding": raw.get("total_funding", float("nan")),
        "total_fees": raw.get("total_fees", float("nan")),
        "final_equity": raw.get("final_equity", float("nan")),
        # Exit counters
        "n_liquidations": raw.get("n_liquidations", 0),
        "n_forced_closes": raw.get("n_forced_closes", 0),
        "n_phase1_neg_exits": raw.get("n_phase1_neg_exits", 0),
        "n_phase1_cap_exits": raw.get("n_phase1_cap_exits", 0),
        "n_phase1_negstop_exits": raw.get("n_phase1_negstop_exits", 0),
        "n_phase2_exits": raw.get("n_phase2_exits", 0),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    n: int = 200,
    horizon_days: int = 365,
    seed: int = 42,
    generator: str = "parametric",
    coins: list[str] | None = None,
    mbuf: float = 3.0,
    params_mode: str = "prod",
    jobs: int | None = None,
    out_dir: str | Path | None = None,
    calib_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Run N Monte-Carlo iterations and return a DataFrame of per-path results.

    Each path i uses seed = base_seed + i for full determinism independent of
    the number of jobs (parallel workers each compute their own seed-i path).

    Rule-5 denominator GAP: ``annual`` / ``max_dd`` / ``calmar`` / ``sharpe``
    are computed on the FULL PORTFOLIO equity curve (budget_cap_usdc scale),
    not on deployed capital.  See module docstring for details and how
    ``total_funding`` / ``total_fees`` / ``final_equity`` can be used by T6/T7
    to reconstruct the occupied-capital APR.

    Arguments:
        n:            Number of MC paths.
        horizon_days: Horizon length in days (converted to hours: days * 24).
        seed:         Base random seed; path i uses seed + i.
        generator:    "parametric" or "bootstrap".
        coins:        List of coin symbols (default BTC,ETH,SOL,HYPE,PURR).
        mbuf:         Margin buffer multiplier.
        params_mode:  "prod" (load_prod_params) or "defaults" (TwoPhaseParams()).
        jobs:         Number of worker processes (None → os.cpu_count(); 1 →
                      sequential, deterministic single-process execution).
        out_dir:      Directory to write results CSV (default: results/).
        calib_dir:    Override for calibration directory (parametric generator).
        data_dir:     Override for real-data directory (bootstrap generator).

    Returns:
        pd.DataFrame with columns matching _COLUMNS, N rows.
        Also writes results/mc_{generator}_{timestamp}.csv.
    """
    if coins is None:
        coins = list(_DEFAULT_COINS)
    if jobs is None:
        jobs = os.cpu_count() or 1
    if out_dir is None:
        out_dir = _DEFAULT_OUT_DIR
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    calib_str = str(calib_dir if calib_dir is not None else _DEFAULT_CALIB_DIR)
    data_str = str(data_dir if data_dir is not None else _DEFAULT_DATA_DIR)

    horizon_h = horizon_days * 24

    # Build argument list — each element is the args tuple for _run_single_path
    path_args = [
        (
            seed + i,     # path_seed
            generator,
            horizon_h,
            list(coins),
            mbuf,
            params_mode,
            calib_str,
            data_str,
        )
        for i in range(n)
    ]

    t0 = time.monotonic()
    rows: list[dict[str, Any]] = []

    if jobs == 1:
        # Sequential — simplest for debugging and for determinism tests
        for args in path_args:
            try:
                rows.append(_run_single_path(args))
            except Exception as exc:  # noqa: BLE001
                path_seed = args[0]
                print(
                    f"[WARN] path seed={path_seed} failed: {exc}\n"
                    + traceback.format_exc(),
                    file=sys.stderr,
                )
    else:
        with Pool(processes=jobs) as pool:
            results = pool.map(_run_single_path, path_args)
        rows = list(results)

    elapsed = time.monotonic() - t0

    df = pd.DataFrame(rows, columns=_COLUMNS)

    # ── Write CSV ─────────────────────────────────────────────────────────────
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"mc_{generator}_{ts}.csv"
    df.to_csv(csv_path, index=False)

    # ── Print summary ─────────────────────────────────────────────────────────
    _print_summary(df, generator, n, horizon_days, elapsed, csv_path)

    return df


def _print_summary(
    df: pd.DataFrame,
    generator: str,
    n: int,
    horizon_days: int,
    elapsed: float,
    csv_path: Path,
) -> None:
    """Print a brief summary to stdout."""
    ann = df["annual"]
    mdd = df["max_dd"]

    p_neg = float((ann < 0).mean())
    med_ann = float(ann.median())
    p05_ann = float(ann.quantile(0.05))
    p95_ann = float(ann.quantile(0.95))
    med_mdd = float(mdd.median())
    p05_mdd = float(mdd.quantile(0.05))
    p95_mdd = float(mdd.quantile(0.95))

    print(
        f"\n=== MC Summary: generator={generator}, n={n}, "
        f"horizon={horizon_days}d, t={elapsed:.1f}s ===\n"
        f"  annual  (CAGR, full-portfolio):  "
        f"  median={med_ann:+.4f}  p05={p05_ann:+.4f}  p95={p95_ann:+.4f}\n"
        f"  max_dd  (fraction, full-portfolio): "
        f" median={med_mdd:.6f}  p05={p05_mdd:.6f}  p95={p95_mdd:.6f}\n"
        f"  P(annual < 0) = {p_neg:.1%}\n"
        f"  [Rule-5 GAP] annual is on full-portfolio equity (~$1000 budget); "
        f"occupied-capital APR is ~2-3× higher (see total_funding column).\n"
        f"  Output: {csv_path}\n"
    )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monte-Carlo runner for two_phase_margin strategy.\n"
            "Runs N independent paths through the prod engine and writes "
            "results to CSV."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--n", type=int, default=200,
        help="Number of MC paths (default: 200)",
    )
    parser.add_argument(
        "--horizon-days", type=int, default=365,
        help="Horizon in days per path; converted to hours × 24 (default: 365)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Base random seed; path i uses seed+i (default: 42)",
    )
    parser.add_argument(
        "--generator", choices=["parametric", "bootstrap"], default="parametric",
        help="Synthetic data generator (default: parametric)",
    )
    parser.add_argument(
        "--coins", type=str, default="BTC,ETH,SOL,HYPE,PURR",
        help="Comma-separated coin list (default: BTC,ETH,SOL,HYPE,PURR)",
    )
    parser.add_argument(
        "--mbuf", type=float, default=3.0,
        help="Margin buffer multiplier (default: 3.0)",
    )
    parser.add_argument(
        "--params", choices=["prod", "defaults"], default="prod",
        dest="params_mode",
        help="Param source: prod=load_prod_params(), defaults=TwoPhaseParams() (default: prod)",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="Worker processes (default: os.cpu_count(); 1=sequential)",
    )
    parser.add_argument(
        "--out-dir", type=str, default=str(_DEFAULT_OUT_DIR),
        help=f"Output directory for CSV results (default: {_DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--calib-dir", type=str, default=None,
        help="Override calibration directory for parametric generator",
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Override real-data directory for bootstrap generator",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    run(
        n=args.n,
        horizon_days=args.horizon_days,
        seed=args.seed,
        generator=args.generator,
        coins=coins,
        mbuf=args.mbuf,
        params_mode=args.params_mode,
        jobs=args.jobs,
        out_dir=args.out_dir,
        calib_dir=args.calib_dir,
        data_dir=args.data_dir,
    )
