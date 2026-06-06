# Sources

## Primary References

1. **Faith, Curtis C. (2007). *Way of the Turtle: The Secret Methods that Turned Ordinary
   People into Legendary Traders*. McGraw-Hill.**
   Original Turtle Trading rules including the two-system (20-day vs 55-day) breakout
   framework, position sizing with ATR-based units, and portfolio-level risk management.

2. **Donchian, Richard D. (1960). Trend-following methods in commodity price analysis.**
   Commodity Research Bureau. Seminal description of the Donchian price channel (highest
   high / lowest low over N periods) as a mechanical trend-entry rule.

## Quantpedia / Academic

3. **Quantpedia: Trend Following with Donchian Channels (QP-91).**
   https://quantpedia.com/strategies/donchian-channel-breakout-trading-rule/
   Summary of the academic evidence for channel-breakout rules, including Fama/Blume
   (1966) and later papers on technical rule profitability.

4. **Lempérière, Y., Deremble, C., Seager, P., Potters, M., & Bouchaud, J.-P. (2014).
   Two Centuries of Trend Following. *Journal of Investment Strategies*, 3(3), 41–61.**
   Demonstrates that trend-following (including channel breakouts) earns a persistent
   risk premium across centuries and asset classes, attributable to behavioral anchoring
   and delayed momentum.

5. **Hurst, B., Ooi, Y. H., & Pedersen, L. H. (2017). A Century of Evidence on
   Trend-Following Investing. *The Journal of Portfolio Management*, 44(1), 15–29.**
   AQR study confirming trend premium across equities, bonds, FX, commodities over 100+
   years. Relevant as a baseline for expected Sharpe of ~0.4 for a trend system.

6. **Baz, J., Granger, N., Harvey, C. R., Le Roux, N., & Rattray, S. (2015). Dissecting
   Investment Strategies in the Cross Section and Time Series. SSRN 2695101.**
   Framework for comparing time-series momentum vs. channel-breakout entry timing.

## Data

- OHLCV data: Hyperliquid exchange, 1-hour bars, 2023-06-01 to 2026-06-01.
  Resampled to 1-day bars using `qutil.load_ohlcv(coin, '1d')`.
- Coins: BTC, ETH, SOL. MATIC excluded (POL rebrand broke the Hyperliquid series).
