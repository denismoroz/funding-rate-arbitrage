"""
report.py — Aggregation and human-readable report generation (T6).

Entry-point:
    uv run python research/two_phase_margin/monte_carlo/report.py \\
        --results-dir research/two_phase_margin/monte_carlo/results \\
        --out research/two_phase_margin/monte_carlo/MONTE_CARLO_REPORT.md

Input:  results/mc_*.csv produced by run_mc.py (T5).
Output: MONTE_CARLO_REPORT.md with:
  - Percentile tables (p05/p25/median/p75/p95 + min/max) for annual, max_dd,
    calmar, sharpe.
  - P(annual < 0), P(max_dd > 0.01), P(max_dd > 0.05), CVaR-style tail.
  - Exit-mix averages (neg/cap/negstop/phase2 per path).
  - Side-by-side parametric vs bootstrap comparison when both are available.
  - Single-path anchor from research/TWOPHASE_MARGIN_aggregate.csv
    (U-prod, buf=3) for contrast.
  - Occupied-capital reframe section (T7 placeholder, no invented multiplier).
  - Optional matplotlib histograms (best-effort; skipped gracefully if absent).

Rule-5 denominator GAP (PLAN.md §Architecture rule 5):
    annual / max_dd / calmar / sharpe are on FULL PORTFOLIO equity
    (~$1 000 budget incl. idle cash).  This is clearly labelled throughout.
    occupied-capital APR reconstruction is deferred to T7.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_MONTE_CARLO_DIR = _THIS_FILE.parent          # …/monte_carlo/
_RESEARCH_TPM = _MONTE_CARLO_DIR.parent       # …/two_phase_margin/
_REPO_ROOT = _RESEARCH_TPM.parents[1]         # repo root

_DEFAULT_RESULTS_DIR = _MONTE_CARLO_DIR / "results"
_DEFAULT_OUT = _MONTE_CARLO_DIR / "MONTE_CARLO_REPORT.md"
_DEFAULT_ANCHOR_CSV = _REPO_ROOT / "research" / "TWOPHASE_MARGIN_aggregate.csv"

# ---------------------------------------------------------------------------
# matplotlib — best-effort, never a hard dependency
# ---------------------------------------------------------------------------
try:
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend; safe in any environment
    import matplotlib.pyplot as plt
    _MATPLOTLIB_OK = True
except Exception:  # noqa: BLE001
    _MATPLOTLIB_OK = False


# ===========================================================================
# 1. load_results
# ===========================================================================

def load_results(paths_or_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Load MC result CSVs and return {generator: DataFrame}.

    Accepts:
      - A single CSV file path (string or Path).
      - A list/tuple of CSV file paths.
      - A directory: scans for mc_*.csv; for each generator keeps the LATEST
        file by name (timestamp suffix in filename: mc_{gen}_{ts}.csv).

    Returns a dict keyed by generator name (e.g. "parametric", "bootstrap").
    If multiple files exist for the same generator in a directory, only the
    most-recently-named file is kept (alphabetically last = latest timestamp).
    """
    paths_or_dir = Path(paths_or_dir) if isinstance(paths_or_dir, str) else paths_or_dir

    csv_paths: list[Path] = []

    if isinstance(paths_or_dir, (list, tuple)):
        # caller passed an explicit list
        csv_paths = [Path(p) for p in paths_or_dir]
    elif paths_or_dir.is_dir():
        # Scan directory: find all mc_*.csv files
        all_csvs = sorted(paths_or_dir.glob("mc_*.csv"))
        if not all_csvs:
            raise FileNotFoundError(
                f"No mc_*.csv files found in directory: {paths_or_dir}"
            )
        # Group by generator (second part of filename after 'mc_')
        # Filename pattern: mc_{generator}_{timestamp}.csv
        by_gen: dict[str, list[Path]] = {}
        for p in all_csvs:
            parts = p.stem.split("_", 2)  # ["mc", generator, timestamp]
            if len(parts) < 2:
                continue
            gen = parts[1]
            by_gen.setdefault(gen, []).append(p)
        # Keep alphabetically-last (= most recent timestamp) per generator
        for gen, files in by_gen.items():
            csv_paths.append(sorted(files)[-1])
    else:
        # Single path
        csv_paths = [paths_or_dir]

    results: dict[str, pd.DataFrame] = {}
    for p in csv_paths:
        df = pd.read_csv(p)
        if "generator" not in df.columns:
            raise ValueError(f"CSV {p} is missing 'generator' column")
        gen = str(df["generator"].iloc[0])
        if gen in results:
            # Merge (shouldn't happen with directory scan, but handle gracefully)
            results[gen] = pd.concat([results[gen], df], ignore_index=True)
        else:
            results[gen] = df

    if not results:
        raise FileNotFoundError(f"No valid MC CSVs loaded from: {paths_or_dir}")

    return results


