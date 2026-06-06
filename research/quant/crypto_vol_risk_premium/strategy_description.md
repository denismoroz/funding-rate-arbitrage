# Strategy Description: Crypto Volatility Risk Premium (VRP)

## Overview

Short-volatility via a **vol-swap proxy**. This is a first-order proxy for a delta-hedged
short-straddle or short-variance position on BTC/ETH options. Real Deribit execution has
wider bid-ask, margin calls, and gamma P&L that this model does not capture — but the
tail profile and long-run sign of the edge are correctly modelled.

## Data Sources

- **Implied vol**: Deribit DVOL index — the exchange's own 30-day constant-maturity
  implied vol index (in vol points, e.g. 59 = 59% annualised). Daily, UTC.
- **Price**: Yahoo Finance daily close (BTC-USD, ETH-USD). Used only to compute
  realised volatility; not used as a signal.

## Signal

At entry date t:

    IV_t = DVOL_t / 100          # annualised decimal, e.g. 0.59

No additional signal — the plain strategy is always short. Conditional variant adds:

    SIGNAL = IV_t - RV_trailing_{t-30:t}   # current premium vs recent realised
    Entry only if SIGNAL > THRESH

Trailing RV is computed only from historical data at t — **no look-ahead**.

## Return Model (vol-swap proxy)

Forward realised vol over the 30-day holding period:

    RV_fwd_{t→t+30} = std(log_returns[t:t+30]) × sqrt(365)    # annualised

This uses **future price data** to compute RV — this is correct because the P&L of a
position opened at t is only *realised* at t+30. The **decision** at t uses only IV_t
and trailing data. There is no look-ahead in the signal.

Per-tranche raw premium (before scaling):

    VRP_raw_t = IV_t - RV_fwd_{t→t+30}           # positive = short-vol profits

Per-tranche P&L on a unit notional:

    PnL_raw_t = VRP_raw_t - COST_VOL_PTS         # subtract round-trip cost

## Sizing (VEGA_SCALE)

Position size is calibrated so the **unlevered** tranche series has an annualised vol
of approximately 15%:

    VEGA_SCALE = 0.15 / (std(PnL_raw) × sqrt(365/30))

This yields VEGA_SCALE ≈ 0.25 for BTC, 0.20 for ETH — meaning the strategy uses
roughly **0.25 notional of vega per unit of capital**, which is sub-1× leverage.

    actual_return_t = PnL_raw_t × VEGA_SCALE

## Costs

Round-trip cost subtracted from each tranche:

| Scenario    | Cost (vol pts) | Description                              |
|-------------|---------------|------------------------------------------|
| 0 vol pts   | 0%            | Zero-cost (theoretical upper bound)      |
| 2 vol pts   | 2%            | **Default** (bid-ask + hedging slippage) |
| 4 vol pts   | 4%            | Pessimistic (wider spreads, illiquidity) |

A 2-vol-pt round-trip is conservative for liquid BTC/ETH options on Deribit but not
extreme; actual costs depend on strike, expiry, and market conditions.

## Tranche Structure

**Non-overlapping** (headline stats): open a new 30-day tranche every 30 days.
Earn the full VRP at close. Cash-neutral between tranches.

**Laddered** (equity curve only): enter 1/30 notional each day, 30 tranches live.
Smoother P&L. The laddered series is presented for visual purposes; all statistical
metrics use the non-overlapping version for independence.

## Data Gap Warning

Deribit DVOL data has a gap from approximately **December 2022 to June 2023** (6 months).
The backtest skips any tranche where the exit date is >60 days from entry (i.e. where
the data gap would cause a non-30-day RV to be used). This means:

- The November 12, 2022 entry (right after FTX bankruptcy) is **excluded** from the
  backtest — the next available DVOL date is June 20, 2023.
- Paradoxically, analysis shows this tranche would likely have been **profitable**:
  DVOL was at 103% on Nov 12 while BTC's next-30-day realised vol was ~32% (markets
  calmed after the initial shock). The gap guard is conservative, not optimistic.
- **2022 tail risk is understated.** The Oct 13, 2022 tranche (which ends Nov 12,
  just as FTX was imploding) is included and shows a -4.6% loss — a realistic 2022 hit.
  But users should assume the true worst case is harsher than shown.

## Proxy Caveats

1. **Not a real vol-swap**: Real Deribit variance swaps and straddles have different
   convexity, path dependence, and margin dynamics.
2. **No delta hedging P&L**: A real short-straddle requires continuous delta hedging;
   its P&L is approximately `(IV² - RV²) × vega / 2`, not simply `IV - RV`. Our
   linear proxy understates the convexity premium and underestimates losses during
   very large moves.
3. **No margin / liquidation model**: A real short-options position faces mark-to-market
   margin calls during vol spikes; a leveraged position can be liquidated before the
   horizon.
4. **No vol-of-vol / gap risk**: The model does not capture sudden gap moves that would
   require oversized delta hedges or cause immediate losses beyond the theta collected.

## Pseudocode

```python
for t in range(0, T, H):                     # every 30 days
    IV = DVOL[t] / 100
    RV_fwd = realised_vol(price[t:t+H])      # computed at t+H
    VRP = IV - RV_fwd
    return_t = (VRP - COST_VOL_PTS) * VEGA_SCALE
    equity *= (1 + return_t)
```
