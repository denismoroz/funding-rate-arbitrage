# Spec: Persist break-even phase as a first-class FarbState

**Status:** ready for delegation
**Author/reviewer:** Opus (review + commit). **Implementer:** Sonnet, per workstream.
**Origin:** ETH #19 analysis 2026-06-10 — "phase" (Phase 1 / Phase 2) is recomputed every
tick from `in_profit = gross_funding_so_far >= total_fees_paid`, with no persistence and no
hysteresis. A position oscillating around break-even can flap PRE↔POST forever and dodge
every exit (Phase-2 threshold misses when momentarily PRE; `consec_negative` resets on any
positive blip; `CLOSE_PHASE1_CAP` is dead near BE; negstop needs < −0.15). Cost is opportunity
(a stuck concurrency slot), not a loss. See memory `project_two_phase_exit_gap`.

**Fix direction (decided):** make the phase a real persisted `FarbState`, latched one-way,
on par with the rest of the state machine. No hysteresis/high-water-mark hacks — the latch IS
the persisted state.

## Locked decisions

1. **Latch semantics:** PRE_BREAKEVEN → POST_BREAKEVEN triggers when `cum_funding` **ever**
   crosses `>= total_fees_paid`. One-way; the crossing is permanent (a true latch), not a
   snapshot of current `in_profit`. Backfill must scan `funding_accruals` history.
2. **POST give-back:** once POST_BREAKEVEN, a position **never returns** to PRE_BREAKEVEN, even
   if `cum_funding` later drops back below fees. POST watches only the exit threshold. (This is
   the backtested variant.)
3. **Rename decision enums** too, for consistency: `CLOSE_PHASE1_NEG`→`CLOSE_PRE_BE_NEG`,
   `CLOSE_PHASE1_CAP`→`CLOSE_PRE_BE_CAP`, `CLOSE_PHASE1_NEGSTOP`→`CLOSE_PRE_BE_NEGSTOP`,
   `CLOSE_PHASE2`→`CLOSE_POST_BE`. Ripple into tests + `research/`.
4. **Hard cutover:** single deploy + DB migration in lockstep (one prod instance). No `OPEN`
   deprecated alias.
5. **Frontend:** add a visible **`state` column** showing the position's current state.

## Naming

- FarbState values: `PRE_BREAKEVEN` (`"pre_breakeven"`), `POST_BREAKEVEN` (`"post_breakeven"`).
  Remove `OPEN`.
- Helpers: `FarbPosition.is_active` ≡ `state ∈ {PRE_BREAKEVEN, POST_BREAKEVEN}`;
  `FarbState.is_terminal` ≡ `state ∈ {CLOSED, FAILED}`. Module-level `ACTIVE_STATES`.
  Retire the `_STEADY_STATES` name (it lumped a live resting state with terminal ones); the
  advance-loop stop condition becomes "non-transient" = active ∪ terminal.

---

## W0 — Dev environment on a copy of the prod DB (FIRST; zero prod risk)

Sonnet prompt MUST include `ssh dis@10.8.0.5` (prod is `dis@10.8.0.5`, repo
`/Users/dis/prj/funding-rate-arbitrage`, db `data/frab.db`).

- Copy prod DB locally to a separate file: `scp dis@10.8.0.5:/Users/dis/prj/funding-rate-arbitrage/data/frab.db ./data/frab.local.db`
- In the **local copy only**: `UPDATE strategies SET status='paused';` so even a started engine
  trades nothing. Keep `dry_run=True` invariant (all mutating executor calls blocked).
- Bring up the local web against the copy. This is the test bed for W5/W7. Never run the engine
  against prod from local.

## W1 — Domain: states + helpers (pure code, unit tests FIRST)

- `src/frab/domain/enums.py`: add `PRE_BREAKEVEN`, `POST_BREAKEVEN`; remove `OPEN`. Add
  `FarbState.is_terminal`. Add `ACTIVE_STATES`.
- `FarbPosition.is_active` helper.
- `src/frab/engine/two_phase_signals.py` `decide_two_phase`: **remove the `in_profit`
  computation**. The phase is the caller's `fp.state`; the function decides exits given the
  known phase, it does not reconstruct it from funding-vs-fees. Rename the decision enum values
  per decision 3.

## W2 — Phase handlers (isolated modules + a test per state)

Resting states (like `OPEN` today): dispatched by the hourly `ExitEvaluator`, not the
minute-tick advance loop. Make them first-class isolated handlers keyed on `fp.state`.

- `src/frab/strategy/two_phase/states/pre_breakeven.py`: negstop → min_hold gate → consec_neg →
  cap; **plus the latch transition PRE→POST** when `cum_funding >= fees`.
- `src/frab/strategy/two_phase/states/post_breakeven.py`: only the
  `signal < phase2_exit_threshold` guard → CLOSING_SHORT. Never transitions back to PRE.

## W3 — Repo + transitions

- `src/frab/repo/farb_repo.py`: `list_open` → `list_active` (`state IN ACTIVE_STATES`); add the
  PRE→POST transition guard.
- `src/frab/strategy/two_phase/states/opening_short.py`: target state `OPEN` → `PRE_BREAKEVEN`.

## W4 — Cross-cutting backend (every place that assumes the single OPEN steady state)

Replace `list_open` / `FarbState.OPEN` with the active set in:
`actions/funding_accrual.py` (accrue in BOTH phases), `engine/margin_watchdog.py`,
`strategy/two_phase/strategy.py` (the advance-loop stop set), `evaluators/entry.py`,
`ledger/ledger.py`, `api/routes/equity.py`.
`api/routes/farb_positions.py`: the `?status=open` filter → active set; close-eligibility =
`state ∈ ACTIVE_STATES`.

## W5 — Migration + backfill (validate on the W0 copy, NOT prod)

- Alembic data migration: `state='open'` → `pre/post`.
- Backfill the 4 live positions by scanning `funding_accruals`: POST if cumulative funding
  EVER reached `>= total_fees_paid`, else PRE. (ETH #19 → PRE — it never crossed; peak was
  −$0.00065 from BE at 2026-06-09 19:00 MSK.)
- Run + verify the migration on `data/frab.local.db` first.

## W6 — Frontend

- `web/src/lib/api.ts`: types + the `"open"` filter → active.
- `web/src/components/OpenFarbPositions.tsx`: fetch active; close-button gate on active;
  `pre`/`post`-break-even badges; **add a dedicated visible `state` column** (decision 5).
- `web/src/components/Header.tsx`: status-dot colors for the new states; **the header funding
  aggregate must sum over the active set**, not the literal `"open"` string. Sonnet locates the
  exact aggregation site and fixes it.
- UI labels: "pre-break-even" / "post-break-even".

## W7 — Integration run on the copy

Apply migration to the prod-DB copy, bring up web, verify: ETH #19 shows pre-break-even,
header funding correct, no trades (strategy paused).

---

## Delegation order & review

W1 (domain + tests) → W2 (handlers + tests) → W3/W4 (backend) → W5 (migration, validated on
copy) → W6 (frontend, incl. `state` column) → W7 (integration). Tests precede code in each
workstream. Opus reviews + commits each (memory `feedback_delegation`). W0 prompt carries
explicit prod creds (`feedback_subagent_prod_creds`) and the `dry_run` invariant
(`feedback_dry_run_invariant`). Do not touch prod until the whole chain is green on the local
copy and reviewed.
