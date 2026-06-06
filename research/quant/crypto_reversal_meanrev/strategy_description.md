# Strategy Description: Short-Term Reversal / Mean Reversion

## Flavor A: Cross-Sectional Short-Term Reversal

### Intuition
In equity markets, the 1-week reversal anomaly (Lehmann 1990, Jegadeesh 1990) shows that last week's biggest losers tend to outperform and last week's biggest winners tend to underperform over the next week. The effect is attributed to liquidity provision, overreaction, and bid-ask bounce. In crypto, similar patterns have been documented (see sources).

### Rules
- Universe: BTC, ETH, SOL, AVAX, LINK, AAVE, ARB, OP, DOGE, UNI, INJ, TIA (12 coins; MATIC excluded due to POL rebrand data cutoff)
- Frequency: daily bars (1d), weekly rebalance
- Each week's last trading day: compute trailing-K-day return for each coin
- Rank all 12 coins by that return (ascending = worst to best)
- Long the bottom-N coins (biggest losers) with equal weight = 1/N each
- Short the top-N coins (biggest winners) with equal weight = -1/N each
- Dollar-neutral by construction: sum(weights) = 0; gross exposure = 2.0
- Hold weights until the next weekly rebalance (forward-fill daily)
- Default: K=5 days, N=3 coins per leg
- Grid: K in {3, 5, 7}, N in {2, 3, 4}

### Pseudocode
```
prices = load_daily_closes(UNIVERSE)
trailing_ret[t] = prices[t] / prices[t-K] - 1

for each weekly rebalance date t:
    ranks = sort(trailing_ret[t], ascending=True)
    losers = ranks[:N]     # worst K-day performers
    winners = ranks[-N:]   # best K-day performers
    w[t, losers]  = +1/N
    w[t, winners] = -1/N
    w[t, others]  = 0

# Forward-fill w between rebalance dates
# qutil shifts w by 1 bar: position held at t+1 earns ret[t+1]
# Costs charged on |Δw| each bar at cost_bps per side
```

---

## Flavor B: Single-Asset Intraday Z-Score Mean Reversion

### Intuition
Price deviations from a rolling mean tend to revert. When the z-score of the close (relative to a trailing window) becomes sufficiently extreme, we fade the move. This is classic statistical mean reversion applied intraday.

### Rules
- Assets: BTC and ETH (equal allocation, 1/N each)
- Frequency: hourly bars (1h)
- For each coin: compute z = (close - SMA_W) / std_W (trailing window of W hours, all causal)
- Position: +1 (long) when z < -Z_thresh; -1 (short) when z > +Z_thresh
- Exit to flat when |z| < 0.5 (exit band)
- State machine: once in a position, stay until exit condition; no re-entry in same direction while position is open
- Weight per coin = position / n_coins (scales to 0.5 per coin when active)
- Default: W=48 hours, Z_thresh=2.0
- Grid: W in {24, 48, 72}, Z in {1.5, 2.0, 2.5}

### Pseudocode
```
for coin in [BTC, ETH]:
    sma[t] = mean(close[t-W+1 : t])
    std[t] = std(close[t-W+1 : t])
    z[t] = (close[t] - sma[t]) / std[t]

    position = 0  # state machine
    for t in timeline:
        if position == 0:
            if z[t] < -Z_thresh: position = +1
            if z[t] > +Z_thresh: position = -1
        else:
            if |z[t]| < 0.5: position = 0
        w[coin, t] = position / n_coins

# qutil shifts w by 1 bar: no look-ahead
# Costs charged on |Δw| each bar
```

### Key Risk: Turnover
Flavor B triggers ~1.3-4.0 round trips per day depending on parameters. At 5bps/side (realistic perp taker), this is ~0.065-0.2% per day in costs alone, which annualizes to 24-73%. The strategy must generate gross alpha exceeding this purely from mean-reversion gains — which it does not in this dataset.
