# Final Assessment: Crypto Cross-Sectional Momentum

## Target

The research target is ~25% CAGR. This backtest covers 2023-10-31 to 2026-05-12
(~2.5 years).

## Summary of results (5 bps, long-only default K=30 N=3)

| Metric | Long-Only K30-N3 | Long-Short K30-N3 | BTC B&H | EW Basket |
|--------|-------------------|-------------------|---------|-----------|
| CAGR | -10.1% | +8.1% | +39.5% | -0.3% |
| Vol (ann) | 76.9% | 54.9% | 48.1% | 77.4% |
| Sharpe | 0.25 | 0.42 | 0.93 | 0.39 |
| Sortino | 0.37 | 0.53 | 1.41 | 0.58 |
| Max DD | -81.1% | -50.3% | -49.5% | -80.7% |
| Calmar | -0.12 | 0.16 | 0.80 | -0.003 |

**The long-only strategy FAILS the 25% target severely.** It loses money at -10.1%
CAGR while the BTC benchmark compounds at +39.5%. The long-short variant ekes out
+8.1% CAGR but well below target with a Sharpe of 0.42 and a -50% drawdown.

## Yearly breakdown (long-only K30-N3)

| Year | CAGR | Sharpe | Max DD |
|------|------|--------|--------|
| 2023 (partial, 62d) | +1537% (ann.) | 4.66 | -17% |
| 2024 | +25.5% | 0.68 | -54% |
| 2025 | -48.9% | -0.50 | -64% |
| 2026 (partial, 132d) | -56.5% (ann.) | -0.89 | -51% |

The 2023 partial year (Nov-Dec only, 62 trading days) annualizes to a ludicrously
high number due to the crypto bull ramp — it is NOT representative and inflates
full-sample metrics for any strategy that held alts in that period.

## K x N grid (long-only, 5 bps)

Best by Sharpe: K=14, N=4 (Sharpe=0.33, CAGR=-3.9%, MDD=-76.9%). Still negative
CAGR. No grid cell reaches Sharpe > 0.35. Longer lookbacks (60, 90 days) are
dramatically worse (Sharpe < -0.25), suggesting momentum reverts at those scales
in this universe — or that 2025's broad alt drawdown punishes any strategy that
was long top recent performers (who then continued falling).

| K \ N | N=2 | N=3 | N=4 |
|-------|-----|-----|-----|
| K=7  | Sharpe 0.24, CAGR -15.7% | 0.18, -15.9% | 0.29, -7.1% |
| K=14 | 0.19, -17.6% | 0.33, -4.8% | **0.33, -3.9%** |
| K=30 | 0.18, -16.7% | 0.25, -10.1% | 0.23, -10.1% |
| K=60 | -0.36, -43.6% | -0.27, -38.0% | -0.24, -35.6% |
| K=90 | -0.36, -40.8% | -0.29, -36.9% | -0.14, -29.5% |

## Honest verdict

**Cross-sectional momentum does not work on this universe/period. It does not
approach the 25% CAGR target.**

Key reasons:

1. **Crypto assets are highly correlated.** In this 12-coin universe the average
   pairwise correlation is ~0.7–0.8. CS-MOM needs meaningful cross-sectional
   dispersion; when everything moves together (especially in 2025's broad
   alt-bear market), ranking top vs. bottom provides little incremental signal.

2. **2025–2026 was a structural alt-coin bear market.** Every alt-coin in the
   universe fell 32–92% from early 2025 to mid-2026 (BTC -7–15%, ETH -32%,
   SOL -51%, ARB -82%, OP -92%). Any long-only strategy that concentrated in
   the "winners" would pick from a universe of declining assets. Long-short
   fared better by having a short book, but still drew down -50%.

3. **Short lookbacks (K=7, 14) create excessive turnover and pick noise.**
   Long lookbacks (K=60, 90) catch the beginning of the 2024 bull run but then
   hold into the 2025 collapse because the signal turns slowly.

4. **The benchmark is hostile.** BTC returned +39.5% CAGR over this window
   largely because of the 2023–2024 ETF-driven rally. A momentum strategy that
   didn't hold BTC (or held alts over BTC) was structurally short the dominant
   driver of the period.

## Survivorship bias — magnitude and direction

This is the single most important caveat in the results.

**All 12 coins in the universe survived and remained liquid through 2026-06.**
The universe was chosen looking backward from 2026 — this is pure survivorship bias.

In a real deployment:
- Many coins listed in 2022–2023 subsequently became illiquid or delisted.
- A momentum strategy at the time would have included those coins in its universe
  and would have bought recent winners that later cratered.
- Examples of coins that would have appeared: FTX-adjacent tokens, many DeFi
  tokens from the 2021 cycle that never recovered.

**Direction of bias**: entirely upward. The worst-performing, most-failed coins
are excluded from the history. This means our results are already WITH survivorship
bias, and they are still poor (-10% CAGR long-only). The true performance without
survivorship bias would be materially worse, particularly for long-only strategies.

The long-short strategy is somewhat less affected by survivorship because the short
book would have captured some failed tokens. But the long book still benefits from
only having "surviving" winners to choose from.

**Estimated survivorship impact**: Based on the crypto literature (Liu et al. 2022,
Han et al. 2023), survivorship bias typically adds 5–15% annualized return in
crypto backtests. If we subtract 10% for bias, the long-only strategy falls from
-10% to approximately -20% CAGR. This is catastrophic.

## Conclusion

Do not deploy this strategy. The long-only CS-momentum strategy on a concentrated
12-coin universe has negative expected returns even WITH survivorship bias baked in.
The long-short variant (+8% CAGR with -50% DD) barely outpaces cash and would be
wiped out by real-world frictions (funding costs on shorts, borrow costs, liquidity
gaps).

**The strategy might work if:**
- Universe expanded to 50–100 coins with point-in-time data.
- A trend filter is added (only take long positions when coin is above its 200-day MA).
- Combined with time-series momentum rather than pure CS ranking.
- Applied in bull-market regimes only (timing filter).

None of these modifications are validated here.
