"""
Monte-Carlo Validation Harness for two_phase_margin strategy.

Package structure:
  metrics.py          — pure equity-curve metric functions (T0)
  engine_adapter.py   — thin wrapper around simulate() (T1)
  calibrate_stats.py  — extract stylized facts from real history (T2)
  generators/
    parametric.py     — parametric synthetic dfs generator (T3)
    bootstrap.py      — block bootstrap synthetic dfs generator (T4)
  run_mc.py           — MC orchestrator CLI (T5)
  report.py           — aggregation + report generation (T6)
  calibration/        — per-coin calibration JSON outputs
  results/            — per-run parquet/csv outputs
  tests/              — pytest suite
"""
