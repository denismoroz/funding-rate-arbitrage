# Regime-Switch: Trend vs Carry

Dynamic capital allocation between a trend-following ensemble and a delta-neutral carry sleeve,
driven by a Kaufman Efficiency Ratio (and strategy momentum) regime filter.

## Question

Does switching capital between trend and carry sleeves based on market regime
(trending vs choppy) beat a static 50/50 blend?

## Short Answer

**No.** Static 50/50 (CAGR 22%, Sharpe 1.18, Calmar 1.29) beats all four regime-switch
variants on both Calmar and Sharpe, net of switching costs.

## Files

| File | Description |
|------|-------------|
| `backtest.py` | Full backtest implementation |
| `results.csv` | Daily net returns for all variants |
| `metrics.json` | Full metrics, regime diagnostics, yearly breakdown |
| `equity.png` | Equity curves + drawdown panel |
| `final_assessment.md` | Detailed findings and verdict |
| `strategy_description.md` | Regime logic + pseudocode |
| `sources.md` | References (Finominal, Kaufman) |

## Results

| Variant         | CAGR  | Sharpe | MDD    | Calmar | % In Trend | # Switches |
|-----------------|-------|--------|--------|--------|------------|------------|
| trend_only      | 34.3% | 0.99   | -33.9% | 1.01   | 100%       | —          |
| carry_only      | 7.3%  | 9.95   | -0.7%  | 10.72  | 0%         | —          |
| **static_50_50**| **22.0%** | **1.18** | **-17.1%** | **1.29** | — | —     |
| hard_er         | 25.6% | 1.01   | -28.1% | 0.91   | 40.6%      | 121        |
| soft_er         | 24.3% | 1.17   | -21.4% | 1.13   | 40.6%      | 121        |
| hard_mom        | 34.6% | 1.09   | -34.6% | 1.00   | 46.0%      | 66         |
| soft_mom        | 28.7% | 1.16   | -25.3% | 1.14   | 46.0%      | 66         |

## Method

- **Trend**: 6-param ensemble (MA 10/50, 20/100, 50/200 + TSMOM 30,60,90), BTC/ETH/SOL, 5bps cost
- **Carry**: hourly funding+staking compounded to daily
- **ER**: Kaufman Efficiency Ratio over 30d BTC close, threshold = trailing expanding median
- **Momentum**: trend sleeve's 30d trailing cum_ret > 0
- **Switch cost**: 5bps on |Δw_trend| each rebalance day
- **No look-ahead**: all signals decided at t, applied at t+1
- **Period**: 2023-06-01 to 2026-06-01 (1097 days)
