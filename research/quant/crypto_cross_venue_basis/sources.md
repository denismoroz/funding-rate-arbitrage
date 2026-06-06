# Sources

## Academic / Industry

1. **Hedge Fund Journal — Liquibit: Market-Neutral Crypto Strategy**
   https://thehedgefundjournal.com/liquibit-market-neutral-crypto-strategy-traditional-trading/
   Documents a live market-neutral cross-venue crypto funding arbitrage fund. Confirms that
   cross-venue funding rate differentials are a real, institutionally-exploited strategy class.
   Relevant for validating the strategy concept and highlighting capacity/operational constraints.

2. **MDPI Mathematics — Two-Tiered Funding Markets in Crypto Perpetuals**
   https://www.mdpi.com/2227-7390/14/2/346
   Academic analysis of the structural heterogeneity in funding rates across centralized
   perpetuals exchanges. Provides theoretical grounding for why HL vs Binance/Bybit spreads
   persist: different liquidity pools, user bases, and open-interest composition create
   persistent but volatile rate differentials.

## Data Sources

- **Hyperliquid (HL):** `research/data/<COIN>.csv` — hourly funding rate stamps, 2023-06 to 2026-05
- **Binance:** `research/data_binance/<COIN>.csv` — 8-hourly funding rate stamps, 2023-06 to 2026-05
- **Bybit:** `research/data_bybit/<COIN>.csv` — 8-hourly funding rate stamps, 2023-06 to 2026-05
- **Drift:** `research/data_drift/<COIN>.csv` — hourly, 2023-06 to 2025-01-08 (API decommissioned)

## Existing Benchmark

- Single-venue HL carry returns: `research/quant/crypto_funding_carry/results_funding_plus_staking.csv`
  Used for correlation analysis. See `research/quant/crypto_funding_carry/backtest.py` for methodology.
