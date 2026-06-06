# Final Assessment: Crypto Funding Rate Contrarian / Reversal

## Verdict

**There is NO evidence of genuine price-reversal alpha. The hypothesis is rejected.**

This strategy does not achieve the 25% APR target or any positive return target. Every single price-only result across all variants, all parameter combinations, and both universes is negative. This is not a borderline case — the signal actively loses money on price.

---

## Key Numbers (Full Universe, 12 coins, 2023-10-31 to 2026-05-12)

### Variant A: Cross-Sectional, N=3, L=30

| Component | 5 bps | 10 bps |
|-----------|-------|--------|
| **Price-only CAGR** | **-18.2%** | **-42.2%** |
| **Price-only Sharpe** | **-0.15** | **-0.85** |
| Total CAGR | -7.9% | -34.9% |
| Total Sharpe | +0.08 | -0.61 |
| Funding contribution (ann) | +11.8% | +11.8% |

### Variant B: Time-Series, Z=1.5, L=30

| Component | 5 bps | 10 bps |
|-----------|-------|--------|
| **Price-only CAGR** | **-8.2%** | **-10.8%** |
| **Price-only Sharpe** | **-0.39** | **-0.54** |
| Total CAGR | -4.7% | -7.4% |
| Total Sharpe | -0.18 | -0.34 |
| Funding contribution (ann) | +3.7% | +3.7% |

**BTC B&H benchmark:** CAGR +39.5%, Sharpe +0.93  
**BTC correlation:** A_total = -0.002 (near-zero, truly market-neutral), B_total = +0.177

---

## Decomposition Analysis

The split between price-only and funding-component is the central finding:

**Variant A at 5bps:** Price loses -18.2%, funding adds +11.8% → total = -7.9%.  
The funding component is substantial (~65% of price losses recovered) but cannot compensate for the directional price bleed. Even at 5bps costs, funding does not save the strategy.

**Variant B at 5bps:** Price loses -8.2%, funding adds +3.7% → total = -4.7%.  
The funding contribution is small (Variant B spends less time in extreme positions, so it earns less funding carry).

**Interpretation:** The entire positive return component is passive carry (funding received by shorts, paid by longs). The contrarian price prediction — the novel claim — is simply wrong. Prices do NOT systematically revert after funding extremes in this dataset. In fact, they continue in the crowded direction (momentum dominates).

---

## Grid Search: No Hiding Place

Every single cell in the sensitivity grids has negative price-only CAGR and negative price-only Sharpe:

**Variant A grid (N × L, 9 cells):** price Sharpe ranges from -0.91 to +0.20 (single cell N=4, L=60 is marginally positive at 0.20 — this is noise over 925 bars and must not be treated as signal).

**Variant B grid (Z × L, 9 cells):** price Sharpe ranges from -0.57 to +0.01. All negative or flat.

There is no robust parameter combination that generates positive price-only returns.

---

## Yearly Breakdown: Variant A (Price-Only)

| Year | CAGR | Sharpe |
|------|------|--------|
| 2023 | -37.5% | -0.40 |
| 2024 | -14.2% | +0.02 |
| 2025 | -1.9% | +0.17 |
| 2026 | -51.1% | -2.02 |

2024 and 2025 show near-zero price-only Sharpe — close to random. 2023 and especially 2026 are sharply negative. The strategy is not marginally-negative-but-worth-researching; it actively bleeds.

---

## Why the Hypothesis Fails

**Momentum dominates reversal in crypto.** Crypto perpetual markets in 2023-2026 were predominantly momentum-driven during bull phases. High funding went with upward-trending coins (SOL, AVAX, AI coins) and shorting those was consistently wrong. The crowding thesis requires that the crowded position unwinds quickly; instead, the crowd was right for extended periods.

**The reversal signal is too slow.** A 30-day rolling z-score window is too long. By the time funding is +2σ, the momentum trade is already mature and the crowd may stay crowded for weeks more. The theoretical reversion mechanism (funding cost incentivizes fresh shorts) is real but operates on a timescale shorter than the 1-day signal rebalancing.

**Cross-sectional (Variant A) has additional hazard.** Shorting the highest-funding coin and buying the lowest-funding coin is the OPPOSITE of carry-adjusted momentum. In a strong bull market, high-funding coins ARE the outperformers, making Variant A a pure momentum fade — which predictably loses.

---

## Extended Universe (HYPE + ZEC, 188 bars)

Results are even worse: Variant A price-only CAGR = -82.6%, Sharpe = -3.85. The short sample (6 months, Nov 2025 - May 2026, covering a broad crypto sell-off) makes the 188-bar results unreliable for drawing conclusions. Not reported as actionable.

---

## Comparison to 25% APR Target

The strategy does not achieve positive returns at any cost level. The **gap to target is not a few percentage points** — it is the direction that is wrong. Price-only CAGR of -18.2% to -8.2% means:
- To reach +25% price-only, we need a ~30-40 percentage point reversal in direction
- Funding subsidy of +4% to +12% helps only marginally
- Even the "best" total result (+0.08 Sharpe at 5bps) would be noise over 2.5 years and does not justify deployment

---

## Conclusion

**This strategy should NOT be deployed.** The directional hypothesis is empirically false in the sample. Genuine price-reversal alpha from funding z-score is absent. The partial offset from funding carry does not save the strategy and is better captured by a dedicated carry strategy (already live in this repo as CarryMesh, earning ~19-25% real APR).

The literature (MDPI 2025, Coinglass) identifies funding extremes as positioning indicators, but the predictive direction in perpetual crypto markets over 2023-2026 favors continuation (momentum), not reversal. A momentum-on-funding-signal backtest would be the natural next test; it is explicitly outside the scope of this study.
