# Sweep Validated — June 2026

**Data window**: U3-new uses 2023-06-08→2026-05-12 (~2.9 yr); U4/U5/U7 are constrained to HYPE/PURR/XPL OHLCV availability: 2025-11-06→2026-05-12 (~6.2 months). Annual returns are time-normalized but the shorter window means U4+ numbers carry higher estimation noise.

---

## 1. Bug Fix Summary

**Bug**: In `simulate_portfolio` (lines 379-380), the parameter overrides were written as `MIN_HOLD_MS = min_hold_hours` and `SIGNAL_WINDOW_MS = signal_window_hours` — local variables with wrong names, leaving the module-level `MIN_HOLD_HOURS` and `SIGNAL_WINDOW_HOURS` globals unchanged.

**Fix**: Renamed to `MIN_HOLD_HOURS = min_hold_hours` and `SIGNAL_WINDOW_HOURS = signal_window_hours`.

**Verification**: Smoke test confirmed fix works.
```
m1 = simulate_portfolio(min_hold_hours=24, ...)   → total_funding=1159.20
m2 = simulate_portfolio(min_hold_hours=720, ...)  → total_funding=761.46
assert m1 != m2  → PASS
```

**Module-level defaults** (changed by prior agent, not reverted):
- `CONCURRENCY_CAP = 6` (was 3)
- `ENTRY_THRESHOLD = 0.10` (was 0.30)
- `EXIT_THRESHOLD = -0.10` (was -0.15)

Both configs below pass explicit overrides so the module defaults do not affect sweep results.

---

## 2. Top 3 Configurations by Sharpe

**Config A — research baseline** (entry=0.30, exit=-0.15, min_hold=120h)

| Universe | buf | sz  | K | Annual% | Sharpe | MaxDD% | Liq |
|----------|-----|-----|---|---------|--------|--------|-----|
| U3-new   | 5.0 | 100 | 3 | +72.7%  | 3.86   | 0.017  | 0   |
| U3-new   | 5.0 | 150 | 3 | +109.0% | 3.81   | 0.021  | 0   |
| U3-new   | 3.0 | 150 | 3 | -2.6%   | 0.22   | 53.0   | 1   |
| U4       | 3.0 | 150 | 3 | +4.5%   | 1.41   | 0.016  | 0   |
| U4       | 3.0 | 100 | 3 | +3.0%   | 1.41   | 0.010  | 0   |
| U4       | 5.0 | 100 | 3 | +22.9%  | 1.41   | 0.016  | 0   |
| U5       | 3.0 | 100 | 3 | +15.1%  | 3.15   | 0.010  | 0   |
| U5       | 3.0 | 150 | 3 | +22.6%  | 3.14   | 0.016  | 0   |
| U5       | 5.0 | 100 | 3 | +22.6%  | 3.13   | 0.016  | 0   |
| U7       | 3.0 | 100 | 3 | +69.1%  | 4.17   | 0.038  | 0   |
| U7       | 5.0 | 100 | 3 | +81.0%  | 3.68   | 0.021  | 0   |
| U7       | 5.0 | 150 | 3 | +74.5%  | 3.22   | 0.030  | 0   |

Note: U3-new buf=3 liquidates (lost 2.9yr-era spike); buf=5 is safe. U4 is weak under Config A because the high entry threshold (0.30) rarely fires on the 6-month HYPE data.

**Config B — live prod** (entry=0.10, exit=-0.10, min_hold=120h)

| Universe | buf | sz  | K | Annual% | Sharpe | MaxDD% | Liq |
|----------|-----|-----|---|---------|--------|--------|-----|
| U3-new   | 3.0 | 100 | 3 | +135.2% | 6.28   | 0.027  | 0   |
| U3-new   | 3.0 | 100 | 5 | +135.2% | 6.28   | 0.027  | 0   |
| U3-new   | 3.0 | 150 | 3 | +184.0% | 5.92   | 0.039  | 0   |
| U4       | 3.0 | 100 | 5 | +39.9%  | 6.30   | 0.024  | 0   |
| U4       | 5.0 | 100 | 5 | +61.1%  | 6.09   | 0.022  | 0   |
| U4       | 3.0 | 150 | 5 | +50.1%  | 5.77   | 0.022  | 0   |
| U5       | 3.0 | 100 | 5 | +63.5%  | 7.00   | 0.023  | 0   |
| U5       | 3.0 | 100 | 3 | +45.5%  | 6.06   | 0.026  | 0   |
| U5       | 5.0 | 100 | 5 | +75.2%  | 5.84   | 0.021  | 0   |
| U7       | 3.0 | 100 | 5 | +61.8%  | 5.29   | 0.010  | 0   |
| U7       | 3.0 | 100 | 3 | +67.1%  | 4.65   | 0.049  | 0   |
| U7       | 5.0 | 100 | 3 | +71.5%  | 4.10   | 0.010  | 0   |

