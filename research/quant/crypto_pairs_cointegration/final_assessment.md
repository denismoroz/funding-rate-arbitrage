# Final assessment — Crypto pairs / cointegration stat-arb

**Verdict: REJECT for live (below target, not robust). OOS edge decayed to ~zero in 2025.**

## Result (out-of-sample, post-formation 2024-10 → 2026-05, ~1.53 yr)
| Metric | Value |
|---|---|
| CAGR | **+4.0%** |
| Vol | 17.8% |
| Sharpe | 0.31 |
| Sortino | 0.40 |
| Max drawdown | −15.0% |
| Calmar | 0.27 |
| Exposure | ~99% (always in some pair) |

Yearly OOS: 2024 (tail) +24% Sharpe 1.17 · **2025 −6.7% Sharpe −0.29** · 2026 (partial) +30% Sharpe 1.78.

## Why it fails
- Pairs were **selected on the first 365 days** (Engle-Granger p<0.05) and **held fixed**. The
  cointegration relationships **decayed out-of-sample** — exactly the failure mode the literature
  understates. The reported 16–34% APR / Sharpe 2.5 numbers are typically in-sample or
  re-selected pairs on a favorable window.
- 2025 was a sustained loss: spreads trended (one leg structurally outperformed) rather than
  reverting — cointegration broke without a regime to flag it.
- Market-neutral construction did deliver low correlation to BTC and a contained −15% MDD, but
  the return is not there.

## What might rescue it (not pursued — overfitting surface)
- Walk-forward re-selection of pairs every quarter + Kalman/rolling hedge ratio.
- Half-life-based holding limits and a cointegration-stability filter (re-test p-value live).
- These add parameters and data-mining risk for an uncertain payoff.

## Honesty notes
- Survivorship: universe is large-caps that survived; MATIC excluded (POL rebrand truncated data).
- Costs: 5 bps/side on each leg's turnover (perp taker + slippage). Result is net of these.
- No look-ahead: rolling beta/z shifted; weights execute next bar (qutil convention).
