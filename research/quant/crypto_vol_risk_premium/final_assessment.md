# Final Assessment: Crypto VRP as an Additive Edge

## The Core Question

Does short-vol on BTC/ETH options generate **positive net return** after realistic costs,
and does it **expand the efficient frontier** when added to the existing carry+trend book?

## Verdict: Positive Edge, NOT Additive in Practice

### 1. Is the VRP Real and Positive Net of 2-Vol-Pt Costs?

**BTC: YES. ETH: BARELY.**

BTC short-vol delivers 18.6% CAGR / Sharpe 1.19 / Calmar 1.66 at the 2-vol-pt cost
assumption (0.25× leverage, 15% target vol). Even at 4-vol-pt costs (pessimistic),
CAGR is 12.1% / Calmar 1.0. The premium is positive and significant in 4 of 5 full
calendar years (2021–2025).

ETH short-vol delivers only 3.4% CAGR at 2-vol-pt costs. ETH implied vol has
structurally exceeded realised vol less reliably than BTC, and 2025 was actively
loss-making (-16.1% annual return). ETH VRP does not clear the 25% target on any
cost scenario.

**BTC VRP clears profitability. ETH does not.**

### 2. Does the Premium Survive 2022 Tails?

**PARTIALLY, with a major caveat.**

2022 was actually the *third worst* year in this backtest despite being the year of
LUNA collapse (May 2022) and FTX bankruptcy (November 2022). The BTC strategy earned
+26% CAGR in 2022 with only -6% max drawdown. Why?

The crucial mechanics: when a crash happens **mid-tranche**, the short-vol position
loses because IV at entry was too low relative to actual realised vol. But when IV
is already **elevated at entry** (post-crash), the position often wins because the
market calms down.

The Oct 13, 2022 tranche (entered before FTX was known) took a -4.6% loss as RV
spiked to 82% vs the 66% IV at entry. But most 2022 tranches were profitable.

**The critical caveat**: the November 12, 2022 entry (right after FTX) is EXCLUDED
from the backtest due to a 6-month Deribit DVOL data gap. This tranche would
theoretically have been profitable (IV=103%, next-30-day RV=32%), but the uncertainty
of that period is not fully reflected. **2022 tail risk is understated.**

The true worst-case scenario for short-vol in crypto is a *sustained* vol spike that
persists throughout a 30-day holding window — like a slow-motion crash that keeps
realising higher vol than IV predicted. This is fully possible and not captured by
the limited 2021-2026 sample.

### 3. Is VRP Uncorrelated to Carry and Trend?

**YES — essentially zero correlation.**

Monthly return correlation over the 37-month overlap period (Jun 2023 – Jun 2026):
- VRP vs Carry: **+0.001** (effectively zero)
- VRP vs Trend: **+0.032** (effectively zero)
- Carry vs Trend: +0.701 (high — they share the same directional risk)

This is the theoretically expected result. Vol selling is a pure **vega/theta** bet,
while carry is a funding rate bet and trend is a price momentum bet. They are driven
by different market states.

This low correlation is genuine and robust — it's not just noise. The VRP strategy
makes money when vol is overestimated (calm markets), while trend makes money during
strong directional moves (high vol). They are structurally negatively correlated in
regimes, though the measured correlation is nearly zero rather than negative.

### 4. Does the 3-Way Blend Beat the 2-Way Blend on Calmar?

**NO.**

| Blend | CAGR | Sharpe | Calmar |
|-------|------|--------|--------|
| Carry + Trend (2-way) | 7.2% | 6.6 | **7.9** |
| Carry + Trend + VRP (3-way) | 7.3% | 5.9 | **5.9** |

Adding VRP **reduces** Calmar by 2.1 and Sharpe by 0.7, despite the near-zero correlation.

**Why?** The inverse-vol weighting gives VRP only a 5.2% weight (because carry/trend
have very low vol ~1-2%), yet VRP's fat tail still contributes meaningful drawdown.
The carry+trend blend already has a -0.9% max drawdown (an extremely clean equity curve);
adding any vol-selling creates occasional larger drops. For an already near-optimal blend,
the diversification benefit of zero-correlated VRP is outweighed by VRP's fat tail.

This would change if:
(a) VRP was sized even smaller (say 1-2% weight), or
(b) VRP's tail was hedged (long far-OTM options as disaster insurance), or
(c) The carry+trend blend had a worse Calmar to begin with (more room to improve).

### 5. Does VRP Clear the 25% Target?

**BTC VRP alone: NO (18.6% CAGR at current sizing).**

To reach 25% would require ~1.35× the current VEGA_SCALE (≈ 0.34× leverage).
At that size, vol would be ~20% and Calmar would be slightly lower (~1.4). This
is still sub-1× leverage and achievable, but:
- The 25% target is defined for the full book, not a standalone strategy
- Adding VRP at 25% CAGR sizing to the blend would further hurt the blend's Calmar
- Real options execution (vs vol-swap proxy) will likely deliver lower net return

### Honest Verdict

**VRP is a real premium, genuine alpha, with nearly zero correlation to carry+trend.**

However, it is **not a clean additive edge** to this specific portfolio at current
scales. The carry+trend blend already has a Calmar of 7.9 — a very high bar.
VRP's fat tail properties (occasional 10-15% single-month losses) hurt the risk-
adjusted performance of a portfolio that otherwise rarely drawdowns more than 1%.

**Recommended use**: VRP is best deployed as a **small, capped allocation** (3-5%
of capital) with a mandatory tail hedge (long 1-2% OTM put options that activate in
a vol spike). This removes the fat left tail while preserving the theta income. The
net result would likely be a modest CAGR contribution (+1-2% per year) with minimal
drag on Calmar.

**Do not run naked short-vol at scale.** A single extreme event (BTC -50% in a month,
which has happened twice in 2021-2022) could lose 30-50% of the VRP allocation in 30
days. Position sizing and tail hedging are non-negotiable preconditions for live deployment.

## Summary Table

| Question | Answer |
|----------|--------|
| BTC VRP positive net of 2-vpt costs? | **YES** — 18.6% CAGR, Sharpe 1.19 |
| ETH VRP positive net of 2-vpt costs? | **BARELY** — 3.4% CAGR, not reliable |
| Survives 2022 tails (with caveat)? | **YES** (but tail risk understated) |
| Uncorrelated to carry+trend? | **YES** — corr < 0.05 to both |
| 3-way blend beats 2-way on Calmar? | **NO** — Calmar drops 7.9 → 5.9 |
| Clears 25% CAGR target? | **NO** — 18.6% at 0.25× leverage |
| Additive edge to existing book? | **NOT in practice** at current scale |
| Safe to deploy live at full size? | **NO** — tail hedge required |
