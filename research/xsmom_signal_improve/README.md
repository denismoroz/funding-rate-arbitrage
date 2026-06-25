# XSMOM Signal-Improvement Validation

**Research only. Prod `src/frab/` untouched.**

Incumbent: vanilla XSMOM — momentum ensemble (z-score, lookbacks 14/21/30/45/60 days),
top/bottom tercile dollar-neutral, weekly rebalance, 1× leverage, 4.4 bps/leg.

Five pre-registered signal/structure arms validated through the harness (CPCV + PBO + DSR).

---

## Frozen universe

32 coins (live XSMOM list, HMSTR/TON excluded per chore 4ea6710):
AAVE, ADA, APT, ARB, ATOM, AVAX, BCH, BNB, BTC, CRV, DOGE, DOT, EIGEN, ENA, ETH,
INJ, JTO, JUP, LINK, LTC, NEAR, PENDLE, PYTH, SOL, SUI, TAO, TRX, UNI, WLD, XLM, XRP, ZRO.

Panel: 1101 days (2023-06-08 → 2026-06-12).

---

## Results table

Full-period daily √252 metrics. OOS from harness (CPCV 6 groups, k=2, purge=67d, embargo=7d).

| Config       | Arm       | Ann return | OOS Sharpe* | OOS maxDD* | OOS Calmar* | frac>0 | VERDICT  |
|--------------|-----------|-----------|-------------|-----------|-------------|--------|----------|
| baseline     | —         | +33.0%    | 5.50        | −0.01%    | 43.10       | 100%   | BASELINE |
| R_sharpe     | Arm R     | +33.5%    | (full: 1.06) | −27.8% | 1.21        | 50.6%  | NO-GO    |
| R_tstat      | Arm R     | +33.5%    | (full: 1.06) | −27.8% | 1.21        | 50.6%  | NO-GO    |
| G_gap3       | Arm G     | +26.6%    | (full: 0.73) | −33.8% | 0.79        | 48.3%  | NO-GO    |
| G_gap5       | Arm G     | +20.9%    | (full: 0.59) | −41.8% | 0.50        | 49.0%  | NO-GO    |
| G_gap7       | Arm G     | +15.6%    | (full: 0.47) | −45.3% | 0.34        | 48.0%  | NO-GO    |
| K_rank       | Arm K     | +37.4%    | (full: 1.11) | −23.2% | 1.61        | 49.8%  | NO-GO    |
| T_trend30    | Arm T     | +30.0%    | (full: 0.73) | −48.9% | 0.61        | 43.3%  | NO-GO    |
| T_trend60    | Arm T     | +24.8%    | (full: 0.53) | −48.6% | 0.51        | 44.5%  | NO-GO    |
| B_frac2in10  | Arm B     | +46.0%    | (full: 1.05) | −37.2% | 1.24        | 50.0%  | NO-GO    |
| B_frac3in10  | Arm B     | +33.0%    | (full: 0.96) | −25.1% | 1.31        | 49.8%  | NO-GO    |
| B_frac5in10  | Arm B     | +21.5%    | (full: 0.81) | −25.3% | 0.85        | 50.0%  | NO-GO    |

*OOS Sharpe/Calmar/maxDD are on harness hourly scale (1 period = 1 hour assumed by engine);
full-period (full:) metrics are daily √252-correct. SELECTED = baseline; OOS distribution
reports baseline's 15-split CPCV distribution.

**Menu-wide PBO = 0.534** (panel end 2026-06-12; straddling the 0.5 threshold — see REPRODUCIBILITY note below).
**DSR = 0.886** (informational).
OOS baseline: median Sharpe 5.50 (hourly scale), 100% segments positive.

---

## Arm-level verdicts

Pre-committed criteria (PLAN.md): GO only if vs baseline:
(1) full-period Calmar improves, AND (2) Sharpe does not degrade (tol 0.05), AND (3) PBO < 0.5.

