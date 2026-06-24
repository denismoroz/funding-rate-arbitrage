# XSMOM Risk-Overlay Validation

**Research-only. Production code (`src/frab/`) not touched.**

Tests whether an intra-week risk overlay on the weekly dollar-neutral XSMOM
long-short book reduces drawdown without degrading the edge. Judge: CPCV OOS
distribution + PBO (pre-committed criteria below). NOT eyeballing full-period IS equity.

---

## Setup

| Parameter | Value |
|---|---|
| Universe (frozen) | 32 coins — full live XSMOM list (see `universe.json`) |
| Cost per leg | 4.4 bps (validated real HL perp cost) |
| Weekly rebalance | `rebal_every=7` days |
| Signal | `momentum_ensemble(lookbacks=(14,21,30,45,60))` — bit-for-bit live XSMOM signal |
| Dollar-neutral | Long top tercile / short bottom tercile, equal-weight |
| Panel | 1101 days · 2023-06-08 → 2026-06-12 |
| CPCV | N\_groups=6, k=2, purge=60d, embargo=7d → 25 OOS segments |
| Selected | `baseline` (incumbent — no overlay) |
| Menu size | 31 configs (1 baseline + 6 Arm-A + 12 Arm-B + 12 Arm-C) |

---

## Frozen Universe (32 coins)

```
AAVE ADA APT ARB ATOM AVAX BCH BNB BTC CRV DOGE DOT EIGEN ENA ETH INJ
JTO JUP LINK LTC NEAR PENDLE PYTH SOL SUI TAO TRX UNI WLD XLM XRP ZRO
```

Source: live DEFAULT_XSMOM_UNIVERSE (32 coins after HMSTR+TON removed) intersected
with `research/cross_sectional/crypto/data/<COIN>_1h.csv` history → full
intersection = 32 coins. See `universe.json`.

---

## Pre-Committed Verdict Criteria (from PLAN.md — applied, not moved)

An overlay arm is **GO** only if ALL three hold vs baseline:

1. Full-period Calmar improves (or maxDD reduces), AND
2. Full-period Sharpe does NOT degrade (tolerance 0.05), AND
3. PBO < 0.5 (the IS-best cell transfers out-of-sample; the full 31-config menu
   is used for PBO so multiple testing is correctly penalised).

DSR is **informational only** — the harness calibration showed DSR wrongly fails
profitable negative-skew strategies (same note as validation\_harness/README, confirmed
on FRAB carry). A GO requires all three criteria above, not DSR.

If an improvement is only achievable through the luckiest cell in the grid AND the
PBO is high → the result is overfit → NO-GO regardless of (1)+(2).

---

## Full-Period Daily Metrics (√252 annualisation, honest)

