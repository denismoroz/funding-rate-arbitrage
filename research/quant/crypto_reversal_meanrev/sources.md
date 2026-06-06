# Sources

## Short-Term Reversal: Foundational Literature

**Lehmann, B. N. (1990).** "Fads, Martingales, and Market Efficiency." *Quarterly Journal of Economics*, 105(1), 1–28.
Foundational paper establishing 1-week reversal in US equities. Long prior losers, short prior winners; profitable before transaction costs, mostly attributable to bid-ask bounce and non-synchronous trading.

**Jegadeesh, N. (1990).** "Evidence of Predictable Behavior of Security Returns." *Journal of Finance*, 45(3), 881–898.
Documented short-term reversal and longer-horizon momentum in US equities. Established the empirical tension between 1-month reversal and 3-12 month momentum.

## Crypto-Specific

**Behaviorally-based cross-sectional reversal in crypto assets.** *Journal of Behavioral and Experimental Finance* (2025).
ScienceDirect S154461232501058X. Finds cross-sectional short-term reversal in crypto markets, attributing the effect to investor overreaction and liquidity provision. Notes the anomaly is concentrated in high-attention episodes and diminishes with increased market maturity. Crucially: the paper documents the GROSS reversal signal; after realistic crypto transaction costs (0.1% taker), the net alpha is near zero or negative for the shorter lookback windows.

## Mean Reversion (Intraday)

**Gatev, E., Goetzmann, W. N., & Rouwenhorst, K. G. (2006).** "Pairs Trading: Performance of a Relative-Value Arbitrage Rule." *Review of Financial Studies*, 19(3), 797–827.
Classic pairs-trading/mean-reversion paper. Documents the decay of the edge over time as it becomes more widely known; fee sensitivity central to the analysis.

**Avellaneda, M., & Lee, J.-H. (2010).** "Statistical Arbitrage in the U.S. Equities Market." *Quantitative Finance*, 10(7), 761–782.
z-score mean reversion framework for equity stat-arb. The framework (SMA + z-score entry/exit) is directly adapted in Flavor B.

## Data

All price data: Hyperliquid perpetual OHLCV (1h), 2023-10-31 to 2026-06-01 (inner join over 12-coin universe). 
Note: data start is Oct 2023 due to TIA listing date — the latest-listed coin in the universe constrains the panel start.
