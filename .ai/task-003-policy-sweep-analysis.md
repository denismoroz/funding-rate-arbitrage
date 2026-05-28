# Task 3: Policy sweep + analysis writeup

## Goal
Add a parameter-sweep wrapper around the portfolio simulator from Task 2 that runs multiple `(leverage, margin_buffer_x, position_size)` combinations and produces a comparison table. Also write up findings in a short markdown so a human can decide whether to proceed to the live-engine phase.

This task depends on Task 2 producing a working `research/portfolio_margin.py` with a callable `simulate_portfolio(...)` function (refactored slightly from the script's `__main__` for re-use).

## Files likely involved
- `research/portfolio_margin.py` — minor refactor: extract `simulate_portfolio(...)` callable.
- `research/portfolio_margin_sweep.py` — NEW sweep runner.
- `research/portfolio_margin_sweep_results.csv` — produced by sweep.
- `research/MARGIN_ANALYSIS.md` — NEW markdown writeup (short, ~300 words).

## Scope
- Allow modifying `research/portfolio_margin.py` ONLY to extract a function for re-use (no logic change).
- Create `research/portfolio_margin_sweep.py`.
- Create `research/MARGIN_ANALYSIS.md`.

## Out of scope
- DO NOT change the simulation logic in `portfolio_margin.py`.
- DO NOT modify `engine.py`.
- DO NOT modify any other research script.

## Constraints
- Same stack as Task 2: numpy, pandas, plain script.
- Sweep must complete within 5 minutes for the configurations below.
- Markdown writeup MUST cite numbers from the CSV — no invented numbers.

## Implementation steps

1. **Refactor Task 2 script** to expose a function:
   ```python
   def simulate_portfolio(
       *,
       per_coin_leverage: dict,
       per_coin_maint_ratio: dict,
       position_size: float,
       margin_buffer_x: float,
       top_up_trigger: float,
       healthy_ratio: float,
       concurrency_cap: int,
       budget_cap_usd: float,
       entry_threshold: float = 0.30,
       exit_threshold: float = -0.15,
       min_hold_hours: int = 120,
       signal_window_hours: int = 12,
   ) -> dict:
       """Returns metrics dict: annual_pct, vol_pct, sharpe, sortino, max_dd_pct, calmar,
       n_liquidations, n_top_ups, n_forced_closes, n_skipped_opens_capital,
       min_margin_ratio, peak_committed, final_equity, total_funding, total_fees."""
   ```
   Move the existing script logic INTO this function. The existing `if __name__ == "__main__":` calls it with the defaults and writes the same single-row CSV as before. Behavior unchanged.

2. **Create the sweep script** `research/portfolio_margin_sweep.py`:
   - Imports `simulate_portfolio` from `portfolio_margin`.
   - Defines a baseline `PER_COIN_LEVERAGE` and `PER_COIN_MAINT_RATIO` (same as Task 2 defaults).
   - Sweep grid:
     - `margin_buffer_x ∈ [2.0, 3.0, 5.0]`
     - `position_size ∈ [50.0, 100.0, 150.0]`
     - `concurrency_cap ∈ [3, 5]`
     - Single leverage profile (per-coin from defaults). NO leverage sweep — per-coin caps reflect HL physical limits.
   - For each combination, call `simulate_portfolio(...)`, collect metrics into a list of dicts.
   - Write all results to `research/portfolio_margin_sweep_results.csv` with columns: `margin_buffer_x, position_size, concurrency_cap, annual_pct, vol_pct, sharpe, sortino, max_dd_pct, calmar, n_liquidations, n_top_ups, n_forced_closes, n_skipped_opens_capital, min_margin_ratio, peak_committed, final_equity, total_funding, total_fees`.
   - Print to stdout: top 3 configs by Calmar; top 3 by Sharpe; configs with any liquidations.

3. **Write `research/MARGIN_ANALYSIS.md`** — short report (sections):
   - **Setup**: which coins, period, baseline params, what was swept.
   - **Headline numbers** (cite from sweep CSV): best Calmar config + its annual_pct + max_dd + n_top_ups; baseline (3×, 100, K=3) — its numbers.
   - **vs. sUSDe baseline**: assume `sUSDe ≈ 12% APR pasive`. Compute "premium over sUSDe" for the best config and for the baseline.
   - **Risk events count**: across the sweep, how many configs had ≥1 liquidation? How does Calmar correlate with `margin_buffer_x`?
   - **Recommendation**: one short paragraph — what `(buffer, position_size, K)` to use for live, and what conditions would make us reconsider.
   - **What this DOES NOT model** (one bullet list): wallet-transfer latency, HL spot/perp wallet split friction, real maintenance-margin liquidation engine quirks, slippage on forced-close.

## Acceptance criteria

1. `uv run python research/portfolio_margin.py` still works and produces `portfolio_margin_results.csv` (Task 2 unchanged behaviorally).
2. `uv run python research/portfolio_margin_sweep.py` produces `portfolio_margin_sweep_results.csv` with 18 rows (3 × 3 × 2) + header.
3. `research/MARGIN_ANALYSIS.md` exists, contains all sections above, cites at least 4 numbers from the CSV.
4. Sweep completes within 5 minutes.

## Tests or validation to run

```bash
cd /Users/d/prj/funding-rate-arbitrage

# Re-verify Task 2 still works after refactor:
uv run python research/portfolio_margin.py

# Run sweep:
uv run python research/portfolio_margin_sweep.py

# Verify outputs:
wc -l research/portfolio_margin_sweep_results.csv     # expect 19 (1 header + 18 rows)
head -1 research/portfolio_margin_sweep_results.csv

# Verify the markdown:
head -40 research/MARGIN_ANALYSIS.md
```

## Risks and edge cases

- **Refactor preserves behavior**: the function must be a direct extract of the inline logic. Don't "improve" while moving.
- **CSV row order**: keep deterministic (e.g., nested loops in fixed order so reruns produce identical CSV).
- **`min_margin_ratio` of `nan`**: handle in CSV serialization (pandas default is fine; just verify).
- **Markdown writeup must not invent numbers**: every cited APR/Calmar must trace back to a sweep row.
- **No comparison with the "naive backtest"** in the markdown unless you actually compute it — if you do, that's an additional row from running with `margin_buffer_x` very large (effectively reserving full notional). OK to skip.

## Prompt for the coding agent

```text
Implement this task exactly.
Keep the diff minimal.
Do not change unrelated files.
Do not redesign the solution.
Follow all constraints and acceptance criteria.
If you are unsure, stop and explain the uncertainty instead of guessing.

Task:
[paste this task file content here, or feed via --file]
```

## Suggested OpenCode command

```bash
opencode run \
  --model ollama/qwen3.6:35b-a3b-coding-nvfp4 \
  --agent build \
  --file .ai/task-003-policy-sweep-analysis.md \
  "Implement the attached task exactly. Keep the diff minimal. Do not change unrelated files. Do not redesign the solution."
```
