# TSMOM Indices + Gold — FINDINGS

Hypothesis: time-series momentum (TSMOM / managed-futures trend) on equity indices
+ gold/silver is a **spot-trend** edge that (a) survives the validation stand and
(b) is **orthogonal to crypto** (carry + momentum). The strategic point: crypto
trend was redundant with XSMOM (+0.40 corr — see `project_trend_following`); the
SAME trend factor on a DIFFERENT asset class should be orthogonal → a risk-parity
diversifier. Validated through `research/validation_harness` (CPCV + DSR + PBO) +
orthogonality gate.

**Verdict: NO-GO** — and for a STRONGER reason than the headline metrics suggest
(read "Why it fails" — the edge is a pre-2012 regime artifact, dead for 15 years).

## Data
Yahoo daily closes (free, no key). 13 assets, panel 1996-06 → 2026-06; effective
~21.6 yr once gold/silver exist (GLD 2004-11, SLV 2006-04; NaN before, like crypto
pre-listing).
- Equity index LEVELS (11): SP500 NASDAQ DOW RUSSELL2K FTSE DAX CAC NIKKEI HANGSENG ASX200 TSX
- Metals ETF (2): GLD (gold), SLV (silver)

**Caveat — price-return:** equity index levels miss ~2%/yr dividends → understate
total return on the net-long equity trend → results are CONSERVATIVE by ~that much.
This is a roughly CONSTANT drag and does NOT explain the regime cliff below (which
is the real trend-following decay). GLD/SLV are clean (ETF spot, no roll, no dividend).

## Strategy
TSMOM (Moskowitz-Ooi-Pedersen): per asset, position = sign(trailing K-mo return) ×
(target_vol 10% / trailing-60d realized vol), gross-normalized to 1.0. NOT
dollar-neutral (directional by trend). Ensemble over K ∈ {3,6,12} mo. Monthly
rebal (21 bd), 2 bps/leg, **accrual = None** (pure spot trend — no carry/swap; that
is the whole thesis: this edge should not depend on broker swap, unlike FX carry).
SELECTED = `tsmom_ens` (FIXED — no in-sample selection, like FX `blend_fx`).
Menu (PBO dimension): tsmom3 / tsmom6 / tsmom12 / tsmom_ens / xs_mom.

## Self-tests (selftest.py) — all pass, EXERCISE the real pnl path
- **Cheat** (feed fwd_ret as signal) → huge + Sharpe ⇒ weight[t] earns fwd_ret[t].
- **No-look-ahead** (shift signal to future) → pnl changes materially ⇒ causal.
- **Deterministic** hand-check of sign + vol-scale on a built panel.
- **Vol-scaling**: high-vol asset gets |w|=0.10 vs low-vol |w|=0.90 (same sign).

## Verdict table

| Metric | Value | Threshold | Gate |
|---|---|---|---|
| DSR | **0.965** | > 0.95 | ✅ *but see decay* |
| PBO | 0.800 | < 0.20 | ❌ |
| honest daily Sharpe (tsmom_ens, full) | **0.49** | — | thin; Calmar 0.18, maxDD 39% |
| corr → XSMOM momentum | **−0.04** | < 0.30 | ✅ |
| corr → FRAB carry | +0.005 | < 0.30 | ✅ |
| corr → BTC buy&hold | +0.06 | < 0.30 | ✅ (recomputed independently) |

OOS Sharpe (harness) is √8760-annualized (hourly assumption) → inflated ~5.9×; use
DSR/sign + the honest daily_metrics column above.

## Why it fails — the regime cliff (the decisive finding)

The DSR 0.965 is a FULL-SAMPLE statistic dominated by a regime that ended ~14 years
ago. Honest daily Sharpe of `tsmom_ens` by period:

| Period | Sharpe | ann |
|---|---|---|
| 2004–2008 | **+1.24** | +10.2% |
| 2008–2012 | +0.44 | +8.4% |
| 2012–2016 | **+0.05** | +0.4% |
| 2016–2020 | **−0.02** | −0.1% |
| 2020–2026 | +0.13 | +1.5% |
| **H1 (96–11)** | **+0.67** | |
| **H2 (11–26)** | **+0.07** | |

This is the well-documented **death of trend-following post-2011** (cf. SG Trend
Index, flat-to-down 2011–2019). The entire apparent edge is front-loaded into
2004–2012; the last **15 years are essentially flat (Sharpe +0.07)**. So the
full-period Sharpe 0.49 / DSR 0.965 are mirages of a dead regime — the forward read
is "no usable edge." (Same shape as the on-chain fee-growth decay, but over a
longer, textbook-known regime.)

## Why PBO fails — NOT a clean twin story
Pairwise PBO of the selected ens vs each menu config: ens-vs-tsmom3 = 0.078 (ens
robust), but **ens-vs-tsmom12 = 0.813** (the ensemble does NOT dominate the 12-mo
lookback) and ens-vs-xs_mom = 0.625. Unlike K_rank in signal_improve (clean twin
with pairwise 0.064), here the lookback choice is genuinely unjustified — the
menu-PBO 0.800 is part twin-inflation, part real "can't pick / nothing dominates."

## The one real positive — orthogonality is genuine
corr to BTC +0.06, XSMOM −0.04, FRAB +0.005 — all far inside the 0.30 gate, and the
BTC corr was recomputed independently (+0.058). So **a new ASSET CLASS does yield
orthogonality to crypto** — exactly as a new DATA TYPE did for on-chain fees. This
validates the meta-thesis (orthogonal axes come from new asset classes / data types,
not new signals on the same inputs). BUT a stale signal on an orthogonal axis is
still stale: you cannot risk-parity-blend a sleeve with ~zero forward edge.

## Decision
**Do not build live.** The orthogonality is real and strategically confirming, but
the trend signal itself is a pre-2012 artifact that has been flat for 15 years (the
classic managed-futures decay), and the lookback choice is unjustified (PBO). This
is the SECOND new-asset-class trend test: crypto trend = redundant (+0.40 XSMOM);
indices/gold trend = orthogonal but DEAD. Trend-as-an-axis keeps failing — here by
regime decay, not redundancy.

Revisit ONLY if: (a) trend-following revives in live (the 2022 CTA resurgence did
NOT show here at sleeve level → +0.13), or (b) a DIFFERENT signal on this orthogonal
universe (e.g. cross-asset value/carry on bonds+commodities, or a faster
breakout/vol-target variant) is tested — the universe + harness wiring is reusable.

## Reproduce
```bash
cd research/indices_gold
PYTHONPATH="../validation_harness:../cross_sectional:../cross_sectional/crypto:..:." python selftest.py
PYTHONPATH="../validation_harness:../cross_sectional:../cross_sectional/crypto:..:." python run_ig.py
```
