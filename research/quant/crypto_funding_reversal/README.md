# Crypto Funding Rate Contrarian / Reversal

**Status: HYPOTHESIS REJECTED — no genuine price-reversal alpha found**

## One-Line Summary

Shorting high-funding coins and buying low-funding coins (contrarian on funding z-score) produces negative price-only returns across all 18 parameter combinations tested. The modest funding carry received on short legs partially offsets the losses but does not make the strategy viable.

## Files

| File | Description |
|------|-------------|
| `backtest.py` | Full backtest: data loading, signal, decomposition, grids, outputs |
| `strategy_description.md` | Rules, pseudocode, decomposition logic, no-look-ahead proof |
| `sources.md` | Literature references and data sources |
| `final_assessment.md` | Honest verdict with all numbers, explanation of failure, 25% target gap |
| `metrics.json` | Full metrics: price-only + total + funding component + grids (full universe) |
| `metrics_extended.json` | Same for extended universe (HYPE+ZEC, 188 bars) |
| `results.csv` | Daily returns for default Variant A (price-only + total columns) |
| `trades.csv` | Daily weight matrix for default Variant A cross-sectional |
| `equity.png` | Equity curves: A price-only vs A total vs B total vs BTC B&H |
| `equity_extended.png` | Same for extended universe |

## Quick Results (Full Universe, Default Params, 5bps)

### Variant A — Cross-Sectional (N=3, L=30, market-neutral)
- **Price-only:** CAGR = **-18.2%**, Sharpe = **-0.15**
- **Total (price + funding):** CAGR = **-7.9%**, Sharpe = **+0.08**
- Funding contribution: ~+12% ann (carry received on short positions)

### Variant B — Time-Series (Z=1.5, L=30)
- **Price-only:** CAGR = **-8.2%**, Sharpe = **-0.39**
- **Total:** CAGR = **-4.7%**, Sharpe = **-0.18**
- Funding contribution: ~+4% ann

### BTC Buy & Hold Benchmark
- CAGR = **+39.5%**, Sharpe = **+0.93**

### BTC Correlation
- A total: **-0.002** (near-zero, truly market-neutral)
- B total: **+0.177** (slight positive correlation)

## How to Run

```bash
cd /path/to/funding-rate-arbitrage
source .venv/bin/activate
python research/quant/crypto_funding_reversal/backtest.py
```

Runtime: ~30 seconds. Outputs: metrics*.json, results*.csv, trades*.csv, equity*.png.

## Key Insight: Decomposition Logic

The central test is whether **price-only PnL is positive**. Each variant runs the backtest twice:
1. `funding=None` → pure price direction PnL
2. `funding=panel` → price + carry

Price-only is universally negative. The thesis that "high funding predicts price fall" is empirically false in HL data 2023-2026. Momentum dominates reversal: high-funding coins are typically the outperformers, not the ones to short.
