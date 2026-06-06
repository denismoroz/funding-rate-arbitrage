# Walk-forward probe — "can we tune trend until it works?"

Direct test of the hypothesis that parameter tuning makes crypto trend reach the target.
Universe BTC/ETH/SOL basket, long/flat, 5 bps. Candidates: MA {10/50,20/100,50/200} + TSMOM {30,60,90}.

## Findings

| Approach | CAGR | Sharpe | MaxDD | Calmar | What it is |
|---|---|---|---|---|---|
| In-sample BEST single param (mom30) | **45.8%** | 1.16 | −37.7% | — | ❌ the fantasy — selecting the full-sample winner |
| **Walk-forward tuned-live** (pick best trailing-Sharpe param every 21d) | **22.6%** | 0.71 | −32.8% | — | the honest "tuning live" number |
| Buy & hold BTC (same window) | 23.0% | 0.67 | — | — | passive benchmark |
| A-priori default ma50_200 | 5.0% | 0.33 | −45.0% | 0.11 | the report's honest default |
| **Parameter ENSEMBLE (avg of all 6)** | **34.3%** | **0.99** | −33.9% | **1.01** | ✅ the legitimate improvement |
| Ensemble @ 10 bps | 33.4% | 0.98 | — | — | cost-insensitive |

## Three conclusions

1. **Tuning to a single number is curve-fitting — proven.** The full-sample champion shows 45.8%;
   picking params walk-forward (the live-replicable version) delivers only **22.6%** — half the
   in-sample number evaporates, and it merely matches passive BTC. Any 45% claim is fiction.

2. **BUT the critique of the a-priori default was fair.** 50/200 SMA (5% CAGR) was a poor horizon
   for crypto's fast cycles. Faster trend (mom30, mom90, ma10/50) is a defensible *a-priori* prior,
   not a fit — and several fast params independently work.

3. **The disciplined way to lift performance is ENSEMBLING, not selection.** Averaging all 6 trend
   params (no peeking, no choosing) gives **34.3% CAGR, Sharpe 0.99, Calmar 1.01, cost-insensitive** —
   clears 25% and beats buy-and-hold on risk-adjusted terms (Calmar 1.01 vs 0.78, MaxDD −34% vs −49%).

## The remaining honesty caveat (does NOT go away with tuning)

Ensemble yearly: **2023 +179%, 2024 +45%, 2025 +4%, 2026 −26%.** The 34% is robust *across
parameters and costs* but still **front-loaded by the 2023 ramp and negative in 2026**. Three years
is one crypto cycle. Tuning fixed the param-selection problem; it cannot fix the one-regime-sample
problem. So: trend (ensembled) **approaches/exceeds 25% defensibly on params**, but its forward
expectation is regime-contingent — treat ~15-25% as the live range, not 34%.

## Where tuning is pointless
Reversal (negative gross even at 0 bps), cross-sectional momentum (negative, structural), and FX
trend (≈0% over 22 years) have **no gross edge to harvest** — no parameter turns a negative gross
signal positive. Tuning only pays where a real edge exists (trend, carry).
