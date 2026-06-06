# Strategy Description: Crypto Time-Series Momentum / Trend-Following

## Overview

Long-only trend-following on a basket of BTC, ETH, SOL using two signal families:
(A) SMA crossover and (B) time-series momentum. The strategy is always either long
or flat (no shorting in the default config). A volatility-targeting overlay rescales
positions to target a constant annualised realised volatility.

## Universe and Timeframe

- Assets: BTC, ETH, SOL
- Timeframe: daily bars, resampled from 1h OHLCV (causal: daily bar labelled D uses
  closes from D 00:00..D 23:00)
- Sample: 2023-06-01 to 2026-06-01 (≈3 years, 1097 bars per coin)
- MATIC excluded (HL series ends 2024-09-10, POL rebrand)

## Signal Family A: SMA Crossover

```
fast_sma[t] = mean(close[t-fast+1 : t])
slow_sma[t] = mean(close[t-slow+1 : t])
signal[t]   = 1  if fast_sma[t] > slow_sma[t]
              0  otherwise  (long/flat default)
             -1  when signal==0  (long/short variant only)
```

Grid tested: (fast, slow) in {(10,50), (20,100), (50,200)}.
Default: (50, 200).

## Signal Family B: Time-Series Momentum (TSMOM)

```
ret_k[t]   = close[t] / close[t-k] - 1
signal[t]  = 1  if ret_k[t] > 0
             0  otherwise  (long/flat default)
            -1  when signal==0  (long/short variant only)
```

Grid tested: K in {30, 60, 90} days.

## Volatility Targeting (applied to default only)

```
daily_ret[t]   = close[t] / close[t-1] - 1
rv[t]          = std(daily_ret[t-30 : t-1]) * sqrt(365)   # trailing 30-day annualised vol
scale[t]       = min(TARGET_VOL / rv[t],  VOL_CAP)
                 where TARGET_VOL = 0.40, VOL_CAP = 1.5
weight[t]      = signal[t] * scale[t]                     # per-coin raw weight
basket_w[t]    = weight[t] / N_COINS                      # equal-weight basket (N=3)
```

When rv is undefined (first 30 bars), weight = 0.

## Equal-Weight Basket

Each coin's weight is divided by 3 (N_COINS) so that the maximum portfolio notional
is 1.0 (long all three at full size). With vol targeting, individual coin weights
typically range 0.05..0.50.

## No-Look-Ahead Guarantee

`backtest_weights()` from qutil shifts the target_weight forward by 1 bar internally.
The weight DECIDED at bar t is executed at bar t+1 and earns return[t+1].
No manual shifting is applied in backtest.py.

## Transaction Costs

- Default: 5.0 bps per side on |Δweight| each bar (reflecting HL perp taker ~3.5bps
  + ~1.5bps slippage)
- Sensitivity: also run at 10.0 bps per side

## Pseudocode (default config)

```python
px      = load_closes(['BTC','ETH','SOL'], '1d')['2023-06-01':'2026-06-01']
W       = zeros(T x 3)

for each coin c in ['BTC','ETH','SOL']:
    fast_sma = px[c].rolling(50).mean()
    slow_sma = px[c].rolling(200).mean()
    signal   = (fast_sma > slow_sma).astype(float)         # 0 or 1
    rv       = px[c].pct_change().rolling(30).std() * sqrt(365)
    scale    = clip(0.40 / rv, upper=1.5)
    W[c]     = signal * scale / 3.0                        # basket weight

backtest_weights(px, W, cost_bps=5.0)    # qutil shifts W by 1 bar internally
```

## Sensitivity Grid Notes

The sensitivity grid runs WITHOUT vol-targeting (raw signal / N_coins) so that the
effect of parameter choice is isolated from the vol-scaling overlay. The long/short
variant uses signal ∈ {-1/3, +1/3} per coin.

## Key Results Summary

| Config | CAGR | Sharpe | MDD | Calmar |
|---|---|---|---|---|
| **Default (50/200 SMA, vol-tgt, 5bps)** | 9.2% | 0.46 | -27.7% | 0.33 |
| Default at 10bps | 8.9% | 0.45 | -28.1% | 0.32 |
| Best grid: TSMOM 90d, long/flat, 5bps | 44.8% | 1.10 | -31.1% | 1.44 |
| Buy&Hold BTC (benchmark) | 38.7% | 0.93 | -49.5% | 0.78 |

The 50/200 SMA default chosen a priori is conservative (slow to react); shorter-window
signals materially outperform over this sample period.
