# Final Assessment: Short-Term Reversal / Mean Reversion

**Date:** 2026-06-06  
**Data:** Hyperliquid perp OHLCV, 2023-10-31 – 2026-06-01  
**Universe note:** Panel starts 2023-10-31 (TIA listing constrains the inner join). Only ~2.1 years of data; regime conclusions are tentative.

---

## Results Summary

### Flavor A — Cross-Sectional Reversal (K=5, N=3, 1d bars)

| Cost (bps) | CAGR   | Sharpe | MaxDD  |
|------------|--------|--------|--------|
| 0          | -17.3% | -0.07  | -59.9% |
| 5          | -23.5% | -0.21  | -65.6% |
| 10         | -29.2% | -0.35  | -70.7% |

Yearly breakdown at 5bps:

| Year | CAGR    | Sharpe | MaxDD  | Note                        |
|------|---------|--------|--------|-----------------------------|
| 2023 | +114%   | +1.22  | -31.4% | ONLY 50 bars (7 weeks) — noise, disregard |
| 2024 | -35.9%  | -0.51  | -58.9% | Consistent loser            |
| 2025 | -25.9%  | -0.38  | -47.0% | Consistent loser            |
| 2026 | -7.7%   | -0.07  | -17.2% | Partial year                |

**Best grid cell at 5bps:** K=5, N=4 (Sharpe=-0.01, CAGR=-11.1%) — barely less bad, still negative.

**Correlation with BTC buy-and-hold:** 0.08 — nearly uncorrelated, as expected for a dollar-neutral strategy. But uncorrelated to a losing strategy is still a losing strategy.

### Flavor B — Intraday Z-Score Mean Reversion (W=48, Z=2.0, 1h bars, BTC+ETH)

| Cost (bps) | CAGR   | Sharpe | MaxDD  | Trades/Day |
|------------|--------|--------|--------|------------|
| 0          | -16.5% | -0.33  | -49.8% | ~1.7       |
| 5          | -30.2% | -0.83  | -69.6% | ~1.7       |
| 10         | -41.7% | -1.34  | -81.6% | ~1.7       |

Yearly breakdown at 5bps:

| Year | CAGR    | Sharpe | MaxDD  |
|------|---------|--------|--------|
| 2023 | -33.5%  | -1.40  | -26.0% |
| 2024 | -39.9%  | -1.10  | -50.0% |
| 2025 | -4.7%   | +0.05  | -38.9% |
| 2026 | -49.5%  | -1.70  | -32.7% |

**Best grid cell at 5bps:** W=48, Z=1.5 (Sharpe=-0.62, CAGR=-26.5%) — best of a bad bunch, still clearly negative.

**Correlation with BTC buy-and-hold (daily aggregated):** 0.02 — essentially zero. The strategy is uncorrelated but persistently losing.

### Benchmark: BTC Buy-and-Hold
CAGR=+38.7%, Sharpe=+0.93, MaxDD=-49.5%

---

## Honest Verdict

### Does either strategy survive 5–10bps realistic costs?

**No. Neither flavor is viable at any tested cost level.**

- Flavor A loses money even at 0bps (CAGR=-17.3%). Adding 5bps/side costs worsens it to -23.5%. The reversal signal in crypto is either absent or too weak to overcome the negative carry of shorting up-trending assets and holding down-trending ones in a risk-on 2023-2026 period.

- Flavor B has a similar structural problem: the gross mean-reversion signal is slightly negative at zero cost, suggesting the z-score exits do not generate positive alpha — crypto prices in this sample drift more than they revert at the intraday level. Each round-trip at 5bps further accelerates the drawdown.

### Does either strategy approach the 25% annual return target?

**Not remotely.** The best full-sample gross CAGR across all grid cells:
- Flavor A best at 0bps: K=3, N=3 at -11.4% gross (no cell is positive at 0bps in the corrected run)
- Flavor B best at 0bps: W=48, Z=1.5 at -6.3% gross

Both are negative even before accounting for costs.

### Was there a spurious K=3, N=2 result?

Yes — an early version of the code had a bug: `W.replace(0.0, NaN)` corrupted the weight frame by converting intentional flat positions (coins not in the portfolio that week) to NaN, causing them to forward-fill ghost positions from prior weeks. This generated non-dollar-neutral portfolios and spurious 88% CAGR. The corrected code enforces strict dollar-neutrality at every bar.

### Why is reversal failing here?

1. **Crypto 2023-2026 was a trending bull market.** Short-term reversal works best in mean-reverting, range-bound markets. Shorting BTC/ETH/SOL after a strong week in a multi-year bull run is shorting strength.

2. **Crypto has higher idiosyncratic volatility than equities.** A coin that fell 30% in a week is more likely to continue falling (event-driven, news) than to bounce. The "loser" portfolio accumulates catastrophic names.

3. **Crypto transaction costs are high relative to reversal signal size.** Equity reversal studies typically require 1-5bps costs to be profitable. Crypto taker fees alone are 3.5bps, plus slippage.

4. **Intraday (Flavor B) is even worse.** At 1.7 round trips/day and 5bps/side, the annualized cost drag is ~60%. Even zero-cost gross alpha of -16.5% means the net-of-costs destruction accelerates dramatically.

### Could this work as a portfolio diversifier?

In theory, a strategy uncorrelated (r≈0.08) to BTC could improve a portfolio's Sharpe even at negative standalone returns — IF it were only mildly negative (say, -2 to -5% CAGR standalone). At -23% to -30%, the diversification benefit is not large enough to justify inclusion. The strategy is a significant return drag.

### Conclusion

**Both flavors fail comprehensively.** This is consistent with the short-term reversal literature: the anomaly in equities was largely arbitraged away by the 2000s; in crypto it appears to be either absent or weaker than transaction costs from inception of this dataset. No parameter combination produces positive risk-adjusted returns at realistic 5bps costs. The strategies are not worth further development in this form for this market.

**Recommendation:** Do not trade. Archive for reference. If revisiting, consider: (1) longer reversal lookbacks (1-4 weeks, which approach momentum territory), (2) mean reversion only in explicitly range-bound regimes, or (3) much lower-cost execution venues.