# ===========================================================================
# 2. distribution_stats
# ===========================================================================

def distribution_stats(df: pd.DataFrame) -> dict:
    """Compute distribution statistics from a per-path result DataFrame.

    Returns a dict with keys:

    Per metric (annual, max_dd, calmar, sharpe):
        {metric}_median, {metric}_p05, {metric}_p25, {metric}_p75, {metric}_p95,
        {metric}_min, {metric}_max

    Risk probabilities:
        p_annual_neg        — P(annual < 0)
        p_maxdd_gt_1pct     — P(max_dd > 0.01)
        p_maxdd_gt_5pct     — P(max_dd > 0.05)
        cvar_annual_5pct    — mean annual of worst-5% paths (CVaR-style)

    Exit-mix averages (per path):
        avg_n_phase1_neg_exits
        avg_n_phase1_cap_exits
        avg_n_phase1_negstop_exits
        avg_n_phase2_exits
        avg_n_liquidations
        avg_n_forced_closes

    Metadata:
        n_paths             — number of rows in df
        horizon_h           — horizon in hours (from first row)
        coins               — coin string (from first row)
        mbuf                — margin buffer (from first row)
        generator           — generator name (from first row)
    """
    stats: dict[str, Any] = {}
    n = len(df)
    stats["n_paths"] = n
    stats["horizon_h"] = int(df["horizon_h"].iloc[0]) if n > 0 else 0
    stats["coins"] = str(df["coins"].iloc[0]) if n > 0 else ""
    stats["mbuf"] = float(df["mbuf"].iloc[0]) if n > 0 else float("nan")
    stats["generator"] = str(df["generator"].iloc[0]) if n > 0 else ""

    # ── Per-metric percentiles ────────────────────────────────────────────────
    for metric in ("annual", "max_dd", "calmar", "sharpe"):
        col = df[metric]
        # calmar can be inf; use finite values for finite percentiles
        finite_col = col.replace([float("inf"), float("-inf")], float("nan")).dropna()

        stats[f"{metric}_median"] = float(col.median())
        stats[f"{metric}_p05"] = float(
            finite_col.quantile(0.05) if len(finite_col) > 0 else float("nan")
        )
        stats[f"{metric}_p25"] = float(
            finite_col.quantile(0.25) if len(finite_col) > 0 else float("nan")
        )
        stats[f"{metric}_p75"] = float(
            finite_col.quantile(0.75) if len(finite_col) > 0 else float("nan")
        )
        stats[f"{metric}_p95"] = float(
            finite_col.quantile(0.95) if len(finite_col) > 0 else float("nan")
        )
        stats[f"{metric}_min"] = float(col.min())
        stats[f"{metric}_max"] = float(col.max())

    # ── Risk probabilities ────────────────────────────────────────────────────
    annual = df["annual"]
    max_dd = df["max_dd"]

    stats["p_annual_neg"] = float((annual < 0).mean()) if n > 0 else float("nan")
    stats["p_maxdd_gt_1pct"] = float((max_dd > 0.01).mean()) if n > 0 else float("nan")
    stats["p_maxdd_gt_5pct"] = float((max_dd > 0.05).mean()) if n > 0 else float("nan")

    # CVaR-style: mean annual among the worst 5% of paths by annual return
    if n > 0:
        threshold_5pct = float(annual.quantile(0.05))
        tail_vals = annual[annual <= threshold_5pct]
        stats["cvar_annual_5pct"] = float(tail_vals.mean()) if len(tail_vals) > 0 else float("nan")
    else:
        stats["cvar_annual_5pct"] = float("nan")

    # ── Exit-mix averages ─────────────────────────────────────────────────────
    for col in (
        "n_phase1_neg_exits",
        "n_phase1_cap_exits",
        "n_phase1_negstop_exits",
        "n_phase2_exits",
        "n_liquidations",
        "n_forced_closes",
    ):
        if col in df.columns:
            stats[f"avg_{col}"] = float(df[col].mean()) if n > 0 else 0.0
        else:
            stats[f"avg_{col}"] = 0.0

    return stats


