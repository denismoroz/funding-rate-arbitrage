# Token-Unlock Cliff Short Book — Research Validation

## Thesis

Cliff token unlocks create predictable sell pressure that the market front-runs.
A market-hedged short in the window [-W, -1] before a large cliff unlock captures
this abnormal return as a near-orthogonal edge.

## Data

- **Source:** DeFiLlama free emission API (`defillama-datasets.llama.fi/emissions/{slug}`)
- **Coins:** 32 coins with local HL price data + DeFiLlama emission records
- **Events:** 5,629 total cliff events; 267 with ≥1% supply; 149 with ≥2% supply
- **Price data:** 1,102 days (2023-06-08 → 2026-06-13), local `_1h.csv` files

## Strategy Design

- **Signal:** window [-W, -1] days before known cliff unlock date
- **Direction:** short unlocking coin, long equal-weight universe (market-hedge)
- **Sizing:** proportional to unlock fraction (`size = tokens / max_supply`)
- **Costs:** 4.4 bps/leg one-way (perp taker 3.5 + slippage 0.9), both legs
- **Selected config:** W=10, thr=1% supply, sizing=prop

## Event-Study CAR (bootstrap 95% CI)

| Threshold | n_events | CAR[-10,-1] | CI [lo, hi] | Significant |
|-----------|----------|-------------|-------------|-------------|
| ≥0.5%     | 239      | -2.24%      | [-3.54%, -0.99%] | YES |
| ≥1%       | 127      | -3.81%      | [-5.80%, -1.85%] | YES |
| ≥2%       | 73       | -5.79%      | [-8.60%, -3.24%] | YES |

Signal monotonically strengthens with unlock size — signature of causality, not noise.

## Full-Period Metrics (selected config, √252 annualization)

| Metric | Value |
|--------|-------|
| Ann return | +27.0% |
| Ann vol | 38.3% |
| Sharpe | 0.71 |
| Max drawdown | -56.3% |
| Calmar | 0.48 |
| Skew | -5.13 |
| Kurtosis | 79.9 |

**Skew warning:** The heavy negative skew (−5.1) reflects short-squeeze tail risk.
On 2025-01-17, JUP surged +37% immediately before a major unlock (Jupiter airdrop
expansion), causing a -39.6% single-day book loss. This is real strategy risk — the
short can squeeze exactly when it "should" be winning.

## OOS CPCV Distribution (n=6, k=2, purge=14d, embargo=14d)

| Metric | OOS median | IQR [lo, hi] |
|--------|-----------|--------------|
| Sharpe (daily equiv) | **0.76** | [-0.11, 1.40] |
| Calmar (daily equiv) | **6.11** | [-0.58, 8.71] |
| Frac segments Sharpe>0 | **72%** | — |

## Harness Verdict

| Metric | Value | Interpretation |
|--------|-------|----------------|
| **PBO** | **0.57** ❌ | Config selection does NOT transfer OOS (high PBO) |
| **DSR** | **0.74** ⚠️ | Informational: negative skew inflates DSR threshold |
| OOS frac>0 | 72% | Positive edge in most regimes |
| corr_momentum | +0.012 | Near-zero (excellent orthogonality) |
| corr_carry | +0.047 | Near-zero (excellent orthogonality) |

## Sizing: Prop vs Equal-Weight

| Sizing | Sharpe | Ann Return | Calmar |
|--------|--------|-----------|--------|
| Proportional | 0.71 | +27.0% | 0.48 |
| Equal-weight | 0.60 | +22.9% | 0.41 |
| **Improvement** | **+17.5%** | **+4.1pp** | **+17%** |

Proportional sizing exploits the monotonic size→CAR relationship.

## Best Config (W=7, thr=0.01, prop)

From the menu sweep (self-test), W=7 with prop sizing slightly outperforms W=10
on Sharpe (0.77 vs 0.71), suggesting the market front-runs in a tighter 7-day
window. W=14 consistently underperforms — the signal decays beyond 10 days.

## Verdict

**Marginal-but-real orthogonal edge, NOT ready as a live sleeve.**

**Strengths:**
- Event-study CAR is statistically significant at all size thresholds (bootstrap CI excludes 0)
- Signal monotonically strengthens with unlock size (causality signature)
- Near-zero correlation to momentum (+0.01) and carry (+0.05) — strong orthogonality
- OOS Sharpe 0.76 with 72% positive segments
- Prop sizing improvement (+17.5% Sharpe) confirms the size→signal relationship

**Weaknesses / Risks:**
- PBO = 0.57 (high) → config selection is unreliable across regimes; no single
  (W, thr) dominates OOS consistently. Use a fixed pre-committed config (W=10).
- Skew = -5.1, kurtosis = 80 → fat left tail from short squeezes (JUP Jan 2025).
  The strategy can lose -40% in a day when an unlock triggers a price spike.
- Max drawdown -56% makes Calmar only 0.48 (acceptable for a research book, not
  sizing-significant as a live standalone strategy).
- Universe is small (30 coins) with relatively few large events per year in our
  price history window — event-count limits OOS robustness.
- DSR 0.74 is informational only; negative skew artificially suppresses DSR.

**Recommendation:** Use as a low-allocation orthogonal overlay (not standalone).
Gate size-up on funding > cost (same discipline as FRAB shakedown). Do NOT go live
until risk-managing the short-squeeze tail.

## Squeeze-filter attempt (2026-06-24) — REJECTED by data

Tested the one mechanism-justified DD control: skip events whose coin
outperformed the market by > thr over the L days before entry (crowded-short →
squeeze hypothesis). `build_book(squeeze_lookback=L, squeeze_thr=thr)`.

Result across L∈{10,20}, thr∈{20,30,50}%: **maxDD stays −68% in every variant.**
- L=10: filters ~nothing (10d market-adj run-ups >20% are rare here).
- L=20: removes WINNERS (cum pnl 118%→89%), not the squeezes.

Mechanism rejected: the blow-ups came from coins that did NOT run up beforehand —
pre-entry momentum does not predict the squeeze. Filter left OFF by default.
**Deliberately not tuned further** — searching filter variants until DD drops would
be overfitting (the discipline this whole research line enforces). The fat tail
appears structural to shorting crypto into events; reducing it would need a
different risk architecture (intraday stop-loss), which is itself tunable-prone and
out of scope. Net: token-unlock stays a marginal, orthogonal, NOT-live-ready sleeve.
