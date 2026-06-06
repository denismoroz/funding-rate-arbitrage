# Regime-Switch Strategy: Trend vs Carry

## Concept

A meta-strategy that dynamically allocates capital between two orthogonal return streams —
a **trend-following ensemble** (BTC/ETH/SOL MA + TSMOM) and a **delta-neutral carry** sleeve
(funding rate + staking) — by detecting whether the market is in a trending or choppy regime.

The hypothesis: trend earns in trending regimes, carry earns in choppy regimes. Switching
should improve the Sharpe/Calmar ratio versus a static 50/50 blend.

---

## Input Series

### Trend Ensemble
- Assets: BTC, ETH, SOL (daily closes via HL 1h OHLCV resampled)
- 6 parameter sets:
  - MA crossovers: (10,50), (20,100), (50,200)
  - TSMOM: 30d, 60d, 90d
- Signal: binary long/flat; per-coin weight = signal / 3 (equal-weight 3 coins)
- Cost: 5bps per-side on turnover
- Ensemble: simple average of 6 daily net-return series

### Carry
- Source: `crypto_funding_carry/results_funding_plus_staking.csv`, column `ret_total` (hourly)
- Compounding: `(1 + r).resample('1D').prod() - 1` → daily carry return

---

## Regime Indicators

All indicators are computed on **BTC daily close**, **trailing only** (data up to and including
close at time t). Regime decided at t, applied to t+1 portfolio weights — **zero look-ahead**.

### (i) Kaufman Efficiency Ratio (ER)
```
ER(t, W) = |close[t] - close[t-W]| / sum_{i=t-W+1}^{t} |close[i] - close[i-1]|
```
- W = 30 days
- Numerator: net directional move over the window
- Denominator: total path length (sum of absolute daily changes)
- Range: [0, 1]. High ER → trending. Low ER → choppy/mean-reverting.
- Threshold: trailing expanding median of ER (adaptive threshold, no look-ahead)

### (ii) Strategy Momentum
```
StratMom(t, W) = prod_{i=t-W+1}^{t} (1 + trend_ret[i]) - 1
```
- W = 30 days
- Positive cum_ret → trend sleeve has been working → stay in trend
- Negative → shift to carry

---

## Switch Rules

| Variant   | Trending regime         | Choppy regime           | Indicator |
|-----------|------------------------|------------------------|-----------|
| hard_er   | 100% trend / 0% carry  | 0% trend / 100% carry  | ER >= trailing median |
| soft_er   | 75% trend / 25% carry  | 25% trend / 75% carry  | ER >= trailing median |
| hard_mom  | 100% trend / 0% carry  | 0% trend / 100% carry  | 30d trend cum_ret > 0 |
| soft_mom  | 75% trend / 25% carry  | 25% trend / 75% carry  | 30d trend cum_ret > 0 |

---

## Pseudocode

```python
# At each day t:

# --- Regime detection (trailing, no look-ahead) ---
er_t = abs(btc[t] - btc[t-W]) / sum(abs(btc[i]-btc[i-1]) for i in range(t-W+1, t+1))
er_median_t = expanding_median(er, up_to=t)
er_regime_t = 1 if er_t >= er_median_t else 0

strat_mom_t = prod(1 + trend_ret[t-W+1 .. t]) - 1
mom_regime_t = 1 if strat_mom_t > 0 else 0

# --- Weight decided at t ---
# Hard ER:
w_trend_decided[t] = 1.0 if er_regime_t == 1 else 0.0
# Soft ER:
w_trend_decided[t] = 0.75 if er_regime_t == 1 else 0.25
# (similarly for mom variants)

# --- Applied at t+1 ---
w_trend_held[t+1] = w_trend_decided[t]

# --- Portfolio return ---
ret_gross[t+1] = w_trend_held[t+1] * trend_ret[t+1] + (1-w_trend_held[t+1]) * carry_ret[t+1]

# --- Switching cost ---
delta_w = abs(w_trend_held[t+1] - w_trend_held[t])
switch_cost[t+1] = delta_w * 5bps

# --- Net return ---
ret_net[t+1] = ret_gross[t+1] - switch_cost[t+1]
```

---

## Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| ER window W | 30 days | ~1 month, standard Kaufman window |
| ER threshold | trailing expanding median | Adaptive; avoids look-ahead |
| Momentum window | 30 days | Symmetric with ER window |
| Switching cost | 5bps on \|Δw\| | Per-side HL taker + slippage, applied each rebalance |
| Initial weight | 0.5 (50/50) | Neutral start before first signal |
