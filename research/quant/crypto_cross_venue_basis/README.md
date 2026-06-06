# Cross-Venue Funding Basis Backtest

**Strategy class:** Pure delta-neutral perp-perp cross-venue funding arbitrage.
SHORT high-funding venue + LONG low-funding venue, same coin, equal notional.
No spot inventory. No price exposure. Income = funding spread.

**Universe:** ARB, AVAX, BTC, DOGE, ETH, LINK, MATIC*, OP, SOL  
**Venues:** Hyperliquid (HL), Binance, Bybit  
**Period:** 2023-06 → 2026-05 (primary); Drift secondary ends 2025-01-08

*MATIC data truncated at 2024-09-10 (HL perp renamed/delisted, zeroes out post-date)

## Files

| File | Description |
|---|---|
| `backtest.py` | Full backtest: spread characterization, 3 variants, drift secondary, correlations |
| `results.csv` | Daily per-coin and portfolio net returns |
| `metrics.json` | All metrics, spread stats, correlations, yearly breakdown |
| `equity.png` | Equity curves + drawdown chart |
| `strategy_description.md` | Detailed construction, capital/cost model, cadence alignment |
| `final_assessment.md` | **Honest verdict: real but thin, correlated to carry, not additive** |
| `sources.md` | Academic and industry citations |

## Quick Results

| Variant | CAGR | Sharpe | MDD | corr(carry) |
|---|---|---|---|---|
| Static HL-Binance | 2.9% | 5.95 | -3.2% | 0.70 |
| Static HL-Bybit | 2.5% | 5.01 | -3.4% | 0.66 |
| Dynamic 3% thresh | -2.2% | -3.76 | -12.5% | 0.51 |

Dynamic variant loses money due to Binance/Bybit venue-flip churn (~40%/day × 4 bps = ~5.8% drag/yr).

## Run

```bash
source .venv/bin/activate
python research/quant/crypto_cross_venue_basis/backtest.py
```
