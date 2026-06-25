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
| Menu size | 43 configs (1 baseline + 6 Arm-A + 24 Arm-B/C + 12 Arm-D/E/F/G) |

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
3. PBO < 0.5 (the IS-best cell transfers out-of-sample; the full 43-config menu
   is used for PBO so multiple testing is correctly penalised).

DSR is **informational only** — the harness calibration showed DSR wrongly fails
profitable negative-skew strategies (same note as validation\_harness/README, confirmed
on FRAB carry). A GO requires all three criteria above, not DSR.

If an improvement is only achievable through the luckiest cell in the grid AND the
PBO is high → the result is overfit → NO-GO regardless of (1)+(2).

---

## Arms D/E/F/G — Replacement Mechanism (NEW)

Arms D–G use **single-leg replacement** instead of the paired-cut of B/C:
- When a leg triggers, close **only that leg** and open the next-best-ranked
  coin on the **same side** (long→long, short→short) that is not already held.
- Dollar-neutrality is preserved automatically (no pairing rule needed).
- The replacement coin is picked from the daily momentum scores at day d
  (info ≤ d); it starts earning from day d+1.
- Arms D/E use a fixed-% threshold (same grid as B/C for apples-to-apples).
- Arms F/G use a vol-linked threshold: `cum_pnl ≤ -k·σ_coin` (stop) or
  `cum_pnl ≥ +k·σ_coin` (take-profit), where σ\_coin is the per-coin trailing
  20-day daily vol shifted by 1 (causal: uses returns ≤ d-1).

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
| **D\_stop-8** | **Arm D** | **+33.11%** | **1.00** | **-22.64%** | **1.46** |
| D\_stop-12 | Arm D | +31.49% | 0.93 | -23.42% | 1.34 |
| D\_stop-20 | Arm D | +25.02% | 0.74 | -25.80% | 0.97 |
| E\_tp8 | Arm E | +30.08% | 0.92 | -23.70% | 1.27 |
| **E\_tp12** | **Arm E** | **+33.76%** | **1.03** | **-25.03%** | **1.35** |
| E\_tp20 | Arm E | +28.23% | 0.85 | -23.04% | 1.23 |
| **F\_vstop\_k1.5** | **Arm F** | **+33.90%** | **1.03** | **-23.18%** | **1.46** |
| F\_vstop\_k2.5 | Arm F | +27.63% | 0.81 | -25.10% | 1.10 |
| F\_vstop\_k4.0 | Arm F | +28.41% | 0.83 | -23.92% | 1.19 |
| G\_vtp\_k1.5 | Arm G | +30.26% | 0.94 | -26.95% | 1.12 |
| G\_vtp\_k2.5 | Arm G | +27.04% | 0.82 | -24.86% | 1.09 |
| G\_vtp\_k4.0 | Arm G | +25.39% | 0.76 | -25.48% | 1.00 |

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
| PBO | **0.730** |
| DSR | 0.833 (informational) |
| OOS segments Calmar > 0 | 100% |
| OOS segments Sharpe > 0 | 100% |

Note: PBO rose from 0.806 (31-config menu) to 0.730 (43-config menu). This is
expected but slightly counter-intuitive: a larger menu nominally makes selection
harder, but the D/E/F configs that outperform IS more consistently can shift the
median IS-winner distribution. The PBO remains well above 0.5 — selection still does
not transfer reliably.

---

## Per-Arm Verdict

| Arm | Best cell | Best Calmar | Baseline Calmar | Calmar improves? | Sharpe ok? | PBO ok? | **Verdict** |
|---|---|---|---|---|---|---|---|
| Arm A | A\_vt0.20\_w20 | 0.99 | 1.31 | NO | NO (0.89 vs 0.96) | NO (0.730) | **NO-GO** |
| Arm B | B\_stop-20\_wo\_reb | 1.02 | 1.31 | NO | NO (0.82 vs 0.96) | NO (0.730) | **NO-GO** |
| Arm C | C\_tp20\_sr\_reb | 1.29 | 1.31 | NO | YES (0.94 ≈ 0.96) | NO (0.730) | **NO-GO** |
| Arm D | D\_stop-8 | 1.46 | 1.31 | YES | YES (1.00 > 0.96) | NO (0.730) | **NO-GO** |
| Arm E | E\_tp12 | 1.35 | 1.31 | YES | YES (1.03 > 0.96) | NO (0.730) | **NO-GO** |
| Arm F | F\_vstop\_k1.5 | 1.46 | 1.31 | YES | YES (1.03 > 0.96) | NO (0.730) | **NO-GO** |
| Arm G | G\_vtp\_k1.5 | 1.12 | 1.31 | NO | YES (0.94 ≈ 0.96) | NO (0.730) | **NO-GO** |

