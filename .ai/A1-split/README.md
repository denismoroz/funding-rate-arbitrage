# A1 split into micro-tasks for local-LLM execution

Splits the original A1 (margin model in `research/engine.py`) into 9 sequential micro-tasks, each conforming to local-LLM constraints (≤100 line diff, 1 file, ≤80-line spec, single change point).

Each task is run **inline** through `opencode run "..."` (no `--file`), one at a time, with a `git diff` review and a verify command between tasks. All produced via Qwen 3.6 35B local.

Order:
1. `task-1.1` — add `DEFAULT_MAINT_RATIO` module constant
2. `task-1.2` — extend `simulate()` signature with 6 margin kwargs + early validation; no behaviour change yet
3. `task-1.3` — allocate `spot_cash`/`perp_cash`/counters at the top of `simulate()` under `if margin_active:`; populate margin keys in final `info` dict; no use in main loop
4. `task-1.4` — margin reservation at OPEN points
5. `task-1.5` — hourly funding into `perp_cash` + maintenance/liquidation check
6. `task-1.6` — top-up branch (margin_ratio < trigger, sufficient spot_cash)
7. `task-1.7` — forced-close branch (margin_ratio < trigger, insufficient spot_cash)
8. `task-1.8` — normal-exit branch: release margin to spot_cash
9. `task-1.9` — equity formula branch + final close + verify script

Each task ends with a fast verification: existing backtest (`uv run python research/backtest_a.py`) MUST produce identical output (because `per_coin_leverage=None` by default → branches not entered).
