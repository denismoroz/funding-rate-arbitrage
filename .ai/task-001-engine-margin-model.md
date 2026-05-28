# Task 1: Per-coin margin model in research/engine.py

## Goal
Extend `simulate()` in `research/engine.py` to support an opt-in per-coin margin model with separate `spot_cash` / `perp_cash` virtual wallets, top-up between them, and liquidation handling. When margin params are not provided (defaults), behavior must be **byte-identical** to current code so all existing backtest scripts continue producing the same numbers.

## Files likely involved
- `research/engine.py` — modify `simulate()`, add module constant `DEFAULT_MAINT_RATIO`
- `research/verify_engine_margin.py` — new verification script (NOT pytest, plain script with assert + print, matches existing `research/` style)

## Scope
- Modify `research/engine.py` only (the file containing `simulate()`).
- Create `research/verify_engine_margin.py` as a new file.
- Add new keyword-only parameters to `simulate()` (all have defaults, so positional callers unchanged).
- Add module constant `DEFAULT_MAINT_RATIO`.
- Add new optional keys to the returned `info` dict only when the margin model is active.

## Out of scope
- Do NOT modify any file outside `research/engine.py` and the new `research/verify_engine_margin.py`.
- Do NOT change the signature for existing positional arguments of `simulate()`.
- Do NOT modify any existing backtest scripts (`backtest_a.py`, `concurrency_cap.py`, `two_phase_dynamic.py`, etc.). They must keep working unchanged.
- Do NOT rename or restructure existing functions.
- Do NOT introduce new dependencies.

## Constraints
- Stack: numpy + pandas (no async, no logging framework, no pytest framework). Use plain `print()` for the verify script.
- Backwards compatibility is the most important constraint: if `per_coin_leverage` is `None` (default), `pnl_arr` and `info` must be byte-identical to the previous version for ALL strategies (`A_cycle`, `A_spot_keep`, `B`, `B_hedge`).
- Keep style consistent with existing `engine.py`: snake_case, no type hints on internal locals, comments mostly in Russian where the existing file has them.
- No emojis in code.

## Implementation steps

1. Add a module constant near top of `research/engine.py`:
   ```
   DEFAULT_MAINT_RATIO = {
       "BTC": 0.01, "ETH": 0.01,
       "SOL": 0.025, "AVAX": 0.025, "LINK": 0.025,
       "AAVE": 0.05, "DOGE": 0.05,
   }
   ```

2. Extend `simulate()` signature with the following keyword-only parameters (all with defaults; place after existing parameters, BEFORE `track_capital` is fine, or after — pick what reads cleanest):
   - `coin: str | None = None`
   - `per_coin_leverage: dict | None = None`
   - `per_coin_maint_ratio: dict | None = None`
   - `margin_buffer_x: float = 3.0`
   - `top_up_trigger: float = 2.0`
   - `healthy_ratio: float = 3.0`

3. At the top of `simulate()`, derive `margin_active = per_coin_leverage is not None`.
   - If active: require `coin is not None` (raise `ValueError` otherwise); look up `lev = per_coin_leverage[coin]` and `mr = (per_coin_maint_ratio or DEFAULT_MAINT_RATIO)[coin]`.
   - If `coin` not in the leverage dict → raise `ValueError`.

4. Add two new state variables for the margin model:
   - `spot_cash` (initialized to current `cash` value)
   - `perp_cash = 0.0`
   - Keep the existing `cash` variable for backwards-compat path; do not delete it.

5. **At open (perp short)** — at every place a perp leg opens (currently `cash -= POSITION_SIZE * PERP_TAKER`):
   - If NOT `margin_active`: existing logic unchanged.
   - If `margin_active`:
     - `required_margin = POSITION_SIZE / lev * margin_buffer_x`
     - If `spot_cash < required_margin + POSITION_SIZE * PERP_TAKER` → SKIP this open. Set a flag so the `in_position = True / trades += 1` block is skipped. Increment `n_skipped_opens_capital`.
     - Else: `spot_cash -= required_margin; perp_cash += required_margin; perp_cash -= POSITION_SIZE * PERP_TAKER`
   - Spot leg buy in `A_cycle` uses `spot_cash` instead of `cash` when active.
   - For `A_spot_keep`/`B`/`B_hedge` the initial spot buy at start of `simulate()` also uses `spot_cash` when active.