# ===========================================================================
# 3. write_report
# ===========================================================================

def _fmt_pct(v: float, decimals: int = 4) -> str:
    """Format a fraction as percent string, handling inf/nan."""
    if math.isnan(v):
        return "N/A"
    if math.isinf(v):
        return "∞" if v > 0 else "-∞"
    return f"{v * 100:.{decimals}f}%"


def _fmt_f(v: float, decimals: int = 4) -> str:
    """Format a plain float, handling inf/nan."""
    if math.isnan(v):
        return "N/A"
    if math.isinf(v):
        return "∞" if v > 0 else "-∞"
    return f"{v:.{decimals}f}"


def _load_anchor(anchor_csv: Path) -> dict | None:
    """Load U-prod buf=3 row from TWOPHASE_MARGIN_aggregate.csv."""
    try:
        df = pd.read_csv(anchor_csv)
        row = df[
            (df["universe"] == "U-prod") & (df["margin_buffer_x"] == 3.0)
        ]
        if row.empty:
            return None
        r = row.iloc[0].to_dict()
        return r
    except Exception:  # noqa: BLE001
        return None


def _percentile_table_rows(s: dict, metric: str, label: str, pct_fmt: bool) -> list[str]:
    """Return markdown table rows for one metric."""
    fmt = _fmt_pct if pct_fmt else _fmt_f
    rows = []
    rows.append(
        f"| {label} "
        f"| {fmt(s[f'{metric}_p05'])} "
        f"| {fmt(s[f'{metric}_p25'])} "
        f"| {fmt(s[f'{metric}_median'])} "
        f"| {fmt(s[f'{metric}_p75'])} "
        f"| {fmt(s[f'{metric}_p95'])} "
        f"| {fmt(s[f'{metric}_min'])} "
        f"| {fmt(s[f'{metric}_max'])} |"
    )
    return rows