| Config | Arm | Ann Return | Sharpe | maxDD | Calmar |
|---|---|---|---|---|---|
| **baseline** | Baseline | +32.98% | **0.96** | -25.13% | **1.31** |
| A\_vt0.10\_w20 | Arm A | +9.68% | 0.87 | -11.25% | 0.86 |
| A\_vt0.10\_w40 | Arm A | +8.68% | 0.79 | -13.15% | 0.66 |
| A\_vt0.15\_w20 | Arm A | +14.72% | 0.89 | -15.10% | 0.97 |
| A\_vt0.15\_w40 | Arm A | +13.06% | 0.80 | -18.83% | 0.69 |
| A\_vt0.20\_w20 | Arm A | +19.62% | 0.89 | -19.71% | 0.99 |
| A\_vt0.20\_w40 | Arm A | +17.42% | 0.81 | -23.85% | 0.73 |
| B\_stop-8\_sr\_non | Arm B | +1.33% | 0.04 | -50.91% | 0.03 |
| B\_stop-8\_sr\_reb | Arm B | +23.53% | 0.90 | -26.45% | 0.89 |
| B\_stop-8\_wo\_non | Arm B | +13.16% | 0.41 | -39.76% | 0.33 |
| B\_stop-8\_wo\_reb | Arm B | +23.29% | 0.86 | -36.09% | 0.65 |
| B\_stop-12\_sr\_non | Arm B | +13.68% | 0.41 | -42.58% | 0.32 |
| B\_stop-12\_sr\_reb | Arm B | +25.07% | 0.82 | -30.11% | 0.83 |
| B\_stop-12\_wo\_non | Arm B | +14.11% | 0.40 | -36.97% | 0.38 |
| B\_stop-12\_wo\_reb | Arm B | +26.44% | 0.86 | -28.04% | 0.94 |
| B\_stop-20\_sr\_non | Arm B | +10.92% | 0.32 | -42.14% | 0.26 |
| B\_stop-20\_sr\_reb | Arm B | +24.30% | 0.74 | -26.01% | 0.93 |
| B\_stop-20\_wo\_non | Arm B | +15.19% | 0.43 | -37.56% | 0.41 |
| B\_stop-20\_wo\_reb | Arm B | +27.18% | 0.82 | -26.56% | **1.02** |
| C\_tp8\_sr\_non | Arm C | +17.59% | 0.63 | -21.84% | 0.81 |
| C\_tp8\_sr\_reb | Arm C | +20.31% | 0.79 | -25.45% | 0.80 |
| C\_tp8\_wo\_non | Arm C | +19.72% | 0.68 | -32.37% | 0.61 |
| C\_tp8\_wo\_reb | Arm C | +23.45% | 0.94 | -26.80% | 0.88 |
| C\_tp12\_sr\_non | Arm C | +14.69% | 0.47 | -29.51% | 0.50 |
| C\_tp12\_sr\_reb | Arm C | +23.59% | 0.81 | -26.92% | 0.88 |
| C\_tp12\_wo\_non | Arm C | +16.69% | 0.54 | -38.82% | 0.43 |
| C\_tp12\_wo\_reb | Arm C | +25.11% | 0.89 | -25.75% | 0.97 |
| C\_tp20\_sr\_non | Arm C | +18.38% | 0.55 | -24.57% | 0.75 |
| C\_tp20\_sr\_reb | Arm C | +30.07% | 0.94 | -23.38% | **1.29** |
| C\_tp20\_wo\_non | Arm C | +11.83% | 0.35 | -36.37% | 0.33 |
| C\_tp20\_wo\_reb | Arm C | +29.06% | 0.92 | -23.72% | **1.23** |

---

## Harness Results (CPCV OOS — baseline as SELECTED)

| Metric | OOS Median | IQR lo | IQR hi |
|---|---|---|---|
| annual\_pct (hourly scale) | 0.52 | 0.35 | 0.71 |
| Sharpe (hourly scale) | 5.50 | 3.78 | 7.93 |
| maxDD (hourly scale) | 0.01% | 0.01% | 0.01% |
| Calmar (hourly scale) | 43.10 | 36.82 | 100.22 |
| Frac segments Sharpe > 0 | 100% | — | — |

Note: harness OOS numbers are on the hourly annualisation scale used by
`engine.compute_metrics` (1 element = 1 hour). Our PnL is daily, so Sharpe ~×5.9
vs the daily-correct value. SIGN and relative shape are what matter; the
full-period daily metrics table above is the √252-correct read.

| Metric | Value |
|---|---|
| PBO | **0.806** |
| DSR | 0.835 (informational) |
| OOS segments Calmar > 0 | 100% |
| OOS segments Sharpe > 0 | 100% |

---

## Per-Arm Verdict

| Arm | Best cell | Best Calmar | Baseline Calmar | Calmar improves? | Sharpe ok? | PBO ok? | **Verdict** |
|---|---|---|---|---|---|---|---|
| Arm A | A\_vt0.20\_w20 | 0.99 | 1.31 | NO | NO (0.89 vs 0.96) | NO (0.806) | **NO-GO** |
| Arm B | B\_stop-20\_wo\_reb | 1.02 | 1.31 | NO | NO (0.82 vs 0.96) | NO (0.806) | **NO-GO** |
| Arm C | C\_tp20\_sr\_reb | 1.29 | 1.31 | NO | YES (0.94 ≈ 0.96) | NO (0.806) | **NO-GO** |

