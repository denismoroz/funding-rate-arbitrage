# Sources

## Primary Academic References

1. **Moskowitz, T., Ooi, Y.H., and Pedersen, L.H. (2012)**
   "Time Series Momentum." *Journal of Financial Economics*, 104(2), 228-250.
   The foundational paper establishing that trends in past returns predict future returns
   across asset classes. Documents TSMOM strategy: long when trailing-12-month return > 0,
   else short, with vol-scaling.

2. **Han, Y., Kang, J., and Ryu, D. (2023/2024)**
   "Momentum in Cryptocurrency Markets."
   SSRN Working Paper #4675565. https://ssrn.com/abstract=4675565
   Documents time-series and cross-sectional momentum in crypto, including Bitcoin and
   altcoins. Finds TSMOM effects are economically significant even after transaction costs.

3. **Grayscale Research (2024)**
   "The Trend is Your Friend: Managing Bitcoin's Volatility with Momentum Signals."
   https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals
   Practitioner paper applying SMA crossover and TSMOM signals specifically to Bitcoin.
   Shows that trend signals can reduce maximum drawdown significantly vs buy-and-hold,
   at the cost of some CAGR.

## Additional Background

4. **Lempérière, Y. et al. (2014)**
   "Two centuries of trend following." *Journal of Investment Strategies*, 3(3), 41-61.
   Cross-asset evidence for trend persistence; establishes robustness across regimes.

5. **Hurst, B., Ooi, Y.H., Pedersen, L.H. (2017)**
   "A century of evidence on trend-following investing."
   AQR Capital Management white paper.
   Demonstrates trend following works across equities, bonds, commodities, FX —
   provides context for why crypto (high volatility, structural momentum) might be
   particularly amenable to trend strategies.

## Data Source

- OHLCV data: Hyperliquid perpetuals exchange, 1-hour bars aggregated to daily.
  Files: `research/data/{COIN}_1h.csv` covering 2023-06-01..2026-06-01.
- No external pricing sources used. Survivorship caveat: universe is limited to coins
  with HL listing history from 2023-06-01 (BTC, ETH, SOL).