6. **Hourly while in position** (existing block where funding is added):
   - If NOT `margin_active`: existing `cash += f` unchanged.
   - If `margin_active`:
     - `perp_cash += f` (instead of `cash`)
     - Compute `notional = short_size * P`, `maintenance = notional * mr`, `unrealized = short_size * (entry_price - P)`, `perp_equity = perp_cash + unrealized`.
     - `margin_ratio = perp_equity / maintenance` if `maintenance > 0` else `inf`.
     - Track `min_margin_ratio = min(min_margin_ratio, margin_ratio)`.
     - **Liquidation**: if `perp_equity <= maintenance` (i.e. `margin_ratio <= 1.0`):
       - Treat as forced close. `realized = short_size * (entry_price - P)` is large negative.
       - Set `cash_loss = perp_cash` (we lose all margin). Set `perp_cash = 0`, `short_size = 0`, `entry_price = 0`, `in_position = False`. Increment `n_liquidations`. Skip the perp_fee for liquidation.
       - For `A_cycle`: also unwind spot leg using current `P` (with spot fee).
     - **Else if** `margin_ratio < top_up_trigger`: top-up.
       - `target = healthy_ratio * maintenance`
       - `top_up = target - perp_equity`
       - If `spot_cash >= top_up`: `spot_cash -= top_up; perp_cash += top_up; n_top_ups += 1`.
       - Else: partial top-up `avail = max(0, spot_cash); spot_cash -= avail; perp_cash += avail`. Recompute `margin_ratio`. If still `margin_ratio < 1.5` → forced close:
         - `realized = short_size * (entry_price - P); perp_cash += realized - short_size * P * PERP_TAKER`
         - `spot_cash += perp_cash; perp_cash = 0`
         - `short_size = 0, entry_price = 0, in_position = False`
         - Increment `n_forced_closes`. For `A_cycle`: also unwind spot leg.

7. **On normal exit** (existing close path on exit signal):
   - If NOT `margin_active`: unchanged.
   - If `margin_active`:
     - `realized = short_size * (entry_price - P); perp_cash += realized - short_size * P * PERP_TAKER`
     - `spot_cash += perp_cash; perp_cash = 0`
     - Spot leg unwind in `A_cycle` uses `spot_cash`.

8. **Equity computation** (the `equity_now = ...` line):
   - If NOT `margin_active`: existing line unchanged.
   - If `margin_active`: `equity_now = spot_cash + perp_cash + units_spot * P + (short_size * (entry_price - P) if in_position else 0)`.

9. **Final close** (the end-of-loop "Финал: закрыть всё" block):
   - Branch similarly: if `margin_active`, close perp from `perp_cash` and add to `spot_cash`; spot unwind goes to `spot_cash`.

10. **Info dict additions** (only when `margin_active`):
    - `info["n_liquidations"] = n_liquidations`
    - `info["n_top_ups"] = n_top_ups`
    - `info["n_forced_closes"] = n_forced_closes`
    - `info["n_skipped_opens_capital"] = n_skipped_opens_capital`
    - `info["min_margin_ratio"] = min_margin_ratio if min_margin_ratio < float("inf") else float("nan")`
    - `info["peak_committed_capital"] = peak_committed` — max value of `POSITION_SIZE * (1 + 1/lev * margin_buffer_x)` observed (for spot_keep variants, add the permanent `POSITION_SIZE` of spot leg).
    - `info["final_spot_cash"] = spot_cash`
    - `info["final_perp_cash"] = perp_cash`
    - When `margin_active` is False — these keys are absent (do not add them).

11. Create `research/verify_engine_margin.py`:
    - Load BTC data via `load_data("BTC")`.
    - Use plain `print()` and `assert ...` (no pytest).
    - Scenarios listed in **Acceptance criteria** below.

## Acceptance criteria

