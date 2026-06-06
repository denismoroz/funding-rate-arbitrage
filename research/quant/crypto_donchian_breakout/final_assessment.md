# Final Assessment: Donchian Channel Breakout

## Objective
Test whether a classic Turtle-style Donchian channel breakout applied to BTC/ETH/SOL
(equal-weight basket, daily bars, 2023-06-01..2026-06-01) can approach a 25% CAGR
hurdle with acceptable risk-adjusted returns.

---

## Default Strategy (N=55 / M=20, long-only, 5 bps/side)

| Metric           | Strategy  | BTC Buy&Hold |
|------------------|-----------|--------------|
| CAGR             | **28.5%** | 38.6%        |
| Volatility       | 31.3%     | 46.6%        |
| Sharpe           | **0.96**  | 0.93         |
| Sortino          | 0.99      | 1.41         |
| Max Drawdown     | **-28.6%**| -49.5%       |
| Calmar           | **1.00**  | 0.78         |
| Exposure         | 50.5%     | 100%         |

The strategy **meets the 25% CAGR hurdle** (28.5% vs 25% target).
It achieves this with roughly half the time in-market and substantially lower max
drawdown than buy&hold (-28.6% vs -49.5%), producing a superior Calmar ratio (1.00 vs 0.78).
Sharpe is marginally better (0.96 vs 0.93) despite 50% less exposure — meaning the
active bets are better compensated per unit of risk.

---

## Trade Profile

Classic Turtle / breakout signature confirmed:

| Metric           | Value     |
|------------------|-----------|
| # Closed trades  | 21        |
| Win rate         | 52.4%     |
| Profit factor    | **4.36**  |
| Avg trade        | +17.3%    |
| Avg winner       | +42.8%    |
| Avg loser        | -10.8%    |
| Best trade       | +212%     |
| Worst trade      | -33.1%    |

This is the expected breakout profile: moderate win rate (~50%) with a dramatically
asymmetric payoff (winners are 4× larger than losers on a gross basis). The strategy
is not a mean-reversion system — losses are small (well-defined channel exits) and
occasional trend-riding trades are enormous.

---

## Yearly Breakdown

| Year | CAGR     | Sharpe | MaxDD  |
|------|----------|--------|--------|
| 2023 | **+211%**| 3.42   | -11%   |
| 2024 | **+32%** | 0.94   | -27%   |
| 2025 | **-5%**  | -0.05  | -18%   |
| 2026 | **-28%** | -2.71  | -13%   |

**Regime dependence is severe.** The 2023 result (the crypto bull-to-consolidation-to-run
period) is extraordinary and constitutes the bulk of the 3-year CAGR. The strategy has
been negative in 2025 and significantly negative in 2026 so far. The trend environment
in 2025-2026 has been choppy / macro-pressured, which is precisely when breakout systems
underperform: frequent false breakouts chew through small losses while never delivering
the large trend that pays for them.

The 3-year CAGR is flattered by a single extraordinary year. **On a forward-looking
basis, 25% is not a reliable expectation** — 5-15% in flat/bear regimes is more honest.

---

## Grid Search Results (long-only, 5 bps)

| N  | M  | CAGR  | Sharpe | MaxDD   | Calmar | #Trades | Win%  | PF   |
|----|-----|-------|--------|---------|--------|---------|-------|------|
| 20 | 10 | 23.0% | 0.79   | -38.5%  | 0.60   | 51      | 35.3% | 2.20 |
| 20 | 20 | 31.5% | 0.91   | -40.7%  | 0.77   | 36      | 41.7% | 2.85 |
| **55** | **10** | **32.4%** | **1.13** | **-26.7%** | **1.21** | 26 | 53.8% | 3.86 |
| 55 | 20 | 28.5% | 0.96   | -28.6%  | 1.00   | 21      | 52.4% | 4.36 |

**Best grid cell: N=55 / M=10** — tighter exit preserves profits while the 55-day entry
filters noise. Sharpe 1.13, MaxDD -26.7%, Calmar 1.21. The improvement is modest,
suggesting the system is not highly parameter-sensitive in this range (good for robustness)
but also not dramatically improvable by tuning exits.

The shorter-entry (N=20) cells suffer: more frequent false breakouts, higher drawdowns,
lower profit factors. The Turtle insight — use longer windows to filter noise — is confirmed.