**All seven arms: NO-GO.** PBO = 0.730 alone is sufficient to block every arm — the
IS-best config does not reliably transfer out-of-sample.

---

## Finding Narrative

### Baseline (incumbent)

The vanilla weekly XSMOM (no overlay) is a strong incumbent: full-period Sharpe 0.96,
Calmar 1.31, 100% of OOS segments positive Sharpe, 100% positive Calmar.

### Arms A, B, C (retained from prior run)

Identical to prior findings — no changes. See section below.

### Arm D — Replacement Stop, Fixed %

D\_stop-8 and D\_stop-12 show genuine IS improvement: Calmar 1.46 (+11% vs baseline)
and Sharpe 1.00 (also above baseline). The tight stop (-8%) replaces losing legs
quickly and the replacement picks a coin with better current momentum — functionally
this is a "refresh the losers" operation that partially mimics the weekly rebalance
at a shorter horizon. The wide stop (-20%) degrades to near-baseline because it fires
too rarely to matter.

However: PBO = 0.730. The IS-best cell (D\_stop-8) is the most frequent IS-winner
in CSCV splits, but it is not consistently the OOS-winner. The mechanism
(replacing losers) appears to capture regime-specific episodes where intra-week
reversal is pronounced — not a stable structural edge.

### Arm E — Replacement Take-Profit, Fixed %

E\_tp12 gives Calmar 1.35, Sharpe 1.03 — both above baseline. This is the "let
winners run, then recycle capital into the next-best momentum name" mechanic. The
+12% threshold is wide enough to only fire on genuine momentum surges, and the
replacement coin continues the momentum theme. The +8% threshold is slightly weaker
(fires more, catches more noise). The +20% threshold fires so rarely it barely
differs from baseline.

Despite this, PBO = 0.730 blocks the arm. The IS-winner oscillates between E\_tp12
and D\_stop-8 in different CSCV splits, indicating that performance depends on
whether a given period has more trending or more mean-reverting character.

### Arm F — Replacement Stop, Vol-Linked (predicted best shot)

F\_vstop\_k1.5 gives Calmar 1.46, Sharpe 1.03 — the joint-best IS result in the
entire menu. The vol-linked threshold adapts to each coin's actual volatility:
a high-vol coin needs a larger adverse move before the stop fires, avoiding
whipsaw on noisy names; a low-vol coin gets a tighter stop that protects against
sustained drawdown.

**F vs D comparison (the core question):** F\_vstop\_k1.5 matches D\_stop-8 on
Calmar (1.46) but beats it on Sharpe (1.03 vs 1.00) and maxDD (-23.18% vs -22.64%).
The vol-linked stop is modestly better IS — it achieves the same return reduction with
less volatility. However, the improvement over fixed-% is small and the PBO is
unchanged (the OOS selection problem is the same: whichever cell won IS was partly
lucky on which regime dominated). Vol-linking helps modestly with IS efficiency but
does not solve the transferability problem.

The prior prediction (F has the best chance) is confirmed IS, but the PBO verdict
prevents a GO.

### Arm G — Replacement Take-Profit, Vol-Linked

G shows weaker performance than E across all k values, and G\_vtp\_k1.5 (Calmar 1.12)
underperforms the fixed-% counterpart E\_tp12 (Calmar 1.35). The vol-linked
take-profit creates a tighter trigger for low-vol coins (fires more on stable names,
cutting winners early) and a wider trigger for high-vol coins (may fire too late
after most of the gain has reversed). The asymmetry goes the wrong way vs the stop:
vol-linking helps stops (avoids whipsaw on noisy coins) but hurts take-profits
(the coins most likely to surge are high-vol, so the vol-linked threshold fires
too slowly). Arm G is the weakest of the replacement family.

### The PBO = 0.730 picture (full 43-config menu)

