"""
report.py — Aggregation and human-readable report generation.

Реализуется в T6.

Input:  results/mc_*.parquet produced by run_mc.py (T5).
Output: MONTE_CARLO_REPORT.md with:
  - Percentile table (5/25/50/75/95 + min/max) for annual, max_dd, calmar, sharpe.
  - P(annual < 0), P(max_dd > X), CVaR-style worst-5% tail.
  - Side-by-side single-path anchor from TWOPHASE_MARGIN_aggregate.csv (U-prod, current).
  - Optional: matplotlib histograms (APR/max_dd/Calmar) + fan-chart equity.

Acceptance (T6):
  - Report renders on >= 500 paths from both generators.
  - Percentile table and P(neg year) are present.
"""

from __future__ import annotations

from pathlib import Path


def aggregate(results_path: Path) -> dict:
    """Load MC results parquet and compute distribution statistics.

    Returns dict with percentile tables and tail risk measures.

    Реализуется в T6.
    """
    raise NotImplementedError("report.aggregate реализуется в T6")


def render_report(
    stats: dict,
    output_path: Path,
    anchor_csv: Path | None = None,
    plot: bool = True,
) -> None:
    """Write MONTE_CARLO_REPORT.md from aggregated statistics.

    Arguments:
        stats:       output of aggregate().
        output_path: path to write the markdown report.
        anchor_csv:  optional path to TWOPHASE_MARGIN_aggregate.csv for
                     single-path anchor comparison.
        plot:        if True, generate matplotlib figures (requires matplotlib).

    Реализуется в T6.
    """
    raise NotImplementedError("report.render_report реализуется в T6")
