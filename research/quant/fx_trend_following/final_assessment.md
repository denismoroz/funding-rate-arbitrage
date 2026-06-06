# Final assessment — FX trend following (G10 daily)

**Verdict: REJECT. No edge on G10 majors — CAGR ≈ 0% full sample, NEGATIVE in the recent decade.**

## Results (7 G10 pairs, daily, 2004-06 → 2026-06; blended 3/6/12-mo TSMOM, vol-targeted)
| Window | CAGR | Vol | Sharpe | Sortino | MaxDD | Calmar |
|---|---|---|---|---|---|---|
| Full @1bp | −0.31% | 7.3% | −0.01 | −0.01 | −22.5% | −0.01 |
| Full @2bp | −0.73% | 7.3% | −0.06 | −0.09 | −25.5% | −0.03 |
| **2015+ @1bp** | **−2.46%** | 7.4% | **−0.30** | −0.43 | −22.5% | −0.11 |

Yearly: the only meaningfully positive years are **crisis/trend years** — 2007-08 (GFC),
2014 (USD rally), 2020 (COVID), 2022 (hiking cycle). The long stretches 2010-2013, 2016-2019,
and 2023-2026 are flat-to-negative. The strategy is now in a multi-year drawdown.

## Why it fails
- Classic FX trend on **G10 majors only** has decayed badly post-2008 — widely documented.
  Central-bank regime suppression of FX vol (2010s) and range-bound majors kill the signal.
- Real CTA trend products that still work trade **50+ markets** (commodities, rates, equity
  indices, EM FX) with risk parity and longer horizons — diversification across many trending
  markets is the actual edge, not trend on 7 correlated currency pairs.
- Costs are NOT the problem here (1 vs 2 bps barely matters) — there is simply no gross edge.

## Honesty notes
- Data: yfinance daily spot, 2004-2026 (~22 yr) — a genuinely long, multi-regime sample, which
  makes the negative result credible rather than a small-sample artifact.
- No look-ahead (qutil next-bar execution; signals from trailing windows).
- Scope limitation: this tests the *FX-only G10 trend* hypothesis. A diversified multi-asset
  managed-futures program is a different (data-heavier) project and is out of scope here.

## Takeaway
The one FX representative in the study **does not work** in its tractable single-market form.
Crypto trend (research/quant/crypto_trend_tsmom) is the better home for the trend premium.
