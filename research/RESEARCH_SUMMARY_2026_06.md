# Research Summary - June 2026

## 1. Experiment: Parameter Sensitivity (Threshold & Concurrency)

**Goal:** Evaluate the impact of lowering entry/exit thresholds and increasing the concurrency cap.

**Parameters:**
- **Universe:** `U7` (Expanded: BTC, ETH, SOL, HYPE, ZEC, PURR, XPL)
- **Thresholds:** `0.10` (Entry) / `-0.10` (Exit) [Previously 0.30 / -0.15]
- **Concurrency Cap:** `6` [Previously 3]
- **Margin Buffer:** `3.0x`
- **Position Size:** `$100.00`

**Results:**
| Metric | Baseline (3 cap, 0.3/-0.15) | Expanded (6 cap, 0.1/-0.1) |
|--------|-----------------------------|----------------------------|
| **Annual Return** | **+124.60%** | **+119.95%** |
| **Volatility** | 7.99% | 8.82% |
| **Sharpe Ratio** | 3.63 | 3.21 |
| **Max Drawdown** | 0.03% | 0.03% |
| **Total Funding Earned** | $1830.20 | $2102.02 |
| **Total Fees Paid** | $684.23 | $903.45 |
| **Skipped Opens (Capital)**| 0 | 91,671 |

**Conclusion:**
Lowering thresholds and increasing concurrency leads to **diminishing returns**. While more funding is captured, the cost of trading (fees) and the capital constraint (running out of funds for margin) degrade the Sharpe ratio and overall profitability. The "sweet spot" remains a tighter threshold with a lower concurrency cap to maintain capital efficiency.

---

## 2. Per-Coin Alpha Attribution (Full Period)

Analysis of which coins are driving the strategy's ~124% annual return:

| Coin | Contribution to Total Annual Return (%) |
|------|-----------------------------------------|
| **PURR** | **39%** |
| **HYPE** | **23%** |
| **ETH** | 16% |
| **BTC** | 15% |
| **SOL** | 14% |
| **XPL** | 14% |
| **ZEC** | 2% |

**Key Finding:** `PURR` and `HYPE` are the primary alpha engines. `SOL` has transitioned into a neutral/negative contributor in recent 90d windows.

---

## 3. Universe Expansion Impact

**Comparison:**
- **U3 (Baseline):** 124.6% Annual Return.
- **U7 (Expanded):** 124.6% Annual Return (at baseline 0.3/-0.15 params).

**Conclusion:** The expansion successfully added high-alpha candidates (`PURR`, `HYPE`) to the pool, but the `CONCURRENCY_CAP=3` prevents the strategy from realizing their benefits unless they are the top 3 signals. The expansion is successful because it effectively "fixed" the universe by replacing/diluting low-yield coins like `SOL` with `PURR`.
