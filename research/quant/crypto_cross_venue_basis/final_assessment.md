# Final Assessment: Cross-Venue Funding Basis

## TL;DR

**The cross-venue basis (HL vs Binance/Bybit) is structurally real but financially thin.
Net of 2x capital and realistic costs, the best static variant delivers ~2.9% CAGR
(Sharpe 5.95, MDD -3.2%).  This is ~7-10× smaller than the 25% target and is
highly correlated to the existing single-venue HL carry book (r ≈ 0.70).
It is NOT an additive, independent edge at meaningful size.**

---

## Spread Stats (HL vs Binance, pooled, 2023-06 → 2026-05)

| Metric | HL-Binance | HL-Bybit |
|---|---|---|
| Mean spread (annualized) | **+5.8%** | **+5.0%** |
| Median spread | +3.6% | +3.1% |
| Std dev | 28.5% | 28.5% |
| % days HL higher | **66%** | **64%** |
| Lag-1 autocorrelation | 0.69 | 0.66 |
| Half-life | ~1.9 days | ~1.7 days |

**Interpretation:** The spread is structurally one-sided (HL higher ~65% of days) but
noisy (std > mean by 5×). Persistence is short (~2 days half-life), meaning the spread
mean-reverts quickly. The theoretical edge is real; the noise is large.

### Per-coin HL-Binance mean spread:
BTC: +7.9% · ETH: +8.6% · SOL: +7.4% · DOGE: +9.6% · LINK: +7.1%
AVAX: +4.1% · ARB: +2.8% · OP: +1.5% · MATIC: +0.8% (truncated Sep 2024)

---

## Performance Summary (net of costs, return on 2× capital)

| Variant | CAGR | Sharpe | MDD | Calmar | Deployed | corr(BTC) | corr(carry) |
|---|---|---|---|---|---|---|---|
| Static HL-Binance (V1) | **2.9%** | **5.95** | -3.2% | 0.91 | 100% | 0.01 | **0.70** |
| Static HL-Bybit (V2) | **2.5%** | **5.01** | -3.4% | 0.72 | 100% | 0.03 | **0.66** |
| Dynamic 3% thresh (V3) | **-2.2%** | -3.76 | -12.5% | -0.18 | 84% | 0.04 | 0.51 |
| Dynamic 0% thresh | 0.2% | 0.45 | -6.9% | 0.04 | 96% | 0.04 | 0.55 |
| Dynamic 6% thresh | -4.0% | -6.42 | -16.7% | -0.24 | 67% | — | — |
| Dynamic 10% thresh | -3.0% | -5.28 | -13.6% | -0.22 | 47% | — | — |

### Yearly trend (V1: Static HL-Binance):
| Year | CAGR | Sharpe | Comment |
|---|---|---|---|
| 2023 | 0.3% | 0.37 | Many coins had negative spreads early (AVAX, LINK, OP) |
| 2024 | **6.1%** | 14.44 | Bull market: HL perps premium spiked, best period |
| 2025 | **2.1%** | 12.18 | Steady but compressing |
| 2026 | 0.6% | 6.18 | Jan-May YTD; spread continues to narrow |

**Trend: compressing.** The spread peaked in 2024 and has been trending narrower as
arb capital flows in and HL's user base matures.

---

## Why the Dynamic Variant Loses Money

The dynamic variant attempts to pick the best low venue (Binance vs Bybit) each day.
The problem: Binance and Bybit swap back and forth as the "cheapest" venue with ~35-45%
daily flip frequency.  Each flip costs 4 bps on capital.  The marginal gain from picking
the best low venue over the other is only ~3.5-4.6% annualized — but with 40% flip rate
the drag is ~0.4 × 4 bps/day × 365 = **5.8% annualized in flip costs alone**, far
exceeding the marginal gain.

Dynamic selection **destroys value** vs the simpler static HL-Binance pair.

---

## Correlation Analysis

**corr(static V1, BTC daily price return) = +0.013** — essentially zero.  The strategy
is genuinely price-agnostic.

**corr(static V1, HL single-venue carry) = +0.70** — strongly correlated.  Both
strategies are driven by the same underlying factor: HL funding rate level.

- When HL funding is high → both the single-venue carry and the cross-venue spread are wide.
- When HL funding drops → both suffer simultaneously.

**This is NOT an additive, uncorrelated edge.** The cross-venue basis is a *subset*
of the single-venue HL carry signal. Running both books together concentrates HL-funding-
regime exposure, not diversifies it.

---

## Drift Secondary

Drift had **higher** funding than HL for 7/9 coins in the 2023-2025 sample (negative
HL-Drift spread).  ETH was the only mildly positive case (+1.0%).  Going short HL /
long Drift would have **lost money** (-8.5% CAGR).  Drift REST API was
decommissioned 2026-04-01; Drift data ends 2025-01-08.  **HL-Drift is not a viable
pair and should not be pursued.**

---

## Honest Verdict

### Is the spread real and structural?
**Yes** — HL consistently charges higher funding than Binance/Bybit.  The edge is
structural (driven by HL's smaller, more retail-biased open interest) and persistent at
the annual timescale.  

### Is it tradeable at profit?
**Barely, for the static pair.**  2.9% CAGR on 2× capital = **1.45% APY on
gross notional**.  Practical execution degrades this further:
- **Latency** — you can't capture funding at the exact stamp moment; realized is lower.
- **Inter-venue transfer / withdrawal risk** — moving USDC between HL and Binance takes
  minutes to hours; during that window one leg is naked.
- **Independent liquidation** — each venue liquidates independently; a spike on one side
  can liquidate the short leg before you can close the long.
- **Capacity** — the strategy is limited to market-depth-appropriate sizes; at $10k/coin
  the 4-bps fill assumption is borderline; at $100k it degrades.
- **Data timing** — HL is hourly (precise), Binance/Bybit are every 8h (coarser);
  intra-day HL spikes that boost the theoretical spread cannot be fully captured.

### Is it additive to the existing HL carry book?
**No.**  Correlation = 0.70.  Both books benefit from the same "HL has high funding"
factor.  Adding this overlay amplifies HL-regime risk without true diversification.

### vs 25% target?
**2.9% CAGR is 12% of the 25% target.** Even before practical execution degradation,
the cross-venue basis alone cannot contribute meaningfully.  As a *complement* to
single-venue carry it adds nothing incremental (it IS a carry-like exposure, just with
a lower gross yield due to the 2× capital denominator and the cost of maintaining
positions on two venues simultaneously).

### Bottom line
The HL-Binance cross-venue basis is academically interesting, measurably real, and
operationally complex for a net CAGR that compresses over time.  **It is not worth
pursuing as an independent book** given:
(a) thin net return (2.9%), (b) high operational complexity (two venues, margin on both),
(c) 0.70 correlation to existing carry (no diversification), and
(d) confirmed YoY compression (2024: 6.1% → 2026 run-rate: 0.6%).