---

## Cost Sensitivity (N=55 / M=20)

| Cost (bps) | CAGR  | Sharpe | Calmar |
|------------|-------|--------|--------|
| 5          | 28.5% | 0.96   | 1.00   |
| 10         | 28.2% | 0.95   | 0.98   |

**Very low cost sensitivity.** With only ~7 trades/coin/3yr (21 closed trades across 3
coins), turnover is minimal. Doubling costs barely changes outcomes. This is a genuine
feature of Donchian systems: they trade rarely.

---

## Long/Short Variant (N=55 / M=20, 5 bps)

| Metric       | Long/Short | Long-Only |
|--------------|------------|-----------|
| CAGR         | 11.3%      | 28.5%     |
| Sharpe       | 0.47       | 0.96      |
| Max Drawdown | -55.7%     | -28.6%    |
| Calmar       | 0.20       | 1.00      |

**Adding shorts is destructive in this sample.** Crypto's long-run positive drift means
shorting into downtrends that reverse sharply produces large losses. The short leg roughly
halves CAGR and nearly doubles drawdown. Long/flat dominates for crypto trend systems.

---

## Per-Coin Breakdown (N=55 / M=20, 5 bps)

| Coin | CAGR  | Sharpe | MaxDD   | Calmar | Exposure |
|------|-------|--------|---------|--------|----------|
| BTC  | 15.6% | 0.64   | -31.5%  | 0.50   | 38.8%    |
| ETH  | 37.1% | 1.09   | -28.4%  | 1.31   | 33.2%    |
| SOL  | 22.6% | 0.65   | -67.7%  | 0.33   | 32.7%    |

ETH carries the basket. SOL has brutal individual drawdowns (-67.7%) but is
partially mitigated by being 1/3 of the basket. BTC's trend signal is weakest
in this period — possibly because it is also the most liquid/efficient.

---

## Robustness Assessment

**Positive:**
- Parameters are essentially unchanged from 1980s commodity futures origins (N=55/M=20).
  No in-sample fitting. The default is a priori justified.
- Low cost sensitivity — only ~7 trades per coin over 3 years.
- MaxDD materially lower than buy&hold with comparable CAGR. Genuine risk reduction.
- Trade profile (low win rate, high payoff) is exactly what theory predicts — no
  suspicion of overfitting.

**Negative / Caveats:**
- **3-year window is extremely short** for a system that holds positions for months.
  21 closed trades across 3 coins is not statistically meaningful. Sharpe CIs are wide.
- **2023 dominates.** Remove 2023 and the strategy is flat-to-negative on this sample.
  This is inherent to trend following: profits are lumpy and regime-dependent.
- **Survivorship bias**: BTC, ETH, SOL are the winners of this period. The system was
  not applied to a broader universe that would include losers.
- **Look-ahead clean** but the backtest period (2023-2026) includes one of the strongest
  crypto bull cycles on record.
- **No transaction costs for funding** (long perp pays positive funding ~6-8%/yr on HL
  at current rates). If this strategy were implemented as long perp + cash, live funding
  costs would reduce CAGR by ~3-4 percentage points in trending periods (more in sideways).

---

## Verdict vs 25% CAGR Target

| Criterion              | Assessment                                     |
|------------------------|------------------------------------------------|
| 25% CAGR (3yr sample)  | Met: 28.5% — but heavily regime-dependent      |
| Drawdown reduction     | Genuine: -28.6% vs -49.5% B&H, Calmar 1.0     |
| Robustness             | Moderate: 2025-2026 regimes are negative       |
| Forward confidence     | Low: ~5-15% realistic in non-trending regimes  |
| Vs alternatives        | TSMOM / carry strategies likely superior live  |

**Honest verdict: meets the 25% hurdle on the 3-year backtest, but with a one-year
result driving 85% of the P&L. The system works as advertised — trend-following with
genuine drawdown reduction and a clean trade profile — but is not a reliable 25% CAGR
machine. In choppy/sideways crypto regimes it will be flat-to-negative.**

A practitioner deploying this would need to either (a) accept regime dependency and hold
through flat periods, or (b) combine with a carry/funding strategy (e.g., the live
Strategy A already running) that earns in sideways markets where this underperforms.
The two strategies have genuinely different regime sensitivities, making combination
interesting from a portfolio construction perspective.