def _stats_block(s: dict, gen_label: str) -> list[str]:
    """Return markdown lines for one generator's stats block."""
    lines: list[str] = []
    n = s["n_paths"]
    horizon_d = s["horizon_h"] / 24
    lines.append(
        f"**Generator:** `{s['generator']}` — {gen_label}  \n"
        f"**Paths:** {n}  |  "
        f"**Horizon:** {horizon_d:.0f} d ({s['horizon_h']} h)  |  "
        f"**Coins:** `{s['coins']}`  |  "
        f"**mbuf:** {s['mbuf']:.1f}×"
    )
    lines.append("")

    # Note on denominator
    lines.append(
        "> **Denominator note (Rule-5 GAP):** `annual` and `max_dd` below are "
        "computed on **full-portfolio equity** (~$1 000 budget incl. idle cash), "
        "NOT on deployed/occupied capital. "
        "APR on occupied capital is materially higher — see the "
        "_Occupied-capital reframe_ section."
    )
    lines.append("")

    # Percentile table
    hdr = (
        "| Metric | p05 | p25 | median | p75 | p95 | min | max |"
    )
    sep = "|--------|-----|-----|--------|-----|-----|-----|-----|"
    lines.append(hdr)
    lines.append(sep)
    lines += _percentile_table_rows(s, "annual", "annual (CAGR, full-budget)", True)
    lines += _percentile_table_rows(s, "max_dd", "max_dd (fraction, full-budget)", True)
    lines += _percentile_table_rows(s, "calmar", "Calmar", False)
    lines += _percentile_table_rows(s, "sharpe", "Sharpe", False)
    lines.append("")

    # Risk probabilities
    lines.append("**Risk probabilities:**")
    lines.append("")
    lines.append(
        f"- P(annual < 0) = **{s['p_annual_neg']:.1%}** "
        f"({int(round(s['p_annual_neg'] * n))}/{n} paths)"
    )
    lines.append(
        f"- P(max_dd > 1%) = **{s['p_maxdd_gt_1pct']:.1%}** "
        f"({int(round(s['p_maxdd_gt_1pct'] * n))}/{n} paths)"
    )
    lines.append(
        f"- P(max_dd > 5%) = **{s['p_maxdd_gt_5pct']:.1%}** "
        f"({int(round(s['p_maxdd_gt_5pct'] * n))}/{n} paths)"
    )
    lines.append(
        f"- CVaR annual (worst-5% mean) = **{_fmt_pct(s['cvar_annual_5pct'])}** "
        "(full-budget basis)"
    )
    lines.append("")

    # Exit mix
    lines.append("**Exit-mix (averages per path):**")
    lines.append("")
    lines.append(
        f"| Exit type | Avg / path |\n"
        f"|-----------|------------|\n"
        f"| Phase-1 neg exits | {s['avg_n_phase1_neg_exits']:.2f} |\n"
        f"| Phase-1 cap exits | {s['avg_n_phase1_cap_exits']:.2f} |\n"
        f"| Phase-1 **NEGSTOP** exits | **{s['avg_n_phase1_negstop_exits']:.2f}** |\n"
        f"| Phase-2 exits | {s['avg_n_phase2_exits']:.2f} |\n"
        f"| Liquidations | {s['avg_n_liquidations']:.2f} |\n"
        f"| Forced closes | {s['avg_n_forced_closes']:.2f} |"
    )
    lines.append("")

    return lines


