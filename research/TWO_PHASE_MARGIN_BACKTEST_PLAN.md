# Plan: Two-Phase Dynamic + Margin Backtester (matches prod)

## Why this exists

Current state: we have two disjoint backtesters in `research/`:
- [portfolio_margin.py](portfolio_margin.py) — has cross-margin model with per-coin leverage, req_margin reserves, top-up, forced-close. BUT uses simplistic `min_hold=120h + exit_threshold` decision logic.
- [two_phase_dynamic.py](two_phase_dynamic.py) — has the real two-phase exit + dynamic min_hold logic. BUT no margin model.

Production runs [src/frab/strategy/two_phase/](../src/frab/strategy/two_phase/) which IS two-phase dynamic AND has margin policy. The disjoint backtesters give numbers (1.43% APR, 15.34% APR) that don't apply to the actual deployed strategy.

**Goal:** ONE backtester that matches prod. Combines:
- Two-phase exit + per-position dynamic min_hold (from prod)
- Margin model: req_margin reserves, top-up, forced-close (from portfolio_margin.py)
- All leverages / fees / coefficients pulled from prod source files (never hardcoded).

## What to pull from prod (single source of truth — DO NOT hardcode)

Open these files at runtime and use their values:

1. **[src/frab/constants.py](../src/frab/constants.py)**:
   - `RESEARCH_LEVERAGE: dict[str, int]` — per-coin max leverage
   - `RESEARCH_MAINT_RATIO: dict[str, float]` — per-coin maintenance ratio
   - `PERP_TAKER`, `SPOT_TAKER` — fee constants
   - Also check for `DEFAULT_LEVERAGE` / `DEFAULT_MAINT_RATIO` fallbacks
   - Read these via `from frab.constants import ...` if importable, otherwise `ast.parse` the file.

2. **[src/frab/strategy/two_phase/params.py](../src/frab/strategy/two_phase/params.py)** — `TwoPhaseParams` dataclass:
   - `coins`
   - `entry_threshold_apr` (default 0.10)
   - `phase2_exit_threshold` (default −0.10)
   - `base_min_hold_hours` (default 24)
   - `safety_mult` (default 5.0)
   - `cap_min_hold_hours` (default 720)
   - `signal_window_hours` (rolling mean window for signal)
   - `phase1_negative_patience_hours` (default 72)
   - `phase1_breakeven_cap_hours` (default 720)
   - Plus any margin-related defaults if present

3. **[src/frab/strategy/two_phase/strategy.py](../src/frab/strategy/two_phase/strategy.py)** — re-read the actual decision tree to match port behavior to prod exactly. Look for:
   - How signal is computed (rolling mean? exponential?)
   - How break-even is determined (`hours_to_breakeven`)
   - How phase1 vs phase2 is decided
   - When `manual_open` paths skip checks (we are NOT simulating manual_open in the backtest — pure systematic)
   - Concurrency cap K (likely from params)
   - Margin buffer multiplier (likely from params or constants)

4. **Live DB strategy params** (if accessible):
   - `data/frab.db` → `strategies` table → most recent active strategy row → `params_json` field
   - These OVERRIDE the dataclass defaults if a user has customized.
   - If DB not accessible, fall back to params.py defaults and CLEARLY state in report.

5. **Position sizing**:
   - Look for how prod determines per-coin notional. Likely `position_size_usdc` parameter, possibly derived from wallet balance × per-coin fraction.
   - Use the SAME value backtest. If parameter is "auto-derived from wallet", model wallet=$1000 to get the size.

## What to keep from research/portfolio_margin.py

- The fixed margin accounting (post req_margin double-count fix at line 437)
- `accrue_funding` per-tick
- `apply_margin_policy` (top-up + forced-close)
- The per-coin attribution dict (`per_coin[c]["funding_gross"]`, etc.)
- Signal computation via rolling mean of hourly funding rate × 8760

DO NOT keep:
- Hardcoded `MIN_HOLD_HOURS = 120` constant
- The `ENTRY_THRESHOLD / EXIT_THRESHOLD` constants at module level (use params from prod)
- The simplistic close logic — replace with two-phase

## Two-phase exit + dynamic min_hold logic (port from prod)

For each open position at each hour:

```
hours_in = pos.hours_in (now incremented)
gross_funding = pos.funding_accrued_so_far
fees_to_recoup = open_fees_paid + estimated_close_fees  # ~ POSITION_SIZE * 0.0021

# Dynamic min_hold for THIS position based on entry rate:
entry_rate_annual = pos.entry_rate_annual  # captured at open
breakeven_h = 18.4 / entry_rate_annual  # 18.4 = 0.0021 × 8760
position_min_hold = min(cap_min_hold_hours, max(base_min_hold_hours, safety_mult × breakeven_h))

if hours_in < position_min_hold:
    # Not allowed to exit by min_hold guard
    continue

# Two-phase decision:
if gross_funding < fees_to_recoup:
    # PHASE 1 — still recovering fees
    # Exit only if either:
    # (a) consecutive_negative_hours >= phase1_negative_patience_hours, OR
    # (b) hours_to_breakeven_at_current_rate > phase1_breakeven_cap_hours
    current_rate = recent funding rate
    if current_rate <= 0:
        pos.consec_neg += 1
    else:
        pos.consec_neg = 0
    if pos.consec_neg >= phase1_negative_patience_hours:
        CLOSE("phase1_consec_neg")
    elif current_rate > 0:
        hours_to_be = (fees_to_recoup - gross_funding) / (current_rate / 8760 * POSITION_SIZE)
        if hours_to_be > phase1_breakeven_cap_hours:
            CLOSE("phase1_cap_exceeded")
else:
    # PHASE 2 — past break-even, take profit when signal degrades
    smoothed_signal = rolling 12h mean × 8760
    if smoothed_signal < phase2_exit_threshold:
        CLOSE("phase2_signal_degraded")
```

