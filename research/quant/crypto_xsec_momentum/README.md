# Crypto Cross-Sectional Momentum

**Status: NEGATIVE RESULT — strategy does not reach 25% CAGR target.**

## What this is

A weekly-rebalanced cross-sectional momentum strategy on 12 liquid crypto assets.
Each week, coins are ranked by their trailing-K-day return. The long-only variant
holds the top-N coins equally; the long-short variant also shorts the bottom-N.

## Results summary (Oct 2023 – May 2026, 5 bps costs)

| Strategy | CAGR | Sharpe | Sortino | Max DD | Calmar |
|----------|------|--------|---------|--------|--------|
| Long-only K=30 N=3 | -10.1% | 0.25 | 0.37 | -81.1% | -0.12 |
| Long-short K=30 N=3 | +8.1% | 0.42 | 0.53 | -50.3% | 0.16 |
| BTC buy-and-hold | +39.5% | 0.93 | 1.41 | -49.5% | 0.80 |
| EW basket | -0.3% | 0.39 | 0.58 | -80.7% | -0.003 |

At 10 bps: long-only -11.7% CAGR / Sharpe 0.22; long-short +4.1% CAGR / Sharpe 0.35.

Best grid cell (long-only): K=14, N=4 → CAGR -3.9%, Sharpe 0.33 — still negative.

## Files

- `backtest.py` — complete runnable backtest
- `results.csv` — daily equity curves for all scenarios
- `trades.csv` — weekly holdings and turnover log
- `metrics.json` — full metrics + yearly breakdown + K×N grid
- `equity.png` — long-only equity vs BTC
- `equity_ls.png` — long-short equity vs BTC
- `strategy_description.md` — exact rules and pseudocode
- `sources.md` — academic citations
- `final_assessment.md` — honest verdict and survivorship discussion

## Survivorship warning

Universe chosen with hindsight (all coins liquid in 2026). This biases results
upward by an estimated 5–15% CAGR. True performance would be materially worse.

## How to run

```bash
source .venv/bin/activate
python research/quant/crypto_xsec_momentum/backtest.py
```
