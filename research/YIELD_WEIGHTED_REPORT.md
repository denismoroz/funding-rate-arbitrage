# Yield-Weighted Portfolio Construction — APR vs Concentration Tradeoff

**Date:** 2026-06-05
**Script:** `research/yield_weighted_portfolio.py`
**Inputs:** interval-aware funding (CROSS_VENUE_BACKTEST_REPORT.md, corrected 2026-06-05)
+ staking yields from `research/staking/staking_inputs.csv` (conservative column).
**Params:** buffer 3×, leverage 10×, fee drag 1.5%.

---

## Per-coin gross income (funding + staking − fee drag)

| coin | venue   | funding% | staking% | income% |
|------|---------|----------|----------|---------|
| BTC  | HL      | 9.23     | 0.0      | 7.73    |
| ETH  | Aster   | 8.06     | 2.5      | 9.06    |
| SOL  | Aster   | 6.14     | 6.5      | 11.14   |
| HYPE | HL      | 19.40    | 2.2      | 20.10   |
| AVAX | Aster   | 10.49    | 4.5      | 13.49   |
| LINK | HL      | 11.21    | 0.0      | 9.71    |
| DOGE | Aster   | 7.94     | 0.0      | 6.44    |

Dual-income leaders (funding + staking combined): **HYPE 20.1%, AVAX 13.5%, SOL 11.1%**.
LINK dropped from apparent star (was 19.94% inflated Backpack) to middle-of-pack (9.71%,
no staking) after the interval-aware fix.

---

## Four portfolio construction schemes

### Scheme 1 — Equal-weight (1/7 per coin) — baseline

| metric | value |
|--------|-------|
| 100% decoupled APR | 8.54% |
| **50/50 blend APR** | **9.31%** |
| 100% unified APR | 10.09% |
| Max single-coin weight | 14.3% (any) |
| Max single-venue weight | 57.1% (Aster) |
| Effective N | 7.00 |

---

### Scheme 2 — Yield-weighted (weight ∝ decoupled occupied-APR)

Weights: HYPE 25.9% / AVAX 17.4% / SOL 14.3% / LINK 12.5% / ETH 11.7% / BTC 10.0% / DOGE 8.3%

| metric | value |
|--------|-------|
| 100% decoupled APR | 9.78% |
| **50/50 blend APR** | **10.67%** |
| 100% unified APR | 11.56% |
| Gain vs equal-weight | **+1.36 pp** (50/50 blend) |
| Max single-coin weight | 25.9% (HYPE) |
| Max single-venue weight | 51.7% (Aster) |
| Effective N | 6.11 |

---

### Scheme 3 — Top-3 concentrated (HYPE / AVAX / SOL only, 1/3 each)

| metric | value |
|--------|-------|
| 100% decoupled APR | 11.47% |
| **50/50 blend APR** | **12.51%** |
| 100% unified APR | 13.55% |
| Gain vs equal-weight | **+3.20 pp** (50/50 blend) |
| Max single-coin weight | 33.3% (HYPE/AVAX/SOL each) |
| Max single-venue weight | 66.7% (Aster: SOL+AVAX) |
| Effective N | 3.00 |

Idiosyncratic risk note: HYPE is a single young token on a single venue (HL) with
no comparable precedent. SOL+AVAX are both on Aster (single-venue failure wipes
66.7% of the portfolio). No BTC/ETH exposure means no diversification to the
macro-dominant coins.

---

### Scheme 4 — Capped-tilt (≤25% per coin, ≤50% per venue)

Weights: HYPE 25.2% / AVAX 15.4% / SOL 14.6% / LINK 13.5% / ETH 11.9% / BTC 10.9% / DOGE 8.5%

| metric | value |
|--------|-------|
| 100% decoupled APR | 9.65% |
| **50/50 blend APR** | **10.53%** |
| 100% unified APR | 11.40% |
| Gain vs equal-weight | **+1.22 pp** (50/50 blend) |
| Max single-coin weight | 25.2% (HYPE) |
| Max single-venue weight | 50.4% (Aster, effectively at cap) |
| Effective N | 6.25 |

The venue cap binds because Aster hosts ETH+SOL+AVAX+DOGE — four coins. Enforcing ≤50%
venue forces some weight back onto HL coins (BTC/LINK), which have lower income →
the cap actually costs a small amount of APR relative to pure yield-weighted.

---

## Summary comparison (WITH LST staking)

| Scheme | decoupled | 50/50 | unified | max-coin | max-venue | eff-N |
|--------|-----------|-------|---------|----------|-----------|-------|
| 1. Equal-weight | 8.54% | 9.31% | 10.09% | 14.3% | 57.1% | 7.00 |
| 2. Yield-weighted | 9.78% | **10.67%** | 11.56% | 25.9% | 51.7% | 6.11 |
| 3. Top-3 concentrated | 11.47% | **12.51%** | 13.55% | 33.3% | 66.7% | 3.00 |
| 4. Capped-tilt | 9.65% | **10.53%** | 11.40% | 25.2% | 50.4% | 6.25 |

---

## Verdict: can any SAFE tilt reach 14% occupied-APR?

**No. Not even close on a 50/50 blend.**

- The safest reasonable tilt (capped, ≤25%/coin, ≤50%/venue) reaches **10.5%**
  — only +1.2 pp over equal-weight, 3.5 pp short of 14%.
- Even naked top-3 concentration only reaches **12.5%** (50/50) or **13.6%**
  (100% unified) — still below 14%, at severe concentration cost
  (eff-N = 3, 67% on a single venue).
- To hit 14% you would need: 100% unified + top-3 concentration
  (**13.55%**, still short) or even higher leverage/lower buffer, both of which
  increase liquidation risk.

**The arithmetic is clear:** 14% requires either (a) dangerous single-venue + single-token
concentration or (b) leverage above 10× with buffer below 3×. Neither is safe.

**Practical recommendation:**
- Yield-weighted or capped-tilt scheme buys ~1.2–1.4 pp over equal-weight for
  a small and manageable concentration increase (HYPE rising from 14% → 25%,
  eff-N 7 → 6.1–6.3). This is a reasonable tilt.
- The honest ceiling for a diversified, safe portfolio is **~10.5–11% (50/50 blend)**,
  rising to ~12% only with full unified architecture and still accepting 25%
  HYPE exposure.
- HYPE is the swing factor: it alone contributes ~4 pp to the portfolio in top-3.
  If HYPE funding normalizes to market levels, the whole thesis degrades materially.
  Any real-money allocation should treat HYPE as the volatile wildcard, not the anchor.

---

## Sensitivity: what would it take to reach 14%?

| scenario | 50/50 APR |
|----------|-----------|
| Capped-tilt, lev 10× | 10.53% |
| Capped-tilt, lev 20× | 11.04% |
| Top-3, lev 10×, 50/50 | 12.51% |
| Top-3, lev 10×, 100% unified | 13.55% |
| Top-3, lev 20×, 100% unified | 14.60% |

14% requires: top-3 concentration **and** 100% unified **and** 20× leverage.
That is the most aggressive end of every dial simultaneously — not a safe operating point.