---

## 3. Per-Coin Attribution — Best U7 Config B (buf=3, sz=100, K=5)

Annual: +61.8% | Sharpe: 5.29 | MaxDD: 0.010%

| Coin | n_opens | funding_gross | fees_paid | realized_pnl | hours_in | net_pnl/hr |
|------|---------|--------------|-----------|--------------|----------|------------|
| XPL  | 7       | $3.40        | $1.35     | $114.88      | 3370     | $0.0347    |
| HYPE | 3       | $2.88        | $0.61     | $14.30       | 2904     | $0.0057    |
| PURR | 1       | $2.60        | $0.14     | $62.83       | 1025     | $0.0637    |
| ZEC  | 6       | $2.24        | $1.18     | -$25.48      | 2049     | -$0.0119   |
| ETH  | 1       | $1.82        | $0.18     | $29.92       | 2081     | $0.0152    |
| BTC  | 4       | $1.77        | $0.79     | $44.46       | 2692     | $0.0169    |
| SOL  | 1       | $0.47        | $0.21     | -$2.15       | 541      | -$0.0035   |

Observations: XPL and PURR dominate `net_pnl/hr` despite small funding contributions — their `realized_pnl` (MTM at close) is large relative to hours held. **ZEC and SOL are net negative** on net_pnl/hr; they consume capital and margin buffer without positive contribution. Funding_gross across all 7 coins is tiny ($15 total) relative to realized_pnl ($239 total), meaning performance is driven by spot+perp price convergence at close, not funding harvest.

---

## 4. Does Adding HYPE/PURR/ZEC/XPL Improve Risk-Adjusted Performance?

**Honest answer: no, not clearly, and the comparison is invalid anyway.** U3-new is measured over 2.9 years; U4/U5/U7 over just 6.2 months. The U3 Config B Sharpe of 6.28 covers multiple market regimes; U5's "best" Sharpe of 7.00 covers one 6-month bullrun period (Nov 2025–May 2026) during which HYPE and XPL had exceptional price appreciation that inflated realized_pnl at close. That is not funding alpha — it is spot long exposure at close. Under Config A (higher threshold), U4 barely fires (Sharpe 1.41) and U5 is modest (3.15 vs U3's 3.86). The PURR "alpha" is literally one trade in the best U7 Config B run: 1 open, $62.83 realized_pnl in 1025 hours, which is a $62 spot position appreciation over 6 weeks — not repeatable funding yield. Until we have 12+ months of comparable HYPE/PURR/XPL data across at least one bear cycle, expanding beyond U3 is speculation dressed as optimization.

---

## 5. Caveats

1. **Operation-order change** (`apply_margin_policy` now runs AFTER entries, not before): the research backtester reflects this order (`accrue → exits → entries → margin_policy`). This means margin checks happen one tick late — a position that would have been force-closed before opening a new one won't be. Effect is small at low leverage but worth reverting to `accrue → margin_policy → exits → entries` to match safer live logic. **No code was changed here; flagging for decision.**

2. **PURR maint_ratio**: the backtester uses `DEFAULT_MAINT_RATIO` fallback of 0.025 for PURR. Actual HL maintenance margin for PURR may differ (PURR is a low-liquidity perp). Research results will diverge from live if PURR's actual maint margin is higher. Prod already has correct values; research does not.

3. **OHLCV for HYPE/PURR/XPL** was missing full history; fetched from HL API (Nov 2025 onwards). ZEC OHLCV comes from a separate HL fetch (Oct 2025 onwards). All data is now in `research/data/`.

4. **Prior RESEARCH_SUMMARY_2026_06.md numbers** (124% annual, $1830 funding) used the old module-level defaults with the `min_hold` bug present. Those numbers are unreliable and should be considered superseded by this sweep.
