# T7 — Verdict: is the `two_phase` edge real or an overfit artifact?

_Opus, 2026-06-08. Companion to the auto-generated `MONTE_CARLO_REPORT.md` (T6).
Numbers from the production run: 500 paths × 365 d × 5 coins (BTC/ETH/SOL/HYPE/PURR),
mbuf 3.0, prod params, both generators. Occupied-capital figures from a 120-path
per-path measurement (net P&L ÷ measured avg-deployed)._

## Short answer

**The edge is REAL — but thin, regime-conditional, and structurally low-risk; NOT the
fantasy the single-path Calmar implied.** It is a funding-harvesting utility, not an
alpha engine. It does not lose money on a yearly basis in 1000 simulated paths, but
its return is almost entirely a function of the funding regime, which we do not control.

## The numbers that matter (occupied capital — base-independent)

| | cold-only (bootstrap) | full hot+cold (parametric) |
|---|---|---|
| occupied APR median | **8.6%** | **25.6%** |
| occupied APR p05 → p95 | 7.1% → 10.6% | 15.5% → 52.3% |
| P(annual loss) | **0%** (0/500) | **0%** (0/500) |
| avg deployed capital | $286 | $299 |

Full-budget basis (budget $1 000, ~70% idle): cold median 1.5%, full-cycle median 8.0%.
**Occupied multiplier ≈ 3.3×** (budget / avg-deployed ~$290–300), stable across regimes
because the K=3 concurrency cap binds deployment in both. This matches the documented
live occupancy (~$345) and prior memory (`feedback_apr_denominator`: Real APR ~20–25%).

## Why "real, not overfit"

1. **Positive in 1000/1000 paths**, across BOTH a model-based generator (parametric)
   AND a history-resample (bootstrap). P(loss)=0% in each. The single historical
   backtest was not a lucky path — the carry-minus-fees edge survives thousands of
   alternative histories.
2. **The single-path sits INSIDE the distribution, not above it.** Single-path return
   2.50% (full-budget, cold window) lands around the bootstrap p75–p95 — a *slightly
   favourable* draw of the cold regime, not an outlier. Its drawdown (0.078%) is the
   parametric median exactly — typical.
3. **The tiny drawdown / huge Calmar is STRUCTURAL, not fitted.** max_dd p95 ≈ 0.13%
   (full-budget) across all paths because the book is delta-neutral. The legendary
   "Calmar 114" is the mechanical artefact of a near-zero denominator — **Calmar is a
   misleading metric here; do not anchor on it.** The MC parametric Calmar median is
   ~112, so a high Calmar is *typical* for this strategy, i.e. structural, not special.

## Why "thin and regime-conditional"

- Occupied APR spans ~8.6% (cold) to ~25.6% (full cycle) — a **3× spread driven entirely
  by the funding regime**, not by skill. In cold the strategy earns ≈ lending; the upside
  is hot-funding rent we don't control. This *quantifies* the thesis we kept landing on:
  **frab in cold ≈ lending; the rest is regime luck.**
- There is no edge beyond "capture funding net of fees without losing principal."

## Two risks the MC surfaced that the single cold backtest hid

1. **Liquidations in hot regimes** — parametric averages **0.60 liquidations/path/yr**
   (vs 0 in the historical cold backtest). Higher hot-regime price vol occasionally trips
   the margin model. In a delta-neutral book these don't blow up equity (the spot leg
   offsets), so max_dd stays tiny — but they are real operational events + fee churn.
2. **Heavy churn in cold** — bootstrap averages **NEGSTOP 10.8 + Phase-2 12 exits/path/yr**:
   the book constantly cuts and re-enters as funding flips negative. The 2026-06-08
   `CLOSE_PHASE1_NEGSTOP` fix is doing real work, but it implies high turnover (fee drag)
   exactly in the cold regime where the margin for error is thinnest.

## Honest limitations of this MC (what it still cannot tell us)

- **HYPE/PURR hot-regime price vol is BORROWED from the majors** (no native hot price
  history) — their hot-regime price & liquidation behaviour is *modelled, not observed*.
- **Stationarity assumed.** Generators replay the *historical* regime statistics. A
  **structural break** — funding permanently compressing as the carry trade crowds, an
  exchange failure, an LST depeg — is NOT in the distribution. The MC answers "if the past
  regime statistics repeat"; it cannot price a regime that never occurred.
- **Bootstrap is cold-only** (5-coin price intersection is 2025-11→2026-05): its
  distribution is one regime reshuffled, not a full cycle. Parametric is the only
  hot-capable generator, and it rests on the borrowed-vol assumption above.
- This validates the **edge/robustness**, not parameter overfit per se — walk-forward
  (train/test on disjoint history) is the missing sibling (see PLAN sibling-track).

## Recommendation (capital sizing)

- **Forward expectation on occupied capital:** cold floor ~8%, full-cycle blend ~15–26%,
  no yearly loss *in the model*. On TOTAL capital with ~30% deployment that is the
  ~1.5–8% full-budget band — i.e. to earn occupied-rate returns on $50k you must deploy
  more (more coins / higher concurrency), which raises the liquidation + operational risk
  the MC just flagged.
- **Role:** size `two_phase` as the **capital-preservation + modest-carry sleeve**, not
  the growth engine. It reliably doesn't lose and pays ~lending-plus in cold, ~20%+ in
  hot — but you don't pick the regime. This is consistent with the whole strategic thread:
  the delta (excess over lending) lives in *directional* strategies, not in delta-neutral
  carry.
- **Before scaling deployment:** add walk-forward validation, and instrument live
  liquidation frequency against the parametric 0.6/path/yr prediction.

## Bottom line

`two_phase` is a sound, low-risk funding harvester whose backtested headline numbers were
**not overfit but were flattered by (a) a near-zero-drawdown Calmar and (b) the budget
denominator**. Stripped to occupied capital and run across regimes, it is ~8% in cold and
~25% in a full cycle, never negative on the year in 1000 paths — a dependable preservation
sleeve, not the source of alpha.
