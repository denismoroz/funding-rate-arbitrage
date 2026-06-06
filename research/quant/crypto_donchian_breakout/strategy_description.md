# Donchian Channel Breakout — Strategy Description

## Overview

Classic Turtle Trading adapted for crypto: trade breakouts from the Donchian channel
(N-day high / M-day low) on daily bars, long/flat only (primary) or long/short (secondary).
Equal-weight basket of BTC, ETH, SOL. Period: 2023-06-01 to 2026-06-01.

## Rules (Long/Flat, Primary)

### Entry
Enter LONG at next-bar close when:
```
close[t] > high.rolling(N).max().shift(1)
```
i.e., today's close exceeds the highest HIGH of the **prior** N bars (excluding today).

### Exit
Exit to FLAT at next-bar close when:
```
close[t] < low.rolling(M).min().shift(1)
```
i.e., today's close breaks below the lowest LOW of the **prior** M bars (excluding today).

### Position sizing
- Weight = 1.0 (fully invested) when in position.
- Weight = 0.0 (cash) when flat.
- Equal-weight basket: each coin gets 1/3 of capital when that coin is in signal.

### Default parameters (Turtle "System 2")
- N = 55 (entry)
- M = 20 (exit)

## Rules (Long/Short, Secondary)

Additional short leg:
- Enter SHORT when `close[t] < low.rolling(N).min().shift(1)`.
- Cover short when `close[t] > high.rolling(N).max().shift(1)`.

## No Look-Ahead Protocol

1. Rolling windows use `.shift(1)`: the channel value at bar t is computed from data
   through bar t-1 only.
2. `qutil.backtest_weights` then shifts the resulting target weight one additional bar,
   so the trade executes at bar t+1 (next-bar execution). This double-conservative
   approach ensures zero look-ahead.

## Pseudocode

```python
upper = high.rolling(N).max().shift(1)   # prior N-day high
lower = low.rolling(M).min().shift(1)    # prior M-day low

state = 0  # 0=flat, 1=long
for each bar t:
    if isnan(upper[t]) or isnan(lower[t]):
        state = 0
    elif state == 0 and close[t] > upper[t]:
        state = 1   # breakout entry
    elif state == 1 and close[t] < lower[t]:
        state = 0   # channel exit

weight[t] = state   # 1 = long, 0 = flat
# qutil.backtest_weights(prices, weight) shifts weight forward 1 more bar internally
```

## Cost Model
- Per-side taker + slippage: 5 bps (default), also tested at 10 bps.
- Turnover charged on `|Δweight|` each bar.

## Grid
| N (entry) | M (exit) |
|-----------|----------|
| 20        | 10       |
| 20        | 20       |
| 55        | 10       |
| 55        | 20       |

Default: N=55 / M=20 (Turtle System 2).