1. **Backwards compat for A_cycle**: `simulate(df, strategy="A_cycle")` and `simulate(df, strategy="A_cycle", per_coin_leverage=None)` produce identical `pnl_arr` (`np.array_equal`).
2. **Backwards compat for A_spot_keep**: same check.
3. **Backwards compat for B** (with `regime_below_ma` filter): same check.
4. **Backwards compat for B_hedge** (with a synthetic boolean hedge_signal np.array): same check.
5. **Margin reserved at open**: With `per_coin_leverage={"BTC": 10}, coin="BTC", margin_buffer_x=3.0`, the verify script asserts `info["peak_committed_capital"] >= POSITION_SIZE + POSITION_SIZE/10 * 3.0` (= 1300).
6. **Liquidation triggers**: Build a synthetic short DataFrame where price triples from entry within 24 hours. Run with `per_coin_leverage={"BTC": 20}` (high leverage, tight maint), low `margin_buffer_x=1.5`. Assert `info["n_liquidations"] >= 1`.
7. **Top-up triggers**: Synthetic data with moderate adverse moves (e.g., price rises 10% during position). Assert `info["n_top_ups"] >= 1` and `info["n_liquidations"] == 0`.
8. **Forced-close on insufficient spot**: Set very small initial capital (override `TOTAL_CAPITAL` doesn't work — instead pass `per_coin_leverage={"BTC": 100}` so margin is tiny, then drain spot_cash deliberately via a setup; or simpler: contrive scenario where `spot_cash` is barely above required_margin at open, then a moderate adverse move forces top-up demand > spot_cash). Assert `info["n_forced_closes"] >= 1`. **If hard to construct cleanly**, this assertion may be relaxed to `info["n_forced_closes"] >= 0` (i.e., field exists), and the test verifies the code path is reachable by inspecting that the margin-active branch was entered.
9. The verify script prints a one-line summary per scenario and exits with code 0 if all assertions pass.

## Tests or validation to run

```bash
cd /Users/d/prj/funding-rate-arbitrage

# 1) Run verify script — must exit 0 with all assertions passing.
uv run python research/verify_engine_margin.py

# 2) Existing scripts must produce identical output (sanity check no positional-arg breakage).
uv run python research/backtest_a.py 2>&1 | tail -20

# 3) Quick syntax / import smoke:
uv run python -c "from research.engine import simulate, DEFAULT_MAINT_RATIO; print('ok')"
```

## Risks and edge cases

- **DO NOT** refactor existing code structure unless necessary. Wrap new behavior in `if margin_active:` branches alongside existing logic.
- The `info` dict is consumed by `concurrency_cap.py` and similar scripts via `.get()` or direct keys. Adding NEW keys is safe. REMOVING or RENAMING existing keys breaks downstream.
- `min_margin_ratio` initialization: start at `float("inf")` and only update inside positions. Final value `nan` if never had a position.
- For `B_hedge` strategy, the perp open path is separate from the main entry branch — make sure margin logic is applied there too.
- The final "close everything" block at the end of the loop must mirror the margin branch.
- When `margin_active` is True but `cash` variable is also being read (legacy spots), prefer `spot_cash + perp_cash` for equity but use the dedicated variables for state. Do not let `cash` drift out of sync — easiest: set `cash = spot_cash + perp_cash` after each modification when `margin_active`, OR avoid touching `cash` entirely in the margin branch.
- Floating-point comparisons: use `<=` for liquidation threshold (`margin_ratio <= 1.0`) to be inclusive.
- If `margin_ratio` becomes negative (deep underwater) — `min_margin_ratio` should still record it.

## Prompt for the coding agent

```text
Implement this task exactly.
Keep the diff minimal.
Do not change unrelated files.
Do not redesign the solution.
Follow all constraints and acceptance criteria.
If you are unsure, stop and explain the uncertainty instead of guessing.

Critical: when `per_coin_leverage=None` (the default), the function must produce byte-identical pnl_arr for ALL existing strategies. Wrap new behavior in an `if margin_active:` branch. Do not refactor the existing code path.

Task:
[paste this task file content here, or feed via --file]
```

## Suggested OpenCode command

```bash
opencode run \
  --model ollama/qwen3.6:35b-a3b-coding-nvfp4 \
  --agent build \
  --file .ai/task-001-engine-margin-model.md \
  "Implement the attached task exactly. Keep the diff minimal. Do not change unrelated files. Do not redesign the solution."
```

Non-interactive variant (run only on a clean git tree):

```bash
opencode run \
  --dangerously-skip-permissions \
  --model ollama/qwen3.6:35b-a3b-coding-nvfp4 \
  --agent build \
  --file .ai/task-001-engine-margin-model.md \
  "Implement the attached task exactly. Keep the diff minimal. Do not change unrelated files. Do not redesign the solution."
```

Before using the dangerous variant, verify support:

```bash
opencode run --help | grep -i permission
```
