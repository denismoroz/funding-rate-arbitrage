# Sources

## Academic

1. **"Risk Premia in the Bitcoin Market"** — arXiv:2410.15195 (2024).
   Examines variance risk premium, jump risk premium, and skewness premium in BTC
   options using Deribit data. Finds a significant and persistent VRP on BTC,
   consistent with our backtest finding.

2. **Carr & Wu (2009)** — "Variance Risk Premiums", *Review of Financial Studies*.
   Canonical paper defining VRP as E[RV] - IV for S&P 500; the negative expected
   VRP for equity indices (sellers earn premium) is the benchmark.

## Industry / Exchange Research

3. **Deribit Insights — "Bitcoin Options: Finding Edge in Four Years of Volatility Regimes"**
   https://insights.deribit.com/industry/bitcoin-options-finding-edge-in-four-years-of-volatility-regimes/
   Deribit's own analysis of VRP across BTC option regimes; highlights that
   the premium was highest in 2021 and compressed toward 2023-2024. Consistent
   with our per-year breakdown.

4. **QuantConnect — "Volatility Risk Premium Effect"**
   https://www.quantconnect.com/research/14451/volatility-risk-premium-effect
   Implementation of VRP strategy in equities via SPX options; demonstrates the
   strategy structure (sell 1-month ATM straddle, delta-hedge) that our vol-swap
   proxy approximates.

## Data

5. **Deribit DVOL Index** — https://www.deribit.com/statistics/BTC/volatility-index
   30-day constant-maturity implied vol index, published by Deribit exchange.
   Used as IV proxy. Starts July 2021 for BTC, with a data gap Dec 2022 – Jun 2023.

6. **Yahoo Finance** — BTC-USD and ETH-USD daily close prices.
   Used to compute forward realised volatility.
