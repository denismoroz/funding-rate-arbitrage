# On-chain Fundamental Momentum — FINDINGS

Hypothesis (PLAN.md): cross-sectional **fee-growth** predicts token returns — long
tokens whose on-chain fees/revenue are growing, short those shrinking. Candidate
**third orthogonal factor** (input = protocol economics, not price/funding).
Validated through `research/validation_harness` (CPCV + DSR + PBO) + orthogonality gate.

**Verdict: NO-GO** — but an *informative* NO-GO (see "Why this matters").

## Data
DefiLlama daily fees (free), 18 coins in two groups (within-group z-scored):
- DeFi app-revenue (10): AAVE UNI JUP ENA JTO CRV LINK EIGEN PENDLE ZRO
- Chain gas-fees (8): ETH SOL TRX BNB ARB AVAX SUI INJ

Signal = ensemble log fee-growth over 30/60/90d, z-scored within group, long/short
tercile, weekly rebal, 4.4 bps/leg. Panel 2023-07 → 2026-06 (~1084 trading days),
per-date cross-section 15 (2023) → 18 (2025+).

## Self-tests (selftest.py) — all pass, and they EXERCISE the real PnL path
- **Cheat** (feed fwd_ret as signal) → Sharpe +58 ⇒ weight[t] earns fwd_ret[t] (alignment correct).
- **No-look-ahead** (shift signal to future fees) → PnL changes 124% ⇒ signal is causal.
- **Deterministic** hand-check of growth+zscore pipeline.

## Verdict table

| Metric | Value | Threshold | Gate |
|---|---|---|---|
| DSR | **0.690** | > 0.95 | ❌ |
| PBO | 0.695 | < 0.20 | ❌ (see caveat) |
| per-period Sharpe (sr̂) | +0.0295/day (≈ 0.47 ann) | — | — |
| PSR vs 0 | 0.836 | — | real positive drift (not noise) |
| OOS median Sharpe (CPCV, hourly-scale*) | 2.82 | — | inflated artifact, ignore absolute |
| corr → XSMOM momentum | **+0.14** | < 0.30 | ✅ |
| corr → FRAB carry | +0.01 | < 0.30 | ✅ |
| corr → BTC buy&hold | −0.00 | < 0.30 | ✅ |

\*harness annualizes daily as if hourly → use DSR/sign, not absolute OOS Sharpe.

## Why it fails — and the two real reasons

1. **Under-powered (DSR 0.69 < 0.95).** sr̂ is a genuine positive drift (PSR-vs-0 = 0.84,
   not 0.5 → not pure noise), but on ~3 yr / 18 coins it does not survive multi-test
   deflation. Exactly the pre-registered null (c): "~2 yr full cross-section → too
   little power." **PBO 0.695 caveat:** the menu is the 4 lookback variants
   (growth30/60/90 + ensemble) — near-identical twins, so PBO sits high *by construction*
   (same twin-inflation shown for K_rank in signal_improve). Read it as "can't pick the
   lookback," not "overfit machine."

2. **The edge is DECAYING / regime-dependent (reviewer addition, not in the harness run).**
   Tercile Sharpe over time: **+1.40 (2023-24) → +0.27 (24-25) → −0.64 (25-26)**;
   half-split **h1 +1.16 / h2 −0.47**. Worse: the *strong* period is the EARLY, THIN
   cross-section (~15 coins); as the panel filled to 18 the edge vanished and turned
   negative. So the +0.47 full-period Sharpe is front-loaded and fading — consistent with
   the known pattern that crypto cross-sectional alphas decay. This makes the NO-GO firmer
   than DSR alone: the signal may already be arbitraged/structural-to-the-early-regime.

## Why this matters (the informative part)

The pre-registered null (b) — "fee-growth is just price-momentum in disguise" — is
**rejected**: corr to XSMOM = +0.14 (< 0.30), and ~0 to carry and BTC. Confirmed by an
independent rebuild (corr +0.16). So on-chain fee-growth is a **genuinely orthogonal
information axis** — unlike trend / cross-exchange-spread / reversal, which all collapsed
into the existing carry/momentum factors. **A NEW data type (on-chain) DID produce a
non-redundant signal** — which validates the strategic thesis that the binding constraint
is the *narrowness of the information set* (price + funding → only carry + momentum), not
"all axes taken." The limiter here is data **power + edge decay**, not redundancy.

## Caveats
- Survivorship: the 18 coins are alive today (DefiLlama snapshot); dead protocols excluded.
- DefiLlama point-in-time: fees can be restated / protocols added retroactively — mild
  forward-bias not fully removable.
- Mixed economic objects (app revenue vs chain gas) handled by within-group z-score.
- No crash in sample (2023-26 calm/bull) — tail behaviour untested.

## Decision
**Do not build live.** The hypothesis is *directionally* validated as an orthogonal axis,
but this specific signal is under-powered and decaying. Revisit only if: DefiLlama history
extends (5 yr+), the cross-section widens (≥25 fee-bearing coins), AND the recent-period
sign recovers. The durable takeaway is the **method**: expanding to a new data type
(on-chain) is the path that actually yields orthogonality — keep mining new *information
types*, not new signals on price+funding.

## Reproduce
```bash
.venv/bin/python research/onchain_fundamental/selftest.py
.venv/bin/python research/onchain_fundamental/run_onchain.py   # → run_onchain.json
```