**All three arms: NO-GO.** PBO = 0.806 alone is sufficient to block every arm — the
IS-best config does not reliably transfer out-of-sample. No further analysis needed
under the pre-committed criteria.

---

## Finding Narrative

### Baseline (incumbent)

The vanilla weekly XSMOM (no overlay) is a strong incumbent: full-period Sharpe 0.96,
Calmar 1.31, 100% of OOS segments positive Sharpe, 100% positive Calmar. The baseline
to beat is high.

### Arm A — Vol-Target

Vol-targeting scales down gross during high-vol regimes. On paper it should reduce
drawdown (the primary goal). In practice it reduces both the drawdown AND the return,
leaving Calmar roughly flat or worse. The best cell (A\_vt0.20\_w20) gives Calmar 0.99
vs 1.31 — worse by 24%. The Sharpe also declines (0.89 vs 0.96). Mechanism: weekly
XSMOM already has moderate drawdowns (~25%); vol-targeting during the exact weeks
that drive drawdown also misses the recovery. At higher target\_vol (0.20) the return
is higher but so is the maxDD. PBO = 0.806 means the IS-best vol-target cell is
unpredictably chosen out-of-sample.

### Arm B — Paired Stop

The paired stop (cut both a triggered leg and its paired opposite to keep
dollar-neutrality) is the user's design. It avoids the "ломает хедж" objection by
cutting in pairs. The `_reb` (re-enter next rebalance) variants substantially
outperform `_non` (skip a full extra cycle). The best B cell (B\_stop-20\_wo\_reb)
gives Calmar 1.02, still below baseline 1.31. The `_non` (reentry=none) variants are
worse because they miss recovery rallies. The whipsaw concern (PLAN.md "prior") is
confirmed: many intra-week cumulative drawdowns turn around without requiring a cut.
PBO = 0.806 — high across the full menu.

### Arm C — Paired Take-Profit

The take-profit arm shows a surprise: the `_reb` cells with P=+12/+20% do not hurt as
badly as the prior predicted, with C\_tp20\_sr\_reb at Sharpe 0.94 and Calmar 1.29 —
very close to baseline. The tight take-profit (P=+8%) cuts winners earlier and is
somewhat worse. The moderate take-profit (P=+20%) actually approaches parity with
baseline because momentum runs mean winners typically keep running PAST +20% over the
full weekly window — the +20% trigger only fires rarely, so it barely differs from
baseline. The mechanism is: the right tail of weekly momentum returns is genuinely fat
(some weeks +30%+), so a +20% take-profit fires infrequently and the cost (missing the
remaining tail) is small.

However: Calmar 1.29 < 1.31 (barely worse), and PBO = 0.806. Even the best Arm C cell
fails criterion (1) (Calmar does not IMPROVE) and fails criterion (3) (PBO > 0.5).

### The PBO = 0.806 picture

PBO of 0.806 means: in 80.6% of CSCV train/test splits, the IS-best config was NOT
the OOS-best config. The full menu of 31 configs provides a large enough playground
that winning in-sample is largely a matter of which market regime dominated that split,
not a transferable property. The IS-best winners cycle among C\_tp8\_wo\_reb, baseline,
B\_stop-8\_wo\_reb, C\_tp20\_sr\_reb — no single cell dominates, which is the fingerprint
of regime-dependent rather than structurally superior overlays.

---

## No-Look-Ahead Verification

All invariants verified explicitly in `selftest.py` (8 tests, all pass):

1. **Arm A**: `vol_target_scale` shifts the trailing vol by 1 period before applying
   the scaler. Test: perturb `base_pnl[d]` — the scaler at day `d` is unchanged
   (confirmed via test 7). Warmup days pass through unscaled (test 1).

