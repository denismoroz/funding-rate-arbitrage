"""
generators/ — Synthetic dfs generators for Monte-Carlo runs.

  parametric.py  — GBM + jump-diffusion price, AR(1)/OU funding with regime switching (T3)
  bootstrap.py   — Stationary block bootstrap from real history (T4)

Both generators produce dict[coin → DataFrame[close, fundingRate]] with an
hourly DatetimeIndex — the exact format consumed by engine_adapter.run_on_dfs().
"""
