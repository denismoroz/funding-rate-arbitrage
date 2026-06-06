# Strategy Description: Crypto Funding Rate Contrarian / Reversal

## Core Hypothesis

Extreme funding rates signal crowded positioning. When funding is very high, over-leveraged longs are packed in — the crowd will unwind, driving price DOWN. When funding is very negative, forced shorts dominate — the crowd will cover, driving price UP. We go contrarian on the z-score of funding.

This is a DIRECTIONAL hypothesis about PRICE behavior. The primary question is whether price-only PnL is positive. Funding received on the short leg is a secondary benefit, not the thesis.

## Signal Construction

### Step 1: Daily Funding

```
hourly_funding_rate = research/data/<COIN>.csv  [col: fundingRate, UTC timestamps]
daily_funding[t]    = sum(hourly_fundingRate) over UTC day t      # total funding fraction
funding_ann[t]      = daily_funding[t] * 365                      # annualized
```

Resample is causal: each daily bar aggregates intra-day hourly stamps; no forward look.

### Step 2: Rolling Z-Score

```
roll_mean[t] = mean(funding_ann[t-L+1 .. t])     # trailing window L days, ends at t
roll_std[t]  = std(funding_ann[t-L+1 .. t])
z[t]         = (funding_ann[t] - roll_mean[t]) / roll_std[t]
```

No look-ahead: z[t] uses only data through day t. qutil.backtest_weights further shifts weights +1 bar so they earn return at t+1.

---

## Variant A: Cross-Sectional (Market-Neutral)

**Parameters:** N=3, L=30 (defaults); grid N ∈ {2,3,4}, L ∈ {14,30,60}

```
For each day t:
  rank coins by z[t] ascending
  LONG  bottom-N coins (lowest z, most negative funding)   weight = +1/N each
  SHORT top-N   coins (highest z, most positive funding)   weight = -1/N each
```

- Dollar-neutral: net weight = 0, gross = 2
- Market-neutral: ~zero systematic beta
- Interpretation: relative value across coins; most crowded long vs most crowded short

## Variant B: Time-Series Per-Coin

**Parameters:** Z_thresh=1.5, L=30 (defaults); grid Z ∈ {1.0,1.5,2.0}, L ∈ {14,30,60}

```
For each coin i on day t:
  if z_i[t] > +Z_thresh:  w_i = -1/N_total   (short: high funding -> price reversion down)
  if z_i[t] < -Z_thresh:  w_i = +1/N_total   (long:  low funding -> price reversion up)
  else:                    w_i = 0            (flat)
```

- N_total = total number of coins in universe (12 for full)
- Not dollar-neutral; net position varies with how many coins are in extreme regime
- Interpretation: absolute-value signal; each coin managed independently

---

## Decomposition: Price-Only vs Total (KEY)

Each variant is run twice on identical weights:

```python
# Run 1: Pure price PnL (no funding)
bt_price = backtest_weights(px, W, cost_bps=5, funding=None)

# Run 2: Price + funding carry
bt_total = backtest_weights(px, W, cost_bps=5, funding=funding_daily_panel)
```

In `backtest_weights`, the funding convention is:
- A position w pays `-w * funding` per bar
- So a SHORT position (w < 0) on a positive-funding asset RECEIVES funding: `-(negative) * positive = positive`

The funding component is: `ret_funding = bt_total.ret_funding` (already clean in qutil output).

**If price-only PnL ≈ 0 or negative and all return comes from funding, this strategy is just carry in disguise.** We report both components separately and state this verdict explicitly.

## Cost Model

- Default: 5 bps per side on |Δweight| per bar (perp taker ~3.5bps + slippage)
- Sensitivity: 10 bps per side
- Applied identically in both price-only and total runs

## No Look-Ahead Proof

1. `funding_ann[t]` is computed from hourly stamps up to close of day t
2. `z[t]` uses only data through t (rolling window)
3. `backtest_weights` shifts target_weight forward by 1 bar: position held over day t+1 is decided at t
4. Therefore: return earned is `w[t] * (price[t+1]/price[t] - 1)` — correct causal ordering

## Universe

**Full history:** BTC, ETH, SOL, AVAX, LINK, AAVE, ARB, OP, DOGE, UNI, INJ, TIA  
(MATIC excluded: HL series ends 2024-09-10 due to POL rebrand, would truncate panel)  
Inner-join on daily price bars → 2023-10-31 to 2026-05-12 (925 bars, ~2.5 years)

**Extended (shorter history):** + HYPE, ZEC  
Inner-join → 2025-11-06 to 2026-05-12 (188 bars, ~6 months). Results from this run are reported separately with explicit note on short sample.

## Benchmark

Buy-and-hold BTC. BTC correlation of strategy total returns also reported.
