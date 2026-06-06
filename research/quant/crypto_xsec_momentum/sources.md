# Sources

## Primary academic references

1. **Han, Y., Kang, J., & Ryu, D. (2023).** "Time-Series and Cross-Sectional Momentum in
   the Cryptocurrency Market."
   SSRN Working Paper 4675565.
   https://ssrn.com/abstract=4675565
   — Documents both TS-MOM and CS-MOM in crypto; finds CS-MOM significant but weaker
   than in equities, with strong regime dependence (bull vs. bear markets).

2. **Liu, W., Tsyvinski, A., & Wu, X. (2022).** "Common Risk Factors in Cryptocurrency."
   *Journal of Finance*, 77(2), 1133–1177.
   — Establishes size, momentum, and attention as cross-sectional crypto risk factors.
   Momentum measured at 1-week and 1-month horizons.

3. **Cong, L. W., Li, Y., Tang, K., & Yang, Y. (2023).** "A Trend Factor for the Cross
   Section of Cryptocurrency Returns."
   *Journal of Financial and Quantitative Analysis (JFQA)*, Cambridge University Press.
   — Proposes a trend factor combining short-, medium-, and long-horizon signals for
   crypto cross-section; outperforms pure CS-MOM in out-of-sample tests.

4. **Jegadeesh, N., & Titman, S. (1993).** "Returns to Buying Winners and Selling Losers:
   Implications for Stock Market Efficiency."
   *Journal of Finance*, 48(1), 65–91.
   — Foundational equity CS-MOM paper; 6-month lookback, 6-month holding, skip-1-month.

## Data

- Hyperliquid perp OHLCV (1h bars, resampled to 1d) from `research/data/<COIN>_1h.csv`.
- Date range: 2023-10-31 to 2026-05-12 (limited by TIA listing date inner-joining).
- Costs: Hyperliquid taker fee 3.5 bps, slippage 1.5 bps, total 5.0 bps per side.
