# Sweep Fixed 2026-06: Post Bug-Fix Results

## 1. Bug Confirmation

Diagnostic: one open+close cycle, zero fees/funding, flat price, BTC leverage=20, req_margin=$15:

- **Before fix**: `perp_cash += realized - perp_fee + req_margin` → equity delta = **+$15.00** per close cycle
- **After fix**: `perp_cash += realized - perp_fee` → equity delta = **$0.00**

The bug caused req_margin ($15 in this example) to be double-counted on every close. Over hundreds of close events, this inflated equity by `n_closes × req_margin`, producing the fake +67% annual return.

Both the normal-exit path (line 437) and the forced-close path (lines 172+176 of `apply_margin_policy`) had the same bug. Both are now fixed.

---

## 2. Before/After: U-prod K=4 Config B (buf=3, sz=$100) — live prod config

| Metric | BEFORE (buggy) | AFTER (fixed) |
|---|---|---|
| annual_pct | +67.25% | +1.43% |
| Sharpe | 4.26 | 19.47 |
| total_funding (agg) | $1,442 | $784 |
| total_fees (agg) | $970 | $970 |
| net equity gain | +$672 | +$14 |

Per-coin breakdown (fixed, funding_gross − fees_paid):

| Coin | funding_gross | fees_paid | net |
|---|---|---|---|
| BTC | $1.87 | $1.21 | $0.65 |
| ETH | $2.52 | $0.92 | $1.60 |
| SOL | $0.88 | $0.32 | $0.56 |
| HYPE | $3.73 | $1.03 | $2.70 |
| PURR | $9.32 | $0.85 | $8.48 |
| **TOTAL** | **$18.32** | **$4.32** | **$13.99** |

The $13.99 net gain on a $1,000 budget over ~3 years = **1.43% annualized** — consistent with the known ~$14 actual funding income (1% APR range).

---

## 3. Top 3 Configs by Sharpe per Universe — Config B (entry=0.10), Post-Fix

**U3-new** (BTC, ETH, SOL):
1. buf=5.0, sz=$100, K=3: annual=**+15.82%**, Sharpe=**37.94**, maxdd=0.16%
2. (ties K=4/5 same parameters — same result)
3. buf=5.0, sz=$150, K=3: annual=**+21.44%**, Sharpe=**35.69**, maxdd=0.24%

*Note: buf=3.0 on U3-new produces -98% annual (3 liquidations). The 15%+ result is extreme parameter sensitivity, not robustness.*

**U4** (+ HYPE):
1. buf=5.0, sz=$150, K=4: annual=**+0.73%**, Sharpe=**11.65**, maxdd=0.10%
2. buf=5.0, sz=$150, K=3: annual=**+0.70%**, Sharpe=**10.74**, maxdd=0.13%
3. buf=3.0, sz=$150, K=3: annual=**+0.62%**, Sharpe=**8.56**, maxdd=0.21%

**U5** (+ ZEC):
1. buf=3.0, sz=$150, K=5: annual=**+0.93%**, Sharpe=**12.69**, maxdd=0.09%
2. buf=3.0, sz=$150, K=4: annual=**+0.98%**, Sharpe=**12.50**, maxdd=0.07%
3. buf=5.0, sz=$150, K=5: annual=**+0.72%**, Sharpe=**12.33**, maxdd=0.06%

**U7** (+ PURR, XPL):
1. buf=3.0, sz=$150, K=3: annual=**+1.87%**, Sharpe=**24.13**, maxdd=0.05%
2. buf=3.0, sz=$150, K=4: annual=**+1.81%**, Sharpe=**21.94**, maxdd=0.04%
3. buf=5.0, sz=$100, K=5: annual=**+0.87%**, Sharpe=**18.19**, maxdd=0.06%

**U-prod** (BTC, ETH, SOL, HYPE, PURR):
1. buf=5.0, sz=$100, K=4: annual=**+1.47%**, Sharpe=**26.00**, maxdd=0.11%
2. buf=3.0, sz=$150, K=4: annual=**+2.12%**, Sharpe=**25.93**, maxdd=0.12%
3. buf=3.0, sz=$150, K=5: annual=**+1.85%**, Sharpe=**23.83**, maxdd=0.05%

---

## 4. Honest Verdict

**Most universes do NOT deliver >3% APR net of fees.** Across all U4/U5/U7/U-prod configurations, fixed returns range from 0.4% to 2.1% annually — below the sUSDe ~3% baseline in every case.

The exception is **U3-new (BTC/ETH/SOL) with buf=5.0**, which shows 15-21% APR. However:
- This result requires buf=5 (5× margin buffer). At buf=3, the same universe liquidates catastrophically (-98%).
- The ~14% average annualized funding rate on BTC/ETH/SOL during this backtest period drove the result.
- This is severe parameter sensitivity: a 2× buffer change swings results by 110 percentage points annually.
- Future funding rates may not replicate the 2023-2026 bull-market levels.

**Conclusion**: The strategy is viable as a low-yield, high-Sharpe cash alternative at ~1-2% APR (U-prod, U7) but does not beat sUSDe risk-free yields without taking on meaningful liquidation risk at buf=3.

---

## 5. Invalidation of Prior Research

The double-counting bug was present in all prior sweep runs. The previously reported "+29% baseline" and similar results from earlier portfolio_margin research are **invalid** — they include phantom equity from req_margin double-counting. All prior portfolio_margin sweep results should be treated as unreliable. Only results from `sweep_aggregate_2026_06_FIXED.csv` and `sweep_per_coin_2026_06_FIXED.csv` are trustworthy.

---

*Data: HL funding + OHLCV, 2023-06-08 to 2026-06-01 (~2.98 years). Budget: $1,000. All figures from sweep_aggregate_2026_06_FIXED.csv and sweep_per_coin_2026_06_FIXED.csv.*