| Arm   | Best cell    | Calmar>base? | Sharpe ok? | PBO ok? | Verdict |
|-------|-------------|-------------|-----------|---------|---------|
| Arm R | R_sharpe    | NO (1.21 vs 1.31) | YES (1.06 vs 0.96) | NO (0.534) | **NO-GO** |
| Arm G | G_gap3      | NO (0.79 vs 1.31) | NO (0.73 vs 0.96)  | NO (0.534) | **NO-GO** |
| Arm K | K_rank      | YES (1.61 vs 1.31)| YES (1.11 vs 0.96) | NO (0.534) | **NO-GO** |
| Arm T | T_trend30   | NO (0.61 vs 1.31) | NO (0.73 vs 0.96)  | NO (0.534) | **NO-GO** |
| Arm B | B_frac3in10 | NO (1.31 = 1.31)  | YES (0.96 = 0.96)  | NO (0.534) | **NO-GO** |

**Test INCONCLUSIVE at the data-power floor.** PBO = 0.534 (panel end 2026-06-12) straddles the
0.5 threshold. Re-running with ~13 more days of data (panel end 2026-06-25) gives PBO = 0.484,
which would flip K_rank to GO. A binary PBO = 0.5 cutoff is not a stable final word when the
menu consists of near-identical twin configs whose PBO structurally sits at ~0.5 (see
GRAVEYARD_LESSONS режим 1). No arm demonstrated a ROBUST improvement; but "no robust
improvement shown" is NOT equivalent to "definitively dead."

K_rank is the only arm that passed criteria (1) and (2); its GO/NO-GO verdict flips with the
data window, making it a data-dependent near-miss rather than a clear decision.

---

## Key findings

**Arm R (risk-adjusted momentum):** Sharpe slightly better (1.06 vs 0.96) but Calmar
slightly worse (1.21 vs 1.31 — slightly deeper drawdown). R_sharpe and R_tstat are
identical in this dataset (Sharpe and t-stat differ only by √n scaling, which washes out
after cross-sectional z-scoring). Literature support is real but the magnitude in this
universe is negligible. **NO-GO** — no meaningful improvement.

**Arm G (skip-recent gap):** Monotone decline as gap increases. G_gap3 already has lower
Sharpe (0.73) and lower Calmar (0.79) than baseline. Contrary to the medium-prior
hypothesis: short-term reversal contamination is not a significant problem in this weekly
rebalancing regime (weekly rebalance already buffers the 1-day reversal; gapping 3-7 days
removes signal with no compensation). **NO-GO** — worse in-sample, no case to go further.

**Arm K (rank-based / percentile):** Looks best — Sharpe 1.11, Calmar 1.61, maxDD −23.2%
vs baseline −25.1%. The improvement is economically plausible (rank is robust to the
single-coin outlier distorting z-score). PBO = 0.534 (panel end 2026-06-12): in CSCV splits
K_rank and B_frac2in10 alternate as IS-best, and neither dominates OOS. This is the
hallmark of high PBO — the "best" config is not stable across folds.

**INCONCLUSIVE — data-dependent near-miss.** The GO/NO-GO verdict for K_rank flips
with the data window (PBO 0.534 → 0.484 with +13 days). The correct next step is a
PRE-REGISTERED single-config OOS test of K_rank alone (not a menu-wide PBO comparison
against twin configs), or simply more live data — NOT declaring it definitively dead.
Criteria (1) and (2) pass; the PBO signal is that the data-power floor has been reached,
not that the idea is bad.

**Arm T (TS×XS gate):** Significant drawdown expansion to −49% (trend gates wipe out
the book on bearish days — goes flat, which is fine individually, but this disrupts
the cross-sectional pairing in down-trending markets). Standalone trend is dead per prior
research; overlaying it on XS merely reduces breadth and creates timing gaps. **NO-GO** —
strictly worse on both Calmar and Sharpe. Consistent with the prior result
(project_trend_following: trend alone NO-GO, corr with XSMOM +0.40).

**Arm B (breadth):** Concentration (frac=1/5, B_frac2in10) has higher gross Ann (+46%)
but higher drawdown (−37%) and lower Calmar (1.24 vs 1.31). Breadth (frac=1/2,
B_frac5in10) has lower return AND lower Calmar. The baseline frac=1/3 is Pareto-dominant
on the Calmar metric. **NO-GO** — baseline tercile is the best breadth point.

---

## Literature priors vs outcomes

- **R (risk-adjusted):** Medium prior (yes, t-stat signals work in equities). Here:
  marginal improvement insufficient to overcome PBO > 0.5. Verdict confirmed: weak.