**Important:** verify exact formulas against `src/frab/strategy/two_phase/strategy.py` before implementing. If prod differs from above pseudocode, prod wins.

## Test runs

1. **U-prod** = current production universe (read from DB or params.py). Run with prod params unmodified.
2. **U3** = `["BTC", "ETH", "SOL"]` (same leverages from constants) — for comparison.

**Time window:** common timeline across the universe. For U-prod limited by HYPE/PURR (~Dec 2024 → today). For U3 use the SAME restricted window (not full 2.9 years) so it's apples-to-apples.

**Margin grid:** also vary `margin_buffer_x ∈ {3, 5}` since prod buffer is configurable. Document which value matches actual prod config.

**Budget:** $1000 default. If prod actually uses different, also run that.

## Output files

- `research/TWOPHASE_MARGIN_aggregate.csv` — one row per (universe × buffer) with columns: universe, margin_buffer_x, position_size, K, period_start, period_end, n_hours, annual_pct, sharpe, sortino, max_dd_pct, total_funding (sum of per_coin), total_fees, final_equity, n_liquidations, n_top_ups, n_phase1_exits, n_phase2_exits, n_min_hold_exits
- `research/TWOPHASE_MARGIN_per_coin.csv` — one row per (universe × buffer × coin) with: universe, buffer, coin, n_opens, n_closes, funding_gross, fees_paid, hours_in_position, n_phase1_exits, n_phase2_exits
- `research/TWOPHASE_MARGIN_REPORT.md` — markdown summary

## Report contents (under 400 words)

1. **Source verification**: paste actual values pulled from prod (leverages dict, MAINT_RATIO dict, fees, all TwoPhaseParams field values). Confirms no hardcoding drift.
2. **U-prod result**: annual_pct, Sharpe, total funding, total fees, per-coin attribution table.
3. **U3 result on same window**: same.
4. **Apples-to-apples verdict in one sentence**: did adding HYPE/PURR vs BTC/ETH/SOL alone help or hurt over the same period?
5. **Exit reason breakdown** for U-prod: how many positions exited via min_hold, phase1_consec_neg, phase1_cap_exceeded, phase2_signal_degraded? This tells us if two-phase logic is even firing.
6. **Honest limits**: what aspects of prod are STILL not modeled (e.g. real HL slippage, atomic execution failures, partial fills, recovery from half-open). One bullet list.

## Verification steps

Before reporting:
1. **Synthetic test** (REQUIRED): single coin, constant 20% APR funding, 1000h flat price. With prod params (entry=0.10, exit=−0.10, base_min_hold=24, safety_mult=5, etc.):
   - entry_rate_annual = 0.20, breakeven_h = 18.4/0.20 = 92h
   - position_min_hold = min(720, max(24, 5×92)) = 460h
   - Position should open after signal warmup, hold ≥460h, then close only when signal drops below −0.10 (which never happens with constant rate, so position stays open to end)
   - Expected funding = $100 × 0.00002283 × hours_held = $2.28
   - PASS criteria: per_coin funding matches expected within $0.01
2. **Zero-funding test**: same but funding=0. Position should never enter phase 2 (gross_funding stays 0 < fees_to_recoup). Should exit via phase1_cap_exceeded after position_min_hold is exhausted.
3. **U-prod run** must finish without errors. n_liquidations should be 0 with prod's buffer setting.

## Constraints

- DO NOT touch `src/frab/` — research only.
- DO NOT commit. Working tree only.
- DO NOT write Russian in code/reports — English.
- DO NOT hallucinate numbers. Every figure from your actual run.
- DO read the prod source files BEFORE writing any logic. Match prod, don't invent.
- DO log the actual prod values you pulled (leverages, fees, params) at the top of the report.

## Reference files

- [research/portfolio_margin.py](portfolio_margin.py) — keep margin model
- [research/two_phase_dynamic.py](two_phase_dynamic.py) — reference for two-phase logic (no margin)
- [src/frab/strategy/two_phase/strategy.py](../src/frab/strategy/two_phase/strategy.py) — SOURCE OF TRUTH for decision logic
- [src/frab/strategy/two_phase/params.py](../src/frab/strategy/two_phase/params.py) — SOURCE OF TRUTH for params
- [src/frab/constants.py](../src/frab/constants.py) — SOURCE OF TRUTH for leverages/fees
- [data/frab.db](../data/frab.db) → `strategies` table — actual live params (override defaults)
