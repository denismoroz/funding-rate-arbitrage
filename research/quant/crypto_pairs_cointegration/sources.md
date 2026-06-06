# Sources — crypto pairs / cointegration

- Copula-based trading of cointegrated cryptocurrency pairs, Financial Innovation (Springer, 2024):
  https://link.springer.com/article/10.1186/s40854-024-00702-7
- Statistical Arbitrage Using Cointegration, IJSRA (2026): reports BTC-ETH ~16.34% APR, Sharpe 2.45, MDD ~15.7%:
  https://ijsra.net/sites/default/files/fulltext_pdf/IJSRA-2026-0283.pdf
- Pairs Trading in the Cryptocurrency Market (EUR thesis):
  https://thesis.eur.nl/pub/67552/Thesis-Pairs-trading-.pdf
- Engle & Granger (1987) cointegration; statsmodels `coint` (Engle-Granger two-step).

Data: research/data/<COIN>_1h.csv (Hyperliquid 1h, resampled to daily). Universe = large caps
with full history minus MATIC (POL rebrand). In-sample formation = first 365 days; OOS thereafter.