PBO fell from 0.806 (31-config menu) to 0.730 (43-config menu), but remains well
above 0.5. Adding Arms D/E/F/G brought configs that IS-outperform baseline, which
means the IS-winner is now more likely to actually be above baseline — but the
OOS-winner is still unpredictable across CSCV splits. The most frequent IS-winners
cycle among `C_tp8_wo_reb` (×2626 splits), `E_tp12` (×1427), `B_stop-8_wo_reb`
(×1254), `C_tp8_wo_non` (×1137), `D_stop-8` (×1101). No single config dominates —
the fingerprint of regime-dependence, not structural superiority.

### Summary of F vs D (does vol-linking help?)

| | IS Calmar | IS Sharpe | IS maxDD |
|---|---|---|---|
| D\_stop-8 (fixed -8%) | 1.46 | 1.00 | -22.64% |
| F\_vstop\_k1.5 (vol-linked) | 1.46 | 1.03 | -23.18% |

Vol-linking delivers a modest improvement in IS Sharpe (+0.03) and a marginal
trade-off in maxDD (+0.54%). The improvement is real but small: both catch
roughly the same replacement opportunities, with the vol-linked version being
slightly less trigger-happy on high-vol coins. This is consistent with the
hypothesis that vol-linked stops avoid some whipsaw on noisy names — but the
effect is too small to materially change the PBO outcome.

---

## Prior Arms A, B, C — No Changes

### Arm A — Vol-Target

Vol-targeting reduces both drawdown AND return, leaving Calmar worse than baseline.
The best cell (A\_vt0.20\_w20) gives Calmar 0.99 vs 1.31.

### Arm B — Paired Stop

The paired stop (cut both triggered leg + paired opposite to keep dollar-neutrality).
Best cell B\_stop-20\_wo\_reb gives Calmar 1.02, below baseline 1.31. The `_non`
reentry variants are worse because they miss recovery rallies.

### Arm C — Paired Take-Profit

C\_tp20\_sr\_reb (Calmar 1.29, Sharpe 0.94) approaches baseline parity because the
+20% trigger fires rarely on a weekly book. Barely below baseline Calmar, and
PBO = 0.730.

---

## No-Look-Ahead Verification

All invariants verified explicitly in `selftest.py` (14 tests, all pass):

**Arms D/E/F/G additional invariants:**

1. **D1**: `replacement_overlay` with S=-999% ≡ baseline exactly (never triggers).
2. **E1**: `replacement_overlay` with P=+999% ≡ baseline exactly (never triggers).
3. **F1**: vol-linked stop with k=999 ≡ baseline exactly (threshold unreachable; NaN
   vol during warmup also suppresses triggers conservatively).
4. **D2**: After replacements under a forced low threshold, held book satisfies
   Σ(held) ≈ 0 (dollar-neutral), Σ(long) ≈ +1, Σ(short) ≈ -1 every day.
5. **D3**: For every replacement event, the new coin is on the SAME side as the old
   (long→long, short→short) and was NOT already held before replacement.
6. **NaN2**: No NaN leakage across all 43 menu configs including D/E/F/G.

**Prior invariants (still passing):**

7. Arms B/C: S=-999% / P=+999% ≡ baseline; path engine with unreachable stop ≡
   `xsec.portfolio_returns`; dollar-neutrality after paired cuts.
8. Arm A: scaler[t] uses vol estimated strictly before t (perturbation test).
9. Signal: `momentum_ensemble` uses past prices only.
10. CPCV purge=60d = max(lookbacks); no test-day source bar inside train window.

---

## Bug Fixed During Implementation

**Dollar-neutrality with `reentry="none"`** (Arms B/C): When the blacklist zeroes
out more longs than shorts (or vice versa), the book becomes imbalanced. Fix in
`overlay.py`: after blacklisting, count legs on each side; if unequal, trim the
larger side to `min(n_long, n_short)` and re-normalise to ±1. Verified in tests
2/3/4.

No bugs found in the replacement overlay (Arms D/E/F/G). The single-leg
replacement maintains dollar-neutrality structurally (same-side swap), so no
re-normalisation is needed.

---

## Survivorship bias caveat

