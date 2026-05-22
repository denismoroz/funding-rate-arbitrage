# Margin Policy Sweep — Analysis

## Setup

Universe: 7 coins (BTC, ETH, SOL, AVAX, LINK, AAVE, DOGE) with per-coin leverage caps
(BTC/ETH 20x, SOL/AVAX/LINK 10x, AAVE/DOGE 5x).
Data period: 2023-06-08 to 2026-05-12 (~25 600 hourly bars, covering 2.9 years including
the 2024–2025 bull run and subsequent consolidation).

Baseline: margin_buffer_x=3×, position_size=$100, concurrency_cap K=3.
Budget cap $1 000, entry threshold 0.30 annualised, exit at −0.15, min hold 120 h,
signal window 12 h, taker fees perp 3.5 bp / spot 7 bp.

Sweep grid: margin_buffer_x ∈ {2.0, 3.0, 5.0} × position_size ∈ {$50, $100, $150}
× concurrency_cap ∈ {3, 5} = 18 configs. All other parameters held at baseline defaults.
Wall time: 54 seconds.

## Headline Numbers

**Best Calmar config** — buffer=5×, size=$50, K=5:
- Annual return: +29.76%
- Max drawdown: 0.015%
- Calmar: 1 951
- Sharpe: 3.82
- Liquidations: 0

**Baseline** (buffer=3×, size=$100, K=3):
- Annual return: +29.01%
- Max drawdown: 0.031%
- Calmar: 951
- Sharpe: 3.63
- Liquidations: 0

The best Calmar config roughly doubles the risk-adjusted return of the baseline
by cutting position size in half (smaller individual drawdowns) while raising K to 5.
Absolute return is nearly identical (+29.8% vs +29.0%).

## vs. sUSDe Baseline (~12% APR Passive)

| Config | Annual | Premium over sUSDe |
|---|---|---|
| Best Calmar (5×, $50, K=5) | +29.8% | +17.8 pp |
| Baseline (3×, $100, K=3) | +29.0% | +17.0 pp |

Both configurations generate roughly 2.5× the passive sUSDe yield.
That premium compensates for active execution risk, wallet-management friction,
and funding-rate non-stationarity not captured by backtest.

## Risk Events

Only 1 of 18 configs suffered a liquidation: buffer=2×, size=$150, K=5.
This is the highest-capital-intensity corner of the grid (thin margin, large
position, maximum concurrency). At liquidation the strategy lost its entire
perp wallet, resulting in −11.95% annualised — the only losing config.

Correlation between Calmar and buffer_x across the 17 surviving configs is +0.31
(moderate positive). Larger buffers reduce drawdown depth faster than they reduce
returns, improving Calmar. The strongest lever on Calmar is actually concurrency
(K=5 dominates K=3 in every matched pair) via higher diversification.

## Recommendation

Use **buffer=3×, position_size=$100, K=5** for live deployment. This config
(annual +34.1%, Calmar 1 118, Sharpe 3.97, zero liquidations) sits one step above
the A2 baseline in K without the capital-efficiency loss of the 5× buffer or the
liquidation risk at 2×. The 3× buffer keeps perp margin requirements moderate
while providing a safe distance from the 1× liquidation boundary even during
correlated adverse moves. Position size of $100 keeps per-trade notional within
HL spot-wallet transfer limits without the marginal-return decay seen at $150
(where skipped-open events increase). Raising K from 3 to 5 adds ~5 pp of annual
return with no measurable increase in max drawdown.

## What This DOES NOT Model

- Wallet-transfer latency between HL spot and perp sub-accounts (can be 10–30 s,
  relevant for fast-moving entries and top-ups under stress)
- HL spot/perp wallet split friction: margin top-ups require an on-chain transfer
  that the backtest treats as instantaneous
- Real maintenance-margin liquidation quirks on HL (partial liquidations,
  socialized loss, insurance fund backstop — backtest assumes full cascade)
- Slippage on forced-close: illiquid coins (AAVE, LINK) may incur 0.5–2% of
  additional cost on a panic close at the worst moment
- Funding rate non-stationarity: the 2023–2026 sample includes an unusual bull
  run with persistently high positive funding; live funding may be lower or
  frequently negative, compressing the alpha
