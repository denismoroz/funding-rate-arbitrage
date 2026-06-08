# T7 — Verdict: is the `two_phase` edge real or an overfit artifact?

_Opus, 2026-06-08. Companion to the auto-generated `MONTE_CARLO_REPORT.md` (T6).
500 paths × 365 d × 5 coins (BTC/ETH/SOL/HYPE/PURR), mbuf 3.0, both generators._

> **⚠️ SIZING CORRECTION (2026-06-08, after live cross-check).** The first pass used
> the research sweep's FLAT sizing (fixed $100 notional, ~70% budget idle), which is
> NOT how prod allocates. Prod fills a fixed **slot = budget/K** per position with
> per-coin `notional = slot/(1+buffer/lev)` + a locked margin buffer (params.py). The
> engine now has a `prod_slot` mode replicating this; all numbers below are prod_slot.
> The earlier "occupied 8.6%/25.6%" figures OMITTED the buffer haircut and the
> cold-regime idle — the corrected numbers are LOWER. (`flat` mode and the original
> report are preserved unchanged for the regression anchor.)

## Short answer

**The edge is REAL — but thin, regime-conditional, and structurally low-risk; NOT the
fantasy the single-path Calmar implied.** It is a funding-harvesting utility, not an
alpha engine. It never loses on a yearly basis in 1000 simulated paths, but its return
is almost entirely a function of (a) the funding regime and (b) how much of the budget
it can actually deploy — neither of which it controls in cold.

## The numbers that matter (prod_slot sizing — full strategy budget)

| | cold (bootstrap) | hot+cold (parametric) |
|---|---|---|
| **APR on strategy budget** median | **1.3%** | **12.1%** |
| APR on budget p05 → p95 | 0.5 → 2.6% | 6.6 → 18.1% |
| **APR on _deployed_ capital** (≈ the edge on working money) | **~8%** | **~17%** |
| budget actually deployed (avg) | **16%** | **73%** |
| P(annual loss) | **0%** (0/500) | **0%** (0/500) |
| max_dd median / p95 | 0.08% / 0.16% | 0.14% / 0.18% |
| liquidations / path | 0.02 | 0.61 |

**Two denominators, both true and important:**
- **On DEPLOYED capital** the edge is intact and regime-driven: ~8% cold, ~17% hot —
  consistent with live (~6% gross-funding-on-notional, 2026-06) and the documented
  occupancy. This is "funding carry minus fees minus the leverage-dependent buffer
  haircut" (BTC@40× ≈ 7% haircut → PURR@3× ≈ 50%).
- **On the strategy BUDGET** the cold number collapses to **1.3%** — NOT because the
  edge died, but because **in cold only ~16% of the budget deploys**: few coins clear
  the entry threshold, so ~84% sits idle. In hot, 73% deploys → 12.1%.

**Consequence (vindicates the earlier "stack idle into lending"):** the cold-regime
idle is REAL in prod (coin-availability under the entry threshold), not a sizing
artifact. That idle budget MUST earn a base lending yield or the blended cold return is
~1.3%. With idle parked at ~5% lending, blended cold ≈ 0.84×5% + 0.16×8% ≈ **~5.5%** —
back to ≥ lending, as the live cross-check suggested.

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

- On-deployed APR spans ~8% (cold) to ~17% (hot) — a spread **driven entirely by the
  funding regime**, not by skill. Worse, on the *budget* the spread is 1.3% → 12.1%
  because cold ALSO starves deployment (only 16% of budget finds qualifying coins). The
  upside is hot-funding rent we don't control. This *quantifies* the thesis we kept
  landing on: **frab in cold ≈ lending; the rest is regime luck.**
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

- **Forward expectation on the strategy budget:** ~1.3% cold → ~12% hot, never a yearly
  loss *in the model*. The cold number is dragged by idle (only 16% deploys). The fix is
  NOT to over-deploy (that raises the liquidation/ops risk the MC flagged: 0.6 liq/path
  in hot) but to **park idle budget in base lending** — blended cold then ≈ 5.5%, i.e.
  ≥ lending. On-deployed the edge is ~8% cold / ~17% hot.
- **Role:** size `two_phase` as the **capital-preservation + modest-carry sleeve**, not
  the growth engine. With idle stacked in lending it pays ~lending-plus in cold and ~12%
  on budget in hot — but you don't pick the regime. Consistent with the whole thread:
  the delta (excess over lending) lives in *directional* strategies, not delta-neutral carry.
- **Before scaling deployment:** add walk-forward validation; instrument live liquidation
  frequency against the parametric 0.6/path/yr prediction; and confirm the live deployment
  fraction matches the model's regime-dependent 16%→73%.

## Bottom line

`two_phase` is a sound, low-risk funding harvester whose backtested headline numbers were
**not overfit but were flattered by (a) a near-zero-drawdown Calmar, (b) the budget
denominator, and (c) research sizing that ignored the prod buffer + cold-regime idle**.
Corrected to prod sizing across regimes, it is ~1.3% (cold, mostly idle) to ~12% (hot) on
the strategy budget — ~8%→17% on deployed capital — never negative on the year in 1000
paths. A dependable preservation
sleeve, not the source of alpha.
