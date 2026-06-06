# Trend + Carry Blend — Strategy Description

## Construction

### Sleeve 1: Trend Ensemble (daily)

Universe: BTC, ETH, SOL — the three most liquid coins with full 2023-06+ history on Hyperliquid.

Six parameter sets, all long/flat (no short-selling):
- **MA crossovers**: fast/slow = (10/50), (20/100), (50/200) — signal = 1 when fast MA > slow MA, else 0
- **TSMOM**: lookback k = 30, 60, 90 — signal = 1 when coin is above its k-day-ago price, else 0

Per-coin weight for each param set: `signal / 3` (equal-weight across 3 coins).  
Execution: 5bps per-side cost on turnover (Hyperliquid perp taker + slippage).  
No look-ahead: `qutil.backtest_weights` shifts the signal forward by 1 bar.

**Trend ensemble daily return** = simple average of the 6 params' daily net returns.

### Sleeve 2: Carry (daily)

Source: `crypto_funding_carry/results_funding_plus_staking.csv`, column `ret_total`.

This is a pre-computed hourly net return on **total committed capital** from a delta-neutral
carry strategy: long $1 spot + short $1 perp per coin, income = funding rate + staking yield,
costs charged at entry/exit (spot 7bps, perp 3.5bps per side). Universe: 12 large-cap coins.
Capital model: 2x hedged ($1 spot + $1 perp margin), so APR/2 per hour is the correct income rate.

**Daily carry return** = product of hourly returns within each calendar day:
`carry_daily = (1 + hourly_ret).resample('1D').prod() - 1`

### Alignment

Inner join on the daily date index (UTC). Both series span 2023-06-01 to 2026-06-01 (1097 days).

### Portfolio Variants

**Fixed splits** (daily target weight, simple weighted average):
```
port_ret[t] = w_trend * trend_ret[t] + (1 - w_trend) * carry_ret[t]
```
Tested for w_trend in {1.0, 0.75, 0.50, 0.25, 0.0}.

**Inverse-vol (risk parity) blend**:
- 30d trailing volatility of each sleeve, computed from past data only
- Weights ∝ 1/vol, normalized to sum 1
- Monthly rebalance (last-of-month weight applied next month), then shifted 1 day
- See caveat below

## Regime-Orthogonality Thesis

Trend following and carry are structurally regime-complementary:

- **Trend earns in trending markets**: sustained directional price moves are captured by MA/TSMOM
  signals. 2023 (BTC 3x) and 2024 (BTC ATH cycle) are ideal for trend.
- **Carry earns in choppy/ranging markets**: when price is mean-reverting or range-bound,
  funding rates remain positive (market still pays for leverage), even if price drift is zero.
  2025 (crypto sideways post-ATH) is where carry continues to compound while trend goes flat.

The **correlation of daily returns** between the two sleeves is 0.163 — low but not zero,
because both suffer slightly during sharp drawdowns (crypto liquidation cascades spike everything).
The correlation is predominantly regime-driven, not constant.

Yearly evidence (see `final_assessment.md`):
- 2023: trend dominates (178% CAGR vs 11% carry) — classic bull trend
- 2024: trend still positive (45%) but carry also strong (13%); trend MDD gets nasty (-34%)
- 2025: both weak, carry slightly better by Sharpe; trend MDD severe (-23%)
- 2026 YTD: both negative; trend -26% vs carry -1%; carry dramatically better drawdown control

## Inverse-Vol Caveat

Risk parity is degenerate in this pairing. Carry's daily volatility (~0.037%) is approximately
**60-100x smaller** than trend's daily volatility (~2.3%). Naive inverse-vol therefore assigns
~98-99% weight to carry at all times, making the "risk parity" blend nearly indistinguishable
from carry-only. This is reported honestly in the metrics. The fixed-split variants are far
more informative for understanding the blend's Calmar frontier.
