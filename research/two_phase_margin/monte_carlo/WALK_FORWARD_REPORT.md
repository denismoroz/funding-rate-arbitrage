# Walk-Forward Validation Report

## Configuration

- Coins: BTC, ETH, SOL
- Train window: 12 months
- Test window: 3 months
- Step: 3 months
- IS optimisation metric: `annual`
- Number of folds: 7

## Per-Fold Results

| Fold | Train Period | Test Period | Best entry_thr | Best ph2_exit | IS Annual | IS Calmar | OOS Annual (tuned) | OOS Calmar (tuned) | OOS Annual (static) | OOS Calmar (static) |
|------|-------------|------------|----------------|---------------|-----------|-----------|-------------------|------------------|--------------------|---------------------|
| 0 | 2023-06-08→2024-06-08 | 2024-06-08→2024-09-08 | 0.15 | -0.10 | 21.08% | 134.01 | 5.91% | 74.04 | 6.38% | 92.81 |
| 1 | 2023-09-08→2024-09-08 | 2024-09-08→2024-12-08 | 0.10 | -0.10 | 20.12% | 83.33 | 19.94% | 241.76 | 19.94% | 241.76 |
| 2 | 2023-12-08→2024-12-08 | 2024-12-08→2025-03-08 | 0.05 | -0.05 | 18.55% | 142.21 | 6.26% | 44.33 | 6.26% | 27.46 |
| 3 | 2024-03-08→2025-03-08 | 2025-03-08→2025-06-08 | 0.05 | -0.10 | 11.01% | 69.99 | 2.00% | 6.99 | 0.85% | 2.39 |
| 4 | 2024-06-08→2025-06-08 | 2025-06-08→2025-09-08 | 0.05 | -0.10 | 6.89% | 35.25 | 7.98% | 48.29 | 8.49% | 51.35 |
| 5 | 2024-09-08→2025-09-08 | 2025-09-08→2025-12-08 | 0.15 | -0.10 | 9.79% | 84.69 | 2.45% | 16.80 | 4.67% | 53.84 |
| 6 | 2024-12-08→2025-12-08 | 2025-12-08→2026-03-08 | 0.15 | -0.10 | 5.22% | 41.93 | 1.47% | 27.46 | 1.71% | 9.47 |

## Aggregate Summary

| Metric | Mean IS (tuned) | Mean OOS (tuned) | Mean OOS (static) |
|--------|----------------|-----------------|-------------------|
| Annual return | 13.24% | 6.57% | 6.90% |
| Calmar ratio | 84.49 | 65.67 | 68.44 |

**IS → OOS degradation (annual):** -6.66% (-50.3% relative)

**Tuned OOS vs Static OOS:** static outperforms tuned by 0.33%. Tuning HURTS OOS — consistent with over-fitting (cf. memory project_quant_research: 45%→22% degradation observed in single-param tuning).

## Best-Param Frequency (IS selections)

| entry_threshold_apr | phase2_exit_threshold | # folds selected |
|--------------------|-----------------------|-------------------|
| 0.05 | -0.10 | 2 |
| 0.05 | -0.05 | 1 |
| 0.10 | -0.10 | 1 |
| 0.15 | -0.10 | 3 |

## Interpretation Notes

This walk-forward measures **parameter over-fit risk**, not edge robustness. The Monte Carlo harness (T5/T6) separately validated that the edge survives alternative price/funding paths. These are complementary questions.

IS metric is CAGR (compound annual) on the full portfolio equity curve (prod_slot sizing, mbuf=3.0, BTC/ETH/SOL only). Note: equity includes idle cash in the budget, so APR on deployed capital is higher (~4–5× for 3-coin book).

The param grid is intentionally small (default: 4 entry thresholds × 2 exit thresholds = 8 combos; 7 folds). A larger grid would take proportionally longer. The selected params vary by fold, which itself is evidence of instability.

**Verdict: left to Opus (PLAN.md T8).** Numbers are reported without editorial conclusion — Opus will assess whether degradation is real and whether prod should use static or ensemble params.

---

## Verdict (Opus, 2026-06-08)

**Tuning the two_phase parameters on history does NOT generalize. Prod should run on
the static (or ensemble) params it already uses — not on historically-optimal ones.**

Three facts decide it:

1. **The IS→OOS degradation is real and large: 13.24% → 6.57% (−50% relative).** Across
   7 folds (2023-06 → 2026-03, hot+cold) the in-sample-optimal config keeps barely half
   its return out-of-sample. Each train window finds params that *looked* best on its own
   12 months; the next 3 months don't honor that. This is the textbook over-fit
   signature, and it shows up on REAL data (not synthetic) — independent of, and
   complementary to, the MC edge-robustness result.

2. **Tuning loses to doing nothing: tuned OOS 6.57% < static OOS 6.90% (−0.33pp).**
   Re-tuning every quarter is strictly worse than just holding the prod defaults — and
   that's *before* counting the operational cost and turnover of changing params. The
   one fold where tuning helped (fold 3: 2.00% vs 0.85%) is swamped by folds where it
   hurt (fold 0: 5.91% vs 6.38%; fold 5: 2.45% vs 4.67%). Net: tuning is a coin-flip
   that costs you on average.

3. **The IS-optimal params don't even agree with themselves: 4 distinct configs across
   7 folds, none chosen >3 times.** `entry_threshold_apr` jumps 0.05↔0.10↔0.15 fold to
   fold with no stable winner. There is no "true" historically-best parameter to lock in
   — the optimizer is fitting regime noise, not a durable property of the strategy.

**This independently reproduces the broad-study finding (`project_quant_research`:
single-param tuning 45%→22% live, while a param-ensemble held).** Same lesson, different
strategy: do not chase historically-optimal parameters; they are a trap. The current
static prod params are the right default. If anything is worth adding, it is an
**ensemble/robust** choice (e.g. params that are median-good across folds), never the
per-window argmax.

**Caveat on absolute level:** OOS annual ~6.6% here is full-budget CAGR on a 3-coin
(BTC/ETH/SOL) book with prod_slot sizing — deployed-capital APR is ~4–5× higher, and
this excludes HYPE/PURR (no multi-regime history). Read these numbers as a *relative*
IS-vs-OOS / tuned-vs-static comparison, not as a forward return forecast — that is what
the MC harness (T5–T7) is for.

**Action:** keep prod on static params. Do NOT wire a periodic re-optimization loop.
T8 closes the parameter-overfit question; combined with MC (edge-robustness), the
two_phase validation track is complete.
