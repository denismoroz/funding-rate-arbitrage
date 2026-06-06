# Crypto Trend / Time-Series Momentum Backtest

**Strategy**: Long/flat trend-following on BTC, ETH, SOL using SMA crossover and
time-series momentum signals, with a volatility-targeting overlay.

**Period**: 2023-06-01 to 2026-06-01 (≈3 years, daily bars)

**Benchmark**: Buy & Hold BTC — CAGR 38.7%, Sharpe 0.93, MDD -49.5%

---

## Default Config Results (50/200 SMA, vol-targeted, 5 bps)

| Metric | Value |
|---|---|
| CAGR | 9.2% |
| Annualised Vol | 27.1% |
| Sharpe | 0.46 |
| Sortino | 0.49 |
| Max Drawdown | -27.7% |
| Calmar | 0.33 |
| Exposure | 56.5% |
| Entry trades (all coins) | 9 |
| Years | 3.01 |

At 10 bps: CAGR 8.9%, Sharpe 0.45, MDD -28.1% (cost-insensitive: low turnover).

Long/Short variant (same signal): CAGR -11.9%, Sharpe -0.17, MDD -59.6% — shorting
crypto in a net-bullish 3-year window is structurally costly.

## Yearly Breakdown (default)

| Year | CAGR | Sharpe | Max DD |
|---|---|---|---|
| 2023 | 12.5% | 1.54 | -2.7% |
| 2024 | 46.2% | 1.25 | -22.1% |
| 2025 | -17.0% | -0.46 | -26.7% |
| 2026 | 0.0% | — | 0.0% (all signals flat) |

## Sensitivity Grid (no vol-target, 5 bps, long/flat)

| Signal | Params | CAGR | Sharpe | MDD | Calmar |
|---|---|---|---|---|---|
| MA | sma10/50 | 37.8% | 1.02 | -37.6% | 1.01 |
| MA | sma20/100 | 36.0% | 0.98 | -43.7% | 0.82 |
| **MA** | **sma50/200** | **5.0%** | **0.33** | **-45.0%** | **0.11** |
| TSMOM | mom30d | 45.8% | 1.16 | -37.7% | 1.21 |
| TSMOM | mom60d | 29.8% | 0.86 | -44.3% | 0.67 |
| TSMOM | mom90d | **44.8%** | **1.10** | **-31.1%** | **1.44** |

Bold row = default config (MA sma50/200). TSMOM 90d is the grid champion.

## Files

| File | Description |
|---|---|
| `backtest.py` | Main backtest code |
| `results.csv` | Daily equity curve and returns (default config) |
| `trades.csv` | Entry/exit events for default signal |
| `metrics.json` | Full metrics: default, grid, yearly, cost sensitivity |
| `equity.png` | Equity curve vs buy&hold BTC |
| `strategy_description.md` | Full rules + pseudocode |
| `sources.md` | Academic citations |
| `final_assessment.md` | Honest verdict |

## Verdict

**BELOW the 25% CAGR target** for the default (50/200 SMA): 9.2% CAGR.
The chosen default is the weakest parameter in the MA family — slower signals
are less appropriate for the 3-year crypto bull/crash cycle in this sample.

Shorter-lookback configs (TSMOM 30/90d, SMA 10/50) approach or exceed 25% CAGR,
but with only 3 years of data and 12 parameter combinations, this is in-sample
selection and should not be taken at face value.

The core value proposition of trend (reducing drawdown vs buy&hold) is confirmed:
MDD -28% to -38% vs -50% for BTC buy&hold. Sharpe improvement vs buy&hold requires
shorter lookbacks.

See `final_assessment.md` for full discussion.