def write_report(
    results: dict[str, pd.DataFrame],
    out_path: str | Path,
    anchor: dict | None = None,
    anchor_csv: Path | None = None,
) -> Path:
    """Assemble MONTE_CARLO_REPORT.md and write to out_path.

    Arguments:
        results:    {generator: DataFrame} from load_results().
        out_path:   Destination .md file.
        anchor:     Optional pre-loaded anchor dict (U-prod buf=3 row).
                    If None, attempt to load from anchor_csv or default path.
        anchor_csv: Override path to TWOPHASE_MARGIN_aggregate.csv.

    Returns the resolved out_path.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load anchor ───────────────────────────────────────────────────────────
    if anchor is None:
        csv_p = anchor_csv if anchor_csv is not None else _DEFAULT_ANCHOR_CSV
        anchor = _load_anchor(csv_p)

    # ── Compute stats for each generator ─────────────────────────────────────
    gen_stats: dict[str, dict] = {}
    for gen, df in results.items():
        gen_stats[gen] = distribution_stats(df)

    # ── Metadata from any generator ───────────────────────────────────────────
    any_s = next(iter(gen_stats.values()))

    ts_now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []

    # ─── Title ───────────────────────────────────────────────────────────────
    lines.append("# Monte-Carlo Validation Report: two_phase_margin")
    lines.append("")
    lines.append(f"_Generated: {ts_now}_")
    lines.append("")

    # ─── Summary params ───────────────────────────────────────────────────────
    lines.append("## Run parameters")
    lines.append("")
    lines.append(
        f"| Parameter | Value |\n"
        f"|-----------|-------|\n"
        f"| Generators | {', '.join(f'`{g}`' for g in gen_stats)} |\n"
        f"| Paths per generator | {any_s['n_paths']} |\n"
        f"| Horizon | {any_s['horizon_h'] / 24:.0f} d ({any_s['horizon_h']} h) |\n"
        f"| Coins | `{any_s['coins']}` |\n"
        f"| Margin buffer (mbuf) | {any_s['mbuf']:.1f}× |\n"
        f"| Denominator | **Full-portfolio equity (~$1 000 budget)** "
        f"— NOT occupied capital (see Rule-5 GAP below) |"
    )
    lines.append("")

    # ─── Single-path anchor ───────────────────────────────────────────────────
    lines.append("## Single-path anchor (U-prod, buf=3×)")
    lines.append("")
    if anchor is not None:
        lines.append(
            "Source: `research/TWOPHASE_MARGIN_aggregate.csv`, row `universe=U-prod`, "
            "`margin_buffer_x=3.0`.  "
            "This is the SINGLE historical backtest that motivated the MC study — "
            "the MC distributions below show whether this result is a typical outcome "
            "or a right-tail artifact."
        )
        lines.append("")
        lines.append(
            f"| Metric | Single-path (full-budget basis) |\n"
            f"|--------|--------------------------------|\n"
            f"| annual_pct (linear, %) | **{anchor.get('annual_pct', float('nan')):.4f}%** |\n"
            f"| max_dd_pct (%) | **{anchor.get('max_dd_pct', float('nan')):.4f}%** |\n"
            f"| Sharpe | {anchor.get('sharpe', float('nan')):.4f} |\n"
            f"| n_phase1_negstop_exits | {int(anchor.get('n_phase1_negstop_exits', 0))} |\n"
            f"| n_phase2_exits | {int(anchor.get('n_phase2_exits', 0))} |\n"
            f"| Period | {anchor.get('period_start', '?')} → {anchor.get('period_end', '?')} "
            f"({int(anchor.get('n_hours', 0))} h) |"
        )
        lines.append("")
        lines.append(
            "> **Calmar (single-path):** "
            + (
                f"{anchor['annual_pct'] / anchor['max_dd_pct']:.1f}× "
                "(annual_pct / max_dd_pct — linear ratio, not CAGR/fraction)"
                if anchor.get("max_dd_pct", 0) > 0
                else "∞ (zero drawdown)"
            )
        )
        lines.append("")
    else:
        lines.append(
            "_Anchor CSV not found — single-path comparison unavailable._"
        )
        lines.append("")

    # ─── Distribution tables ─────────────────────────────────────────────────
    lines.append("## Distribution of MC paths (full-budget denominator)")
    lines.append("")
    lines.append(
        "> All metrics computed on full-portfolio equity (budget_cap_usdc ≈ $1 000, "
        "including idle cash). `annual` is CAGR-style; `max_dd` is a fraction (e.g. "
        "0.0050 = 0.50%). `calmar` can be ∞ when max_dd = 0."
    )
    lines.append("")

    gen_labels = {
        "parametric": "parametric (log-level AR(1) funding, GBM+jumps price, "
                      "hot/cold regime switching, cross-coin corr — includes hot & cold regimes)",
        "bootstrap": "bootstrap (synchronous circular block-resample of real history — "
                     "cold-only window by construction; see T4 caveat)",
    }

    if len(gen_stats) == 2 and "parametric" in gen_stats and "bootstrap" in gen_stats:
        # ── Side-by-side comparison block ────────────────────────────────────
        lines.append(
            "Both generators available — **side-by-side comparison** follows.  "
            "Divergence between parametric and bootstrap indicates model sensitivity "
            "(parametric can produce hot-regime paths; bootstrap is cold-history reshuffle)."
        )
        lines.append("")

        for gen in ("parametric", "bootstrap"):
            s = gen_stats[gen]
            label = gen_labels.get(gen, gen)
            lines.append(f"### {gen.capitalize()}")
            lines.append("")
            lines += _stats_block(s, label)

        # Quick side-by-side key-metrics summary
        lines.append("### Quick comparison: median metrics")
        lines.append("")
        p_stats = gen_stats["parametric"]
        b_stats = gen_stats["bootstrap"]
        lines.append(
            "| Metric | parametric | bootstrap | note |\n"
            "|--------|-----------|-----------|------|\n"
            f"| annual median | {_fmt_pct(p_stats['annual_median'])} "
            f"| {_fmt_pct(b_stats['annual_median'])} | full-budget |\n"
            f"| max_dd median | {_fmt_pct(p_stats['max_dd_median'])} "
            f"| {_fmt_pct(b_stats['max_dd_median'])} | full-budget |\n"
            f"| Calmar median | {_fmt_f(p_stats['calmar_median'])} "
            f"| {_fmt_f(b_stats['calmar_median'])} | |\n"
            f"| Sharpe median | {_fmt_f(p_stats['sharpe_median'])} "
            f"| {_fmt_f(b_stats['sharpe_median'])} | |\n"
            f"| P(annual < 0) | {p_stats['p_annual_neg']:.1%} "
            f"| {b_stats['p_annual_neg']:.1%} | |\n"
            f"| CVaR annual worst-5% | {_fmt_pct(p_stats['cvar_annual_5pct'])} "
            f"| {_fmt_pct(b_stats['cvar_annual_5pct'])} | full-budget |"
        )
        lines.append("")

        if anchor is not None:
            # Contrast single-path vs MC medians
            lines.append("### Single-path vs MC median contrast")
            lines.append("")
            sp_annual = anchor.get("annual_pct", float("nan")) / 100
            sp_mdd = anchor.get("max_dd_pct", float("nan")) / 100
            lines.append(
                "| Metric | Single-path | Para median | Boot median |\n"
                "|--------|-------------|-------------|-------------|\n"
                f"| annual (CAGR approx) | {_fmt_pct(sp_annual)} "
                f"| {_fmt_pct(p_stats['annual_median'])} "
                f"| {_fmt_pct(b_stats['annual_median'])} |\n"
                f"| max_dd | {_fmt_pct(sp_mdd)} "
                f"| {_fmt_pct(p_stats['max_dd_median'])} "
                f"| {_fmt_pct(b_stats['max_dd_median'])} |"
            )
            lines.append("")
            lines.append(
                "> Interpretation: if single-path annual >> MC median, the "
                "historical backtest was a favorable-path draw. "
                "If single-path Calmar >> MC Calmar distribution, it may be a "
                "right-tail artifact. Full verdict deferred to T7 (Opus)."
            )
            lines.append("")

    else:
        # Single or other generators
        for gen, s in gen_stats.items():
            label = gen_labels.get(gen, gen)
            lines.append(f"### {gen.capitalize()}")
            lines.append("")
            lines += _stats_block(s, label)

        if anchor is not None and gen_stats:
            s = next(iter(gen_stats.values()))
            sp_annual = anchor.get("annual_pct", float("nan")) / 100
            sp_mdd = anchor.get("max_dd_pct", float("nan")) / 100
            lines.append("### Single-path vs MC median contrast")
            lines.append("")
            lines.append(
                "| Metric | Single-path | MC median |\n"
                "|--------|-----------|-----------|\n"
                f"| annual | {_fmt_pct(sp_annual)} | {_fmt_pct(s['annual_median'])} |\n"
                f"| max_dd | {_fmt_pct(sp_mdd)} | {_fmt_pct(s['max_dd_median'])} |"
            )
            lines.append("")

    # ─── Occupied-capital reframe ─────────────────────────────────────────────
    lines.append("## Occupied-capital reframe (для T7)")
    lines.append("")
    lines.append(
        "All metrics in this report use the **full-portfolio equity denominator** "
        "(budget_cap_usdc ≈ $1 000, including idle/undeployed USDC).  "
        "This understates APR on a per-deployed-capital basis."
    )
    lines.append("")
    lines.append(
        "The relationship is:  \n"
        "```\n"
        "APR_occupied = APR_full_budget × (budget / avg_deployed)\n"
        "```\n"
        "With 7 coins, position_size=\\$100, concurrency_cap=K and typical "
        "occupancy, avg_deployed ≈ \\$300–\\$400 out of \\$1 000 budget, "
        "so the occupied-capital APR is roughly **2.5–3.3× larger** than the "
        "full-budget figures shown in the tables."
    )
    lines.append("")
    lines.append(
        "**Exact multiplier:** requires average deployed notional tracked per "
        "hour across all MC paths.  This is NOT stored in the current result "
        "columns (T5 output).  The `total_funding` / `total_fees` / `final_equity` "
        "columns can be used to reconstruct funding-based APR but not the "
        "occupancy fraction directly.  "
        "**→ Deferred to T7 (Opus) for accurate reconstruction.**  "
        "Do NOT apply an invented multiplier to the tables above."
    )
    lines.append("")

    # ─── Plots ────────────────────────────────────────────────────────────────
    lines.append("## Plots")
    lines.append("")
    plot_paths: list[Path] = []

    if _MATPLOTLIB_OK and results:
        try:
            plot_dir = out_path.parent
            plot_paths = _make_plots(results, plot_dir)
            for pp in plot_paths:
                lines.append(f"![{pp.stem}]({pp.name})")
            lines.append("")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"_Plot generation failed: {exc}_")
            lines.append("")
    else:
        reason = "matplotlib недоступен" if not _MATPLOTLIB_OK else "нет данных"
        lines.append(f"_Графики пропущены ({reason})._")
        lines.append("")

    # ─── Footer ───────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append(
        "_This report was generated by `research/two_phase_margin/monte_carlo/report.py` "
        "(T6). Verdict and interpretation deferred to T7 (Opus)._"
    )

    # ── Write file ────────────────────────────────────────────────────────────
    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")

    return out_path


# ===========================================================================
# 4. _make_plots (matplotlib best-effort)
# ===========================================================================

def _make_plots(results: dict[str, pd.DataFrame], plot_dir: Path) -> list[Path]:
    """Generate and save histogram PNGs. Returns list of saved paths."""
    if not _MATPLOTLIB_OK:
        return []

    saved: list[Path] = []
    metrics_cfg = [
        ("annual", "Annual return (CAGR, full-budget)", True),
        ("max_dd", "Max drawdown (fraction, full-budget)", True),
        ("calmar", "Calmar ratio", False),
    ]
    colors = {"parametric": "#2196F3", "bootstrap": "#FF9800"}

    for metric, title, as_pct in metrics_cfg:
        fig, ax = plt.subplots(figsize=(8, 4))
        any_data = False
        for gen, df in results.items():
            col = df[metric].replace([float("inf"), float("-inf")], float("nan")).dropna()
            if len(col) == 0:
                continue
            vals = col * 100 if as_pct else col
            ax.hist(
                vals,
                bins=min(30, max(5, len(vals) // 2)),
                alpha=0.6,
                label=gen,
                color=colors.get(gen, None),
            )
            any_data = True
        if not any_data:
            plt.close(fig)
            continue

        xlabel = f"{metric} (%)" if as_pct else metric
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Paths")
        ax.set_title(f"MC distribution: {title}")
        ax.legend()
        fig.tight_layout()

        fname = plot_dir / f"mc_hist_{metric}.png"
        fig.savefig(fname, dpi=100)
        plt.close(fig)
        saved.append(fname)

    return saved


# ===========================================================================
# 5. main / CLI
# ===========================================================================

def main() -> None:
    """CLI entry-point: load results, write report."""
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate MC results and write MONTE_CARLO_REPORT.md.\n\n"
            "Reads mc_*.csv files from --results-dir (or a specific file),\n"
            "computes distribution statistics, and writes a markdown report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        default=str(_DEFAULT_RESULTS_DIR),
        help=(
            f"Directory containing mc_*.csv files, or a single CSV path "
            f"(default: {_DEFAULT_RESULTS_DIR})"
        ),
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(_DEFAULT_OUT),
        help=f"Output path for the markdown report (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--anchor-csv",
        type=str,
        default=str(_DEFAULT_ANCHOR_CSV),
        help=(
            f"Path to TWOPHASE_MARGIN_aggregate.csv for single-path anchor "
            f"(default: {_DEFAULT_ANCHOR_CSV})"
        ),
    )
    args = parser.parse_args()

    results_path = Path(args.results_dir)
    out_path = Path(args.out)
    anchor_csv = Path(args.anchor_csv)

    print(f"[report] Loading results from: {results_path}", file=sys.stderr)
    results = load_results(results_path)
    for gen, df in results.items():
        print(f"[report]   {gen}: {len(df)} paths", file=sys.stderr)

    print(f"[report] Writing report to: {out_path}", file=sys.stderr)
    written = write_report(results, out_path, anchor_csv=anchor_csv)
    print(f"[report] Done: {written}", file=sys.stderr)


if __name__ == "__main__":
    main()