2. **Arms B/C**: trigger evaluation uses cumulative PnL AT END of day d (the
   triggering day's own return is kept — you cannot retroactively avoid a move you
   observe only at close). The cut takes effect from day d+1. Verified by tests 2/3
   (S=-999% / P=+999% exactly reproduce baseline) and test 6 (path engine with
   unreachable stop is bit-for-bit identical to `xsec.portfolio_returns`).

3. **Signal**: `momentum_ensemble` uses `price.shift(lookback)` (past price only).
   Z-scoring is cross-sectional on each date's coins (no future coins). Same as
   verified in `signals.py` self-test.

4. **Dollar-neutrality**: after each paired cut, held book sums to 0 within 1e-9
   (test 4). For `reentry='none'`, blacklisting may reduce the number of active legs;
   the re-normalisation at the next rebalance keeps long/short legs equal in count and
   each side summing to ±1 (verified in test 4 for stop + take-profit).

5. **CPCV purge**: purge=60d = max(lookbacks) = max(14,21,30,45,60). No source bar
   for a test-day return falls inside the train window.

6. **Signals on full panel**: `momentum_ensemble` is computed ONCE on the full panel
   (seam-safe per `crypto_pkg` pattern). CPCV only selects rows. No fold-specific
   signal recomputation.

---

## Bug Fixed During Implementation

**Dollar-neutrality with `reentry="none"`**: When the blacklist from a previous window
zeroes out more longs than shorts (or vice versa), the `np.where(blacklist, 0, target)`
produces a structurally unbalanced target book (e.g. 2 shorts, 1 long → Σ = −1/3 ≠ 0).
This caused `_held_sum_path` to fail the dollar-neutrality assert.

**Fix** (`overlay.py` lines after the blacklist application): after zeroing blacklisted
coins, count long/short legs; if sizes differ, trim the excess side to `min(n_long,
n_short)` legs and re-normalise each side to sum to ±1. The selftest's transparent
reimplementation (`_held_sum_path`) received the same fix. Tests 2/3 confirm the fix
doesn't disturb the S=−999% / P=+999% invariants (the path engine with an unreachable
threshold exactly reproduces baseline).

---

## Reproduce Commands

```bash
# 1. Self-tests (must all pass first)
cd research/xsmom_risk_overlay
/path/to/.venv/bin/python selftest.py

# 2. Full harness run (~3-5 minutes)
/path/to/.venv/bin/python run_overlay.py

# Output: run_overlay.json
```

All PYTHONPATH manipulation is handled inside the scripts (same pattern as
`token_unlock/run_unlock.py`). No environment variables needed.

---

## Final Verdict

| Arm | Criterion 1 (Calmar) | Criterion 2 (Sharpe) | Criterion 3 (PBO) | **Verdict** |
|---|---|---|---|---|
| Arm A — vol-target | FAIL | FAIL | FAIL (0.806) | **NO-GO** |
| Arm B — paired stop | FAIL | FAIL | FAIL (0.806) | **NO-GO** |
| Arm C — paired take-profit | FAIL (barely) | PASS (best cell) | FAIL (0.806) | **NO-GO** |

**No overlay arm passes the pre-committed criteria.** The vanilla weekly XSMOM baseline
(Sharpe 0.96, Calmar 1.31, 100% OOS segments positive) is the correct production
configuration. Do not add an intra-week risk overlay.

The core insight: XSMOM's ~25% drawdown comes from the same market regimes (sharp
crypto reversals) that also whipsaw intra-week stops. The stops close positions right
before the reversal reverses, so the overlay buys regime exposure at the worst time.
The vol-target similarly scales down exactly when subsequent recovery would be most
valuable. The take-profit (Arm C) is less damaging than the prior expected because a
+20% intra-week threshold fires rarely on a weekly book (daily crypto vol ~3%, weekly
moves ~7% on average), so it barely differs from baseline — but it also barely helps.
PBO = 0.806 across the full 31-config menu is the definitive quantitative verdict:
no consistent out-of-sample selection.
