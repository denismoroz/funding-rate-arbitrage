# Crypto Cross-Sectional Momentum — Strategy Description

## Concept

Cross-sectional (CS) momentum ranks assets by their recent past return and
bets that recent winners keep outperforming recent losers over the next holding
period. It is well documented in equities (Jegadeesh & Titman 1993) and has been
studied in cryptocurrency markets (see sources.md).

## Universe

12 liquid coins: BTC, ETH, SOL, AVAX, LINK, AAVE, ARB, OP, DOGE, UNI, INJ, TIA.
MATIC excluded: Hyperliquid data truncates at 2024-09-10 due to the POL rebrand.
TIA listed 2023-10-31 — inner-join on all 12 coins limits the backtest start
to 2023-10-31.

**Survivorship caveat**: all coins were liquid and surviving as of 2026-06.
A live deployment would need point-in-time listing dates to avoid selecting only
winners. This is a material upward bias (discussed in final_assessment.md).

## Signal

**Trailing K-day return**: for each coin at close of day t, compute:
```
signal[coin, t] = (close[coin, t] / close[coin, t-K]) - 1
```
K is a hyperparameter; default K = 30.

## Ranking and portfolio construction

At each weekly rebalance boundary:
1. Rank all coins with valid signal by trailing return (descending = best first).
2. **Long-only variant**: assign weight 1/N to each of the top-N coins. Gross = 1.
3. **Long-short variant**: assign weight +1/N to top-N, weight -1/N to bottom-N
   (no overlap). Net exposure ≈ 0. Gross = 2.

Default N = 3.

## Rebalance frequency

**Weekly** (every Monday / first bar of each ISO calendar week). Within the week the
weight is forward-filled — no intra-week changes. This means turnover is ≈ weekly,
not daily, limiting transaction cost drag.

The signal is computed daily but only acted upon at weekly boundaries.

## Execution model

- Signal computed at close of day t (uses only data through t, no look-ahead).
- `qutil.backtest_weights` shifts weights forward by 1 bar internally, so
  positions execute at the NEXT bar's open/close (t+1). No look-ahead.
- Cost: `cost_bps` per side on |Δweight| at each bar where weight changes.
  Default 5 bps (3.5 bps HL taker fee + ~1.5 bps slippage).

## Pseudocode

```python
for each daily bar t:
    compute ret_K[coin] = (close[t] / close[t-K]) - 1  # for all coins

    if t is a weekly boundary (new ISO week):
        rank coins by ret_K descending
        long_side  = top-N coins;  weight = +1/N each
        short_side = bottom-N coins; weight = -1/N each  # long-short only

        current_weight = long_side weights + short_side weights

    W[t] = current_weight   # forward-filled between rebalances

# hand W to backtest_weights() — it shifts and charges costs
```

## Parameter grid

| Parameter | Values tested       | Default |
|-----------|---------------------|---------|
| K (days)  | 7, 14, 30, 60, 90   | 30      |
| N (coins) | 2, 3, 4             | 3       |

## Cost scenarios

| Scenario | cost_bps | Motivation |
|----------|----------|------------|
| Base     | 5.0      | HL taker + modest slippage |
| High     | 10.0     | Stress / illiquid days |