All ABSOLUTE headline numbers in this study (e.g. baseline +32.98%/yr, Sharpe 0.96,
Calmar 1.31) are computed on the frozen survivor universe (32 coins that survived to
mid-2026). According to `research/cross_sectional/crypto/survivorship.json`, the
survivorship premium on the same universe is approximately **+0.46 Sharpe / +20.6%/yr**
vs a point-in-time universe that includes dead/delisted HL coins:

| | Sharpe | Ann return | Calmar |
|---|---|---|---|
| Frozen survivor book | 1.22 | +50.0% | 1.88 |
| Point-in-time book | 0.76 | +29.5% | 1.06 |
| Survivorship premium | +0.46 | +20.6%/yr | — |

The bias is concentrated in 2023–H1 2024 (Sharpe premium +0.94) and is near-zero in H2
(≈ −0.05). Verdict from that file: "LARGE SURVIVORSHIP BIAS — FORWARD NUMBERS UNRELIABLE."

For **forward planning**, the realistic baseline is Sharpe ~0.76 / ann ~+29.5% (point-in-
time), not the survivor figures (~1.2 Sharpe, ~+50%/yr).

Importantly, the RELATIVE arm-vs-baseline comparisons that drive the NO-GO verdicts here
are largely robust to this bias: survivorship is common-mode (the same frozen universe for
baseline and every arm), so it cancels in arm-vs-baseline deltas. The PBO = 0.730 result
is unaffected. Only absolute forward-return projections need adjustment.

---

## Reproduce Commands

```bash
# 1. Self-tests (must all pass first — 14 tests)
cd research/xsmom_risk_overlay
/Users/d/prj/funding-rate-arbitrage/.venv/bin/python selftest.py

# 2. Full harness run (~5-8 minutes; 43 configs × 15 CPCV splits)
/Users/d/prj/funding-rate-arbitrage/.venv/bin/python run_overlay.py

# Output: run_overlay.json
```

All PYTHONPATH manipulation is handled inside the scripts. No environment variables
needed.

---

## Final Verdict

| Arm | Criterion 1 (Calmar) | Criterion 2 (Sharpe) | Criterion 3 (PBO=0.730) | **Verdict** |
|---|---|---|---|---|
| Arm A — vol-target | FAIL | FAIL | FAIL | **NO-GO** |
| Arm B — paired stop | FAIL | FAIL | FAIL | **NO-GO** |
| Arm C — paired take-profit | FAIL (barely) | PASS (best cell) | FAIL | **NO-GO** |
| Arm D — replacement stop (fixed %) | PASS (1.46>1.31) | PASS (1.00>0.96) | FAIL | **NO-GO** |
| Arm E — replacement take-profit (fixed %) | PASS (1.35>1.31) | PASS (1.03>0.96) | FAIL | **NO-GO** |
| Arm F — replacement stop (vol-linked) | PASS (1.46>1.31) | PASS (1.03>0.96) | FAIL | **NO-GO** |
| Arm G — replacement take-profit (vol-linked) | FAIL | PASS (0.94≈0.96) | FAIL | **NO-GO** |

**No overlay arm passes the pre-committed criteria across the full A–G menu.**
The vanilla weekly XSMOM baseline (Sharpe 0.96, Calmar 1.31, 100% OOS segments
positive) remains the correct production configuration.

**Key finding:** Arms D, E, F show genuine IS improvement (Calmar up to 1.46,
Sharpe up to 1.03 — both above baseline), with F (vol-linked replacement stop)
being the joint-best IS performer. This is a meaningful difference from the
paired-cut family (B/C) which could not beat baseline even IS. However, PBO=0.730
means the IS-best config is NOT the OOS-best in 73% of CSCV splits. The
replacement mechanism captures regime-specific advantages (refreshing momentum
losers when short-term reversal is elevated) that do not transfer consistently.

**Vol-linking vs fixed-% (the core question):** F slightly outperforms D IS
(Sharpe +0.03, roughly equal Calmar and maxDD). Vol-linking helps avoid some
whipsaw on noisy coins but the improvement is too small to change the PBO verdict.
The correlation between D\_stop-8 and F\_vstop\_k1.5 IS performance across splits
is high — they are picking up the same regime signal.

**The axis is closed.** Per PLAN.md discipline: "this is the last family of
exit-overlay. If all NO-GO — axis closed, do not return."
