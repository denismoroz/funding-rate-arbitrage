"""
run_mc.py — Monte-Carlo orchestrator CLI.

Реализуется в T5.

Usage:
    uv run python research/two_phase_margin/monte_carlo/run_mc.py \\
        --n 500 \\
        --horizon-days 365 \\
        --seed 42 \\
        --generator parametric \\
        --coins SOL ETH BTC \\
        --mbuf 3.0 \\
        --params prod

Output:
    results/mc_{generator}_{timestamp}.parquet  (+ .csv mirror)
    Each row: seed_i, annual, max_dd, calmar, sharpe (per-path metrics).

Acceptance (T5):
  - Determinism: same --seed => identical rows.
  - --n 50 completes without errors on the prod coin set.
  - Output parquet has exactly N rows.
"""

from __future__ import annotations


def run(
    n: int,
    horizon_days: int,
    seed: int,
    generator: str,
    coins: list[str],
    mbuf: float,
    params_mode: str,
) -> None:
    """Run N Monte-Carlo iterations and write results to results/.

    Реализуется в T5.
    """
    raise NotImplementedError("run_mc.run реализуется в T5")


if __name__ == "__main__":
    raise NotImplementedError("run_mc CLI entry-point реализуется в T5")
