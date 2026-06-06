# Crypto Reversal / Mean Reversion Backtest

**Status: FAILED — does not survive realistic costs. Do not trade.**

Two flavors of short-term mean reversion / reversal tested on Hyperliquid perpetuals, 2023-10-31 to 2026-06-01.

## Quick Results

### Flavor A: Cross-Sectional Weekly Reversal (1d bars)
Default: K=5 days lookback, N=3 coins per leg, dollar-neutral

| Cost | CAGR | Sharpe | MaxDD |
|------|------|--------|-------|
| 0bps | -17.3% | -0.07 | -59.9% |
| 5bps | -23.5% | -0.21 | -65.6% |
| 10bps | -29.2% | -0.35 | -70.7% |

Best grid cell at 5bps: K=5, N=4 → Sharpe=-0.01 (least bad, still negative)

### Flavor B: Z-Score Mean Reversion (1h bars, BTC+ETH)
Default: W=48h window, Z=2.0 threshold

| Cost | CAGR | Sharpe | MaxDD | Trades/Day |
|------|------|--------|-------|-----------|
| 0bps | -16.5% | -0.33 | -49.8% | ~1.7 |
| 5bps | -30.2% | -0.83 | -69.6% | ~1.7 |
| 10bps | -41.7% | -1.34 | -81.6% | ~1.7 |

Best grid cell at 5bps: W=48, Z=1.5 → Sharpe=-0.62 (still clearly negative)

### Benchmark: BTC Buy-and-Hold
CAGR=+38.7%, Sharpe=+0.93, MaxDD=-49.5%

## Files
- `backtest.py` — main backtest script; run directly with activated venv
- `metrics.json` — full metrics, grid results, yearly breakdowns
- `results.csv` — daily equity curves for all flavors at all cost levels
- `trades.csv` — Flavor B (BTC) trade events (1091 entries)
- `trades_a.csv` — Flavor A weekly rebalance log (920 weeks)
- `equity_a.png` — Flavor A equity curve vs BTC buy-and-hold
- `equity_b.png` — Flavor B equity curve vs BTC buy-and-hold
- `strategy_description.md` — detailed rules and pseudocode
- `sources.md` — academic and data citations
- `final_assessment.md` — honest verdict with analysis

## Key Findings

1. **Neither flavor is profitable at ANY cost level, even 0bps.** The gross reversal signal is slightly negative in crypto during this 2023-2026 bull market sample.

2. **Costs accelerate destruction, not just erode profits.** Each 5bps of cost reduces annual return by ~6-13 percentage points for Flavor A and ~14 for Flavor B (due to higher turnover).

3. **Flavor B's high turnover (~1.7 round trips/day) makes it especially cost-fragile.** At 5bps/side, the annualized cost drag is ~62%. The strategy is structurally unviable.

4. **Correlation to BTC is near-zero** (Flavor A: r=0.08, Flavor B: r=0.02). Dollar-neutral construction works. But uncorrelated + losing ≠ useful diversifier at these drawdown levels (-66% to -70% MaxDD at 5bps).

5. **2023 Flavor A "114% CAGR" is an artifact** of annualizing a positive run over only 50 bars (7 weeks). Not meaningful.

6. **A coding bug was caught and fixed:** `W.replace(0.0, NaN)` in the weight construction was corrupting the forward-fill, creating non-dollar-neutral portfolios. This generated a spurious K=3 N=2 result of 88% CAGR. After fixing, all cells are negative.

## Running

```bash
source .venv/bin/activate
python research/quant/crypto_reversal_meanrev/backtest.py
```
Runtime: ~3 minutes (grid search with 9 Flavor A cells × 3 costs + 9 Flavor B cells × 3 costs).
