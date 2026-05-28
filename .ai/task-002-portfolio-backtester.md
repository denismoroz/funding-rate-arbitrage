# Task 2: Portfolio-level margin-aware backtester

## Goal
Create a new script `research/portfolio_margin.py` that simulates the funding-harvest strategy across N coins with **shared cross-margin perp wallet** (single pool used by all open shorts), a `budget_cap_usd`, K-slot concurrency, and the margin policy from Task 1. Outputs combined portfolio metrics and per-coin breakdown to CSV.

This task depends on Task 1 being completed (margin model in `simulate()` must be available, OR the equivalent logic implemented in the portfolio script directly). If Task 1 is not yet merged, this task should be implemented with self-contained per-coin logic inside the portfolio simulator.

## Files likely involved
- `research/portfolio_margin.py` — NEW script (main deliverable)
- `research/portfolio_margin_results.csv` — produced by running the script
- Reference: `research/concurrency_cap.py` — existing portfolio-level pattern to mirror style of

## Scope
- Create ONE new file: `research/portfolio_margin.py`.
- Output a CSV with results.
- Re-use existing `load_data()` from `research/engine.py`.

## Out of scope
- DO NOT modify `research/engine.py` (assume it has Task 1's margin model, but the portfolio simulator does its own bookkeeping at portfolio level).
- DO NOT modify any other research script.
- DO NOT add dependencies.

## Constraints
- Python 3.13, numpy, pandas. Plain script with `if __name__ == "__main__":`.
- All coins simulated synchronously on a common hourly timeline (intersection of available data).
- Single cross-margin perp wallet (sum of all maintenance margins across open positions).
- Hard cap: `budget_cap_usd` — total of (sum spot legs + perp wallet) MUST NOT exceed this at any time.
- K-slot concurrency: at most `concurrency_cap` coins in position simultaneously. When > K candidates have positive signal, pick top-K by signal strength.
- Per-coin `leverage` and `maint_ratio` from the config dict at top of file.
- Uniform `position_size_usd` across coins.
- No emojis. Russian comments OK (match existing research/ style).

## Implementation steps

1. At the top of the file, define config dicts:
   ```python
   PER_COIN_LEVERAGE = {
       "BTC": 20, "ETH": 20, "SOL": 10, "AVAX": 10,
       "LINK": 10, "AAVE": 5, "DOGE": 5,
   }
   PER_COIN_MAINT_RATIO = {
       "BTC": 0.01, "ETH": 0.01, "SOL": 0.025, "AVAX": 0.025,
       "LINK": 0.025, "AAVE": 0.05, "DOGE": 0.05,
   }
   COINS = list(PER_COIN_LEVERAGE.keys())
   POSITION_SIZE = 100.0
   MARGIN_BUFFER_X = 3.0
   TOP_UP_TRIGGER = 2.0
   HEALTHY_RATIO = 3.0
   CONCURRENCY_CAP = 3
   BUDGET_CAP_USD = 1000.0

   ENTRY_THRESHOLD = 0.30   # 30% annualized funding (12h MA)
   EXIT_THRESHOLD  = -0.15
   MIN_HOLD_HOURS  = 120
   SIGNAL_WINDOW_HOURS = 12

   PERP_TAKER = 0.00035
   SPOT_TAKER = 0.00070
   HOURS_PER_YEAR = 8760
   ```

2. Use `from engine import load_data, smooth_funding` (relative — script lives in `research/`).

3. Load all coin data, build a per-coin DataFrame with columns `close`, `fundingRate`. Build a master timeline = intersection of all coins' hourly timestamps (inner join). All bookkeeping iterates this master timeline.

4. State (initialized once):
   - `spot_cash` = `BUDGET_CAP_USD` initially.
   - `perp_cash` = 0.0
   - `positions: dict[coin, dict]` — per-coin: `{"open": bool, "units_spot": float, "short_size": float, "entry_price": float, "hours_in": int, "required_margin": float}`. Closed positions have `open=False` and zeros.
   - Counters: `n_liquidations`, `n_top_ups`, `n_forced_closes`, `n_skipped_opens_capital`, `min_margin_ratio = inf`.
   - History arrays: `equity_arr`, `committed_arr`, `margin_ratio_arr`, `n_open_arr`, all length = len(timeline).

5. Per-coin signal: 12-hour MA of fundingRate × 8760 (annualized).

6. Hourly loop (over master timeline `t in timeline`):
   a. Funding accrual: for every open position, `f = short_size * close[t] * fundingRate[t]; perp_cash += f`.
   b. Compute aggregate state:
      - `total_maintenance = Σ short_size_i * close_i[t] * maint_ratio[coin_i]` over open positions
      - `unrealized = Σ short_size_i * (entry_price_i - close_i[t])` over open positions
      - `perp_equity = perp_cash + unrealized`
      - `margin_ratio = perp_equity / total_maintenance` if `total_maintenance > 0` else `inf`
      - Track `min_margin_ratio`.
   c. **Liquidation cascade**: if `margin_ratio <= 1.0` and there's at least one open position:
      - Force close ALL open positions at current prices. Realized PnL on each goes negative; sum loss = `-perp_cash`. Set `perp_cash = 0`, all positions closed without releasing margin. Increment `n_liquidations` per closed position (or by 1 — pick one and document).
      - Spot legs: unwound at current prices, return to `spot_cash` minus fee.
   d. **Top-up if margin_ratio < TOP_UP_TRIGGER and > 1.0**:
      - `target = HEALTHY_RATIO * total_maintenance`
      - `top_up = target - perp_equity`
      - If `spot_cash >= top_up`: `spot_cash -= top_up; perp_cash += top_up; n_top_ups += 1`.
      - Else: forced close of the WEAKEST position (lowest signal strength among open). Release its margin back, unwind spot leg, increment `n_forced_closes`. Recompute aggregate state.
   e. Generate per-coin signals (12h MA × 8760). Mark eligible candidates: `signal > ENTRY_THRESHOLD and not positions[coin]["open"]`.
   f. Generate exit candidates: open positions with `hours_in >= MIN_HOLD_HOURS and signal < EXIT_THRESHOLD`.
   g. Process exits first: for each exit, close normally — realized = `short_size * (entry_price - P)`, `perp_cash += realized - short_size * P * PERP_TAKER`, release `required_margin` back to spot, unwind spot leg with fee. Mark position closed.
   h. Process entries (after exits, so released margin is available):
      - Sort eligible candidates by signal strength descending.
      - For each candidate (in priority order), while `n_open < CONCURRENCY_CAP`:
        - `required_margin = POSITION_SIZE / lev * MARGIN_BUFFER_X`
        - `cost = POSITION_SIZE + required_margin + (POSITION_SIZE * SPOT_TAKER) + (POSITION_SIZE * PERP_TAKER)`
        - Budget check: `committed_after_open <= BUDGET_CAP_USD`? where `committed = (sum spot leg notionals) + perp_cash + cost`. If exceeds → skip, `n_skipped_opens_capital += 1`.
        - Cash check: `spot_cash >= cost`? If no → skip.
        - Else: open. `spot_cash -= POSITION_SIZE + POSITION_SIZE * SPOT_TAKER; perp_cash += required_margin - POSITION_SIZE * PERP_TAKER; spot_cash -= required_margin`. Update position state.
      - Update `n_open_arr[t] = number of open positions`.
   i. Record `equity_arr[t] = spot_cash + perp_cash + Σ units_spot_i * close_i[t] + Σ short_size_i * (entry_price_i - close_i[t])`.
   j. Record `committed_arr[t] = (sum spot leg notionals at entry) + perp_cash`.
   k. Record `margin_ratio_arr[t] = margin_ratio`.

7. Final close: at end of timeline, close everything at last prices. Add to spot_cash.

8. Compute portfolio metrics on `pnl_arr = np.diff(equity_arr, prepend=BUDGET_CAP_USD)`:
   - `total_return = equity_arr[-1] / BUDGET_CAP_USD - 1`
   - `annual_pct = total_return * HOURS_PER_YEAR / len(timeline) * 100`
   - `vol_pct = hourly_returns.std() * sqrt(HOURS_PER_YEAR) * 100`
   - `sharpe`, `sortino` analogous to `engine.compute_metrics`
   - `max_dd_pct` standard drawdown
   - `calmar = annual_pct / max_dd_pct`

9. Write CSV `research/portfolio_margin_results.csv` with one row of these portfolio-level metrics + counters: `annual_pct, vol_pct, sharpe, sortino, max_dd_pct, calmar, n_liquidations, n_top_ups, n_forced_closes, n_skipped_opens_capital, min_margin_ratio, peak_committed, final_equity, total_funding, total_fees`.

10. Print a human-readable summary to stdout.

## Acceptance criteria

1. The script runs without errors: `uv run python research/portfolio_margin.py`.
2. CSV is produced at `research/portfolio_margin_results.csv` with all listed columns.
3. Numerical sanity checks (printed to stdout):
   - `final_equity > 0` (i.e., we didn't lose everything).
   - `annual_pct` is finite.
   - `n_liquidations == 0` for the default config (BUFFER 3×, HEALTHY 3×). If liquidations DO occur with default config, the script should still complete and report them.
   - `committed_arr.max() <= BUDGET_CAP_USD * 1.01` (allow 1% float slack).
4. Computation finishes within 60 seconds on a 2-year window.

## Tests or validation to run

```bash
cd /Users/d/prj/funding-rate-arbitrage

uv run python research/portfolio_margin.py

# Verify CSV exists and has expected columns:
head -1 research/portfolio_margin_results.csv
```

## Risks and edge cases

- **Cross-margin maintenance** is summed across all positions — when one position is in trouble, the others' equity (still positive) covers it via the shared wallet. Make sure you compute `perp_equity` as `perp_cash + Σ unrealized` (sum, not per-position).
- **Order of operations matters per hour**: funding → liquidation check → top-up → exits → entries. Don't mix.
- **Weakest position selection**: use lowest annualized signal value at the moment of the check.
- **Position metadata**: `required_margin` is stored at open and released on close — even if price moved, you return the SAME amount to spot_cash (margin is collateral, not P&L). P&L is realized via the `entry_price - P` term.
- **Liquidation modeling**: at margin_ratio <= 1.0 we model "lose all perp_cash". Real HL liquidation engine is more complex (sequential ADL etc.), but this is good enough for backtest.
- **Floating-point**: prefer comparisons with small epsilon for thresholds. `<= 1.0` is fine.
- **No look-ahead**: signal uses 12h MA up to and including time t. `MA[t]` includes `funding[t]`. OK to keep that, but document.
- If a coin has missing data at time t (after inner-join master timeline this shouldn't happen — but defensively skip the coin for that hour).

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
  --file .ai/task-002-portfolio-backtester.md \
  "Implement the attached task exactly. Keep the diff minimal. Do not change unrelated files. Do not redesign the solution."
```
