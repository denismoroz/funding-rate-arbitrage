# Trend + Carry Blend

Tests whether combining a trend-following ensemble with a delta-neutral funding carry strategy
produces a better risk-adjusted profile than either standalone, exploiting their regime
orthogonality (trend earns in trends, carry earns in chop).

## Files

| File | Description |
|------|-------------|
| `backtest.py` | Reproducible backtest — builds both return series, blends, computes all metrics |
| `metrics.json` | Full metrics for all variants + yearly breakdown + correlation |
| `results.csv` | Daily returns and equity curves for all variants |
| `equity.png` | Equity curves: trend-only, carry-only, best meaningful blend (w_trend=0.25) |
| `strategy_description.md` | Full construction details + regime-orthogonality thesis |
| `final_assessment.md` | Honest verdict: Calmar frontier, regime evidence, 25% CAGR verdict |
| `sources.md` | Internal + external references |

## Quick Results (2023-06-01 to 2026-06-01)

Daily correlation (trend vs carry): **0.163** (low, as hypothesized)

| w_trend | CAGR   | Vol    | Sharpe | MDD    | Calmar |
|---------|--------|--------|--------|--------|--------|
| 1.00    | 34.3%  | 36.3%  | 0.99   | -33.9% |  1.01  |
| 0.75    | 28.5%  | 27.2%  | 1.06   | -25.8% |  1.10  |
| 0.50    | 22.0%  | 18.2%  | 1.18   | -17.1% |  1.29  |
| 0.25    | 14.9%  |  9.2%  | 1.56   |  -8.6% | **1.73** |
| 0.00    |  7.3%  |  0.7%  | 9.95   |  -0.7% | 10.72  |

The Calmar frontier is **monotone** (no interior optimum) because carry's volatility is ~60-100x
smaller than trend's, making risk parity degenerate and Calmar improvement linear.
The w=0.25 blend improves Calmar 71% over trend-only while preserving meaningful CAGR.

See `final_assessment.md` for the full verdict vs the 25% CAGR target.

## Run

```bash
source .venv/bin/activate
python research/quant/crypto_trend_carry_blend/backtest.py
```