- **G (skip-gap):** Medium prior (reversal correction). Here: _worse_ at all gaps in a
  weekly book. The weekly cadence already sidesteps the daily reversal. Verdict: NO.
- **K, T, B:** Weak priors. K is the most interesting near-miss (criteria 1+2 pass, PBO
  knife-edge — see INCONCLUSIVE note above). T and B are clear underperformers.

Overall conclusion: internal XSMOM signal tuning has not produced a ROBUST, transferable
improvement on the current data window. No arm clears all three pre-committed gates
simultaneously and reproducibly. This is not the same as "axis exhausted / all ideas dead":
it means the data-power floor has been reached and a binary PBO gate on a menu of
near-identical configs cannot distinguish marginal improvements from noise at this sample
size. Meaningful next steps: (a) pre-registered single-config OOS test of K_rank alone,
(b) risk-parity blend of FRAB (carry) + XSMOM (momentum) on live data at ~2026-07-16
checkpoint.

---

## REPRODUCIBILITY — panel end date is not pinned

The panel in `run_improve.json` ends 2026-06-12 (1101 days). Re-running `run_improve.py`
today fetches live price data and extends the panel; the PBO is deterministic given the
data but WILL change as more days are added. With ~13 additional days (panel end 2026-06-25,
1114 days) the PBO falls from 0.534 to 0.484, flipping the K_rank verdict from NO-GO to GO.

Consequence: `run_improve.json` is NOT a stable record — it regenerates with live data.
Anyone citing the verdict should report PBO together with its panel end date. To produce
a stable, citable record, pin the panel window (e.g. `panel_end="2026-06-12"`) before
re-running, or treat the committed `run_improve.json` as the canonical snapshot for the
stated panel (2026-06-12).

---

## Survivorship bias caveat

All ABSOLUTE headline numbers in this study (e.g. baseline +33%/yr, Calmar 1.31) are
computed on the frozen survivor universe (32 coins that survived to mid-2026). According
to `research/cross_sectional/crypto/survivorship.json`, the survivorship premium on this
same universe is approximately **+0.46 Sharpe / +20.6%/yr** vs a point-in-time universe
that includes dead/delisted HL coins (survivor book: Sharpe 1.22 / +50%/yr; point-in-time:
Sharpe 0.76 / +29.5%/yr). The bias is concentrated in 2023–H1 2024 (h1 Sharpe premium
+0.94) and is near-zero in H2 (≈ −0.05).

For **forward planning**, use the point-in-time figures (Sharpe ~0.76, ann ~+29.5%) as
the realistic baseline, not the survivor figures (~1.2 Sharpe, ~+50%/yr).

Importantly, the RELATIVE arm-vs-baseline comparisons that drive the NO-GO/inconclusive
verdicts are largely robust to this bias: survivorship is common-mode (the same frozen
universe is used for baseline and every arm), so the bias cancels in arm-vs-baseline
deltas. The PBO calculation is similarly unaffected. Only absolute forward-return
projections need adjustment.

---

## No-look-ahead notes

- Scores[t] use price/funding data with index ≤ t exclusively.
- Arm G: score[t,c] = price[t-gap,c] / price[t-lb,c] - 1 — both lags strictly before t.
- Arm K: percentile rank computed per cross-section at t — no future coins visible.
- Arm T: trend[t] = price[t]/price[t-lb]-1 (data ≤ t only); weight[t] earns fwd_ret[t].
- All signals precomputed once on the full panel; CPCV only slices rows (seam-safe).
- Purge = 67 days = max_lookback(60) + max_gap(7) so the furthest Arm G bar pre-dates
  any train window.

All 42 selftest assertions pass (including degenerate: gap=0 ≡ baseline, frac=1/3 ≡
baseline, rank ordering ≡ z-score on normal data, dollar-neutrality for all arms).

---

## Reproduce

```bash
cd /Users/d/prj/funding-rate-arbitrage
# selftest (must pass before trusting results)
.venv/bin/python research/xsmom_signal_improve/selftest.py

# full harness run (~3-5 min)
.venv/bin/python research/xsmom_signal_improve/run_improve.py
```

Output JSON: `research/xsmom_signal_improve/run_improve.json`.
