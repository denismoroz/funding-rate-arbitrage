# Cross-Venue Funding Basis — Strategy Description

## Core Idea

The same perpetual futures contract on different exchanges receives different funding rates
because each venue has an independent open-interest base and user demand.  Data shows HL
consistently pays higher funding than Binance and Bybit (BTC: +7.9 pp, ETH: +8.6 pp,
SOL: +7.4 pp annualized over 2023-06 → 2026-06).

**Trade structure:** SHORT the perp on the high-funding venue + LONG the same perp on the
low-funding venue, equal notional.  Prices cancel exactly (true delta-neutral, no spot
inventory needed).  Income = funding spread.

---

## Capital Model

| Item | Amount |
|---|---|
| Short leg margin | 1× notional |
| Long leg margin  | 1× notional |
| **Total capital** | **2× notional** |
| Return on capital | `spread_annualized / 2` |

No cross-margining between exchanges — each venue requires its own full margin.
This is the conservative, correct denominator.

---

## Cost Model

| Event | Legs | Bps/leg | Cost on notional | Cost on 2× capital |
|---|---|---|---|---|
| Entry (open both venues) | 2 | 4 | 8 bps | 4 bps |
| Exit (close both venues) | 2 | 4 | 8 bps | 4 bps |
| **Full round-trip** | **4** | **4** | **16 bps** | **8 bps** |
| Venue flip (dynamic only) | 2 | 4 | 8 bps | 4 bps |

Static variants: one round-trip at start (and theoretically at end) — effectively negligible
over a multi-year hold, charged at day 0 only.

Dynamic variant: full round-trip on each on/off transition; half round-trip on each
venue-pair flip (switching only one leg).

---

## Data Alignment & Cadence

| Venue | Raw cadence | Annualization | Daily series |
|---|---|---|---|
| Hyperliquid (HL) | Hourly stamps | × 8760 | `resample('1D').mean()` |
| Binance | Every 8 hours | × 1095 | `resample('1D').mean()` |
| Bybit | Every 8 hours | × 1095 | `resample('1D').mean()` |
| Drift | Hourly (ends 2025-01-08) | × 8760 | `resample('1D').mean()` |

Small gaps (≤ 3 days) forward-filled.  Inner join across venues used for each coin's
daily PnL series.

**Note on cadence mismatch:** HL stamps every hour; Binance/Bybit every 8 hours.
Realized capture < theoretical because:  
(a) you cannot open/close precisely at the funding stamp boundary across venues,  
(b) HL funding rate is volatile intra-day — the daily mean used here smooths out spikes
that are real but only briefly capturable.

---

## MATIC Data Quality Caveat

HL renamed its MATIC perpetual to POL in September 2024. After 2024-09-09, the HL
funding CSV records `fundingRate = 0.0` for every hour.  Meanwhile Binance and Bybit
continue quoting MATIC at or near the 10.95% cap.

Holding the static pair post-delisting would mean:
- SHORT HL MATIC (earning 0%): no longer a real position
- LONG Binance MATIC (paying ~10.95%/yr): losing money

**All MATIC data is truncated at 2024-09-10 UTC** in this backtest.

---

## No-Look-Ahead Enforcement

Signal at day **t** (funding rate observed through end of day t) → position opened at
start of day **t+1** → funding income accrued on day **t+1**.

Implementation: daily return series `net[t]` is shifted forward by 1 bar (`shift(1)`)
before summing to portfolio return.

---

## Strategy Variants

### Variant 1 — Static HL-Binance
Always short HL, long Binance, all coins, equal weight.  Rationale: HL structurally
higher funding than Binance across the full sample.

### Variant 2 — Static HL-Bybit
Identical structure vs Bybit as the long leg.

### Variant 3 — Dynamic Best-Pair
Each day per coin: short the venue with the highest funding, long the lowest.
Active only when annualized spread > `ENTRY_THRESH`.  Grid: 0%, 3%, 6%, 10%.
Venue flips are penalized with 4 bps on capital.

---

## Drift Secondary (informational)

Drift data ends 2025-01-08 and is effectively dead for live use (REST API decommissioned
2026-04-01; requires driftpy SDK).  The data shows Drift had **higher** funding than HL
for most coins (spread: HL-Drift negative for 7/9 coins), the opposite of expectations.
HL-Drift is **not a viable pair** and is reported for completeness only.
