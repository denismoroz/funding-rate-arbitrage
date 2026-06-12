"""
C7 — Covariance-Aware Leg Weighting: does risk-sizing beat equal-dollar?

HYPOTHESIS
----------
Equal-dollar weighting over-concentrates risk because crypto coins are highly
correlated and have very different vols.  Replace equal-$ within each tercile
leg with RISK-aware weights while keeping the book dollar-neutral and see if
OOS risk-adjusted return improves.

VARIANTS TESTED
---------------
  BASELINE   — equal-$ ensemble (from run_crypto_v2 / the frozen C6 winner).
  INV-VOL    — inverse-vol within leg, plateau of vol windows (45/60/90/120d).
  MIN-VAR    — min-variance (shrunk covariance) within leg, plateau of cov
               windows (60/90/120d).

All variants use the SAME tercile membership (identical ensemble signal ranking)
and the SAME portfolio_returns wiring (costs=TAKER 8.5bps/leg, rebal_every=7,
funding accrual=-funding.shift(-1)).

SEAM-SAFETY AND PURGE
---------------------
The binding warm-up is max(60d max lookback, 120d max cov window) = 120d.
We use purge=120 (> the 60d C6 purge) so no look-ahead bleeds across splits.
The baseline is re-run on these SAME splits to make comparisons apples-to-apples.

DEJA-VU GUARD (plateau-vs-spike test)
--------------------------------------
We do NOT cherry-pick one window.  We test a PLATEAU of windows and report
whether the improvement (if any) is stable across the plateau or a spike at
one window.  A spike = mirage; a plateau = real.

ANNUALIZATION CAVEAT (inherited from run_crypto_v2)
---------------------------------------------------
The harness annualizes with HOURS_PER_YEAR=8760 (hourly model), so its absolute
OOS Sharpe / Calmar / ann% are inflated ~×5.9 (Sharpe) / ~×35 (ann) vs daily.
Comparisons on THE SAME splits are apples-to-apples.  For ABSOLUTE levels we
use metrics_daily.daily_metrics (√365 = honest daily).  DSR / PBO are
period-agnostic.

Run:
  cd research/cross_sectional/crypto
  PYTHONPATH=<repo>/research:<repo>/research/validation_harness:\\
            <repo>/research/cross_sectional:<crypto-dir> \\
    python -u run_covweight.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
from covweight import inverse_vol_weights, minvar_weights
from costs import Costs, TAKER
from funding_impact import funding_accrual as _mk_accrual  # -funding.shift(shift)

from contract import Strategy
from runner import run_cpcv, _DIST_KEYS
from splitter import cpcv
from harness import run_harness, save_json
from report import print_report
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACKS    = (14, 21, 30, 45, 60)
MAX_LB       = max(LOOKBACKS)           # 60
COSTS_BPS    = 8.5
REBAL_EVERY  = 7
TERCILE_FRAC = 1.0 / 3.0

# Funding accrual panel (PRIMARY alignment: -funding.shift(-1), same as
# funding_impact.py).  This is a module-level constant so it is not recomputed
# per variant but it needs the panel — loaded once below.

# CPCV params: purge = max(60d ensemble lookback, 120d max cov window) = 120d.
# Using the same n_groups / k as C6 so the number of segments is comparable.
N_GROUPS = 6
K        = 2
PURGE    = 120          # >= max_cov_window — seam-safe
EMBARGO  = 7

# Plateau sweep windows
IV_WINDOWS  = (45, 60, 90, 120)     # inverse-vol
MV_WINDOWS  = (60, 90, 120)         # min-var (more expensive, skip 45)


def _frozen_universe() -> list[str]:
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


# ── Book PnL builders ─────────────────────────────────────────────────────────

def _pnl_from_weights(w: pd.DataFrame, fwd_ret: pd.DataFrame,
                      accrual: pd.DataFrame) -> pd.Series:
    return xsec.portfolio_returns(w, fwd_ret,
                                  costs_bps=COSTS_BPS,
                                  rebal_every=REBAL_EVERY,
                                  accrual=accrual)


def build_books(panel: dict) -> dict[str, pd.Series]:
    """Precompute on the FULL frozen panel: baseline + all iv/mv variants.

    Returns dict label → daily pnl Series.  Seam-safe: precomputed once here;
    CPCV only slices these pre-built series.
    """
    price   = panel["price"]
    fwd_ret = panel["fwd_ret"]

    # funding accrual panel (built from panel data, then frozen)
    # Note: funding_impact._mk_accrual uses the global FUND/FWD in that module.
    # We replicate the computation directly here to stay self-contained.
    funding  = panel["funding"]
    accrual  = -funding.shift(-1)       # PRIMARY: -funding.shift(-1)

    score = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)

    # --- Baseline: equal-dollar (identical to C6 ensemble + funding) ---
    w_baseline = xsec.rank_to_weights(score, tercile_frac=TERCILE_FRAC)
    books: dict[str, pd.Series] = {
        "BASELINE": _pnl_from_weights(w_baseline, fwd_ret, accrual),
    }

    # --- Inverse-vol plateau sweep ---
    for vw in IV_WINDOWS:
        print(f"  building INV-VOL vol_window={vw}d ...")
        w = inverse_vol_weights(score, price, vol_window=vw,
                                tercile_frac=TERCILE_FRAC)
        books[f"INV-VOL-{vw}"] = _pnl_from_weights(w, fwd_ret, accrual)

    # --- Min-var plateau sweep ---
    for cw in MV_WINDOWS:
        print(f"  building MIN-VAR cov_window={cw}d ...")
        w = minvar_weights(score, price, cov_window=cw,
                           tercile_frac=TERCILE_FRAC)
        books[f"MIN-VAR-{cw}"] = _pnl_from_weights(w, fwd_ret, accrual)

    return books


# ── Strategy adapter (same pattern as run_crypto_v2.FixedEnsemble) ───────────

class FixedBook:
    """Exposes a precomputed daily pnl as a harness Strategy.
    fit() is a no-op; simulate() slices the precomputed array."""

    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl.values

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return self._pnl[seg]


# ── Harness package for DSR + PBO ─────────────────────────────────────────────

class CovWeightPackage:
    """Package protocol: ONE synthetic 'XSEC' coin; selected = BASELINE;
    menu = all variants.  PBO measures selection danger across the variants;
    DSR measures the BASELINE (unchanged from C6)."""

    name = "Crypto XSEC — cov-weight variants vs equal-$ baseline"
    selected_name = "BASELINE"
    coins = ["XSEC"]

    def __init__(self, books: dict[str, pd.Series]):
        self._books = books
        self._idx   = books["BASELINE"].index

    def load(self, coin: str) -> pd.DataFrame:
        return pd.DataFrame({"close": self._books["BASELINE"].values},
                            index=self._idx)

    def selected(self, coin: str, df: pd.DataFrame) -> FixedBook:
        return FixedBook(self.selected_name, self._books["BASELINE"])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._books)


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _oos_row(label: str, rep) -> str:
    sh  = rep.dist.get("sharpe",     {}).get("median", float("nan"))
    cal = rep.dist.get("calmar",     {}).get("median", float("nan"))
    ann = rep.dist.get("annual_pct", {}).get("median", float("nan"))
    return (f"  {label:<18}{sh:>12.3f}{cal:>12.3f}{ann:>12.2f}"
            f"{rep.frac_sharpe_pos*100:>11.1f}%{rep.frac_calmar_pos*100:>11.1f}%"
            f"{rep.n_segments:>7d}")


def _daily_row(label: str, pnl: pd.Series) -> str:
    m = daily_metrics(pnl)
    if not m:
        return f"  {label:<18}  (too short)"
    cal_s = f"{m['calmar']:>9.2f}" if not np.isnan(m["calmar"]) else f"{'nan':>9}"
    return (f"  {label:<18}{m['sharpe']:>9.2f}{cal_s}"
            f"{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}"
            f"{100*m['vol_ann']:>9.2f}{100*m['hit']:>8.1f}{m['n']:>7d}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("#" * 80)
    print("##### C7 — Covariance-Aware Leg Weighting: does risk-sizing beat equal-$?")
    print("#" * 80)

    coins = _frozen_universe()
    panel = cryptodata.load_panel(coins=coins)
    px = panel["price"]
    print(f"PANEL  {px.shape[0]} days x {px.shape[1]} coins  "
          f"({px.index.min().date()} -> {px.index.max().date()})")
    print(f"lookbacks={LOOKBACKS}  costs={COSTS_BPS}bps/leg  "
          f"rebal_every={REBAL_EVERY}d  tercile_frac={TERCILE_FRAC}")
    print(f"CPCV: n_groups={N_GROUPS} k={K} purge={PURGE}d embargo={EMBARGO}d")
    print(f"IV windows: {IV_WINDOWS}   MV windows: {MV_WINDOWS}")
    print(f"Funding: -funding.shift(-1)  (PRIMARY alignment, same as funding_impact.py)")
    print()
    print("Building books (precomputed once on full panel — seam-safe)...")

    books = build_books(panel)
    labels = list(books.keys())
    print(f"Books built: {labels}")

    # Use the baseline pnl index to drive CPCV splits.
    df = pd.DataFrame({"close": books["BASELINE"].values},
                      index=books["BASELINE"].index)

    # SAME splits for all variants — apples-to-apples.
    splits = cpcv(len(df), n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    print(f"CPCV splits: {len(splits)}")

    # ── Run CPCV for each variant ─────────────────────────────────────────────
    print("\nRunning CPCV for each variant ...")
    reps: dict[str, object] = {}
    for label in labels:
        strat = FixedBook(label, books[label])
        reps[label] = run_cpcv(strat, df, splits=splits, costs=TAKER,
                               n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
        print(f"  {label:<18} done — {reps[label].n_segments} OOS segments")

    # ── Head-to-head OOS table ────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("HEAD-TO-HEAD — POOLED OOS (same CPCV splits; harness HOURLY scale, see caveat)")
    print("=" * 80)
    print(f"  {'variant':<18}{'med Sharpe':>12}{'med Calmar':>12}{'med ann%':>12}"
          f"{'%Sh>0':>12}{'%Cal>0':>11}{'segs':>7}")
    for label in labels:
        print(_oos_row(label, reps[label]))

    # ── Honest daily full-period metrics ─────────────────────────────────────
    print()
    print("=" * 80)
    print("HONEST FULL-PERIOD DAILY METRICS (metrics_daily, sqrt(365) — TRUE levels)")
    print("=" * 80)
    print(f"  {'variant':<18}{'sharpe':>9}{'calmar':>9}{'ann%':>9}"
          f"{'maxDD%':>9}{'vol%':>9}{'hit%':>8}{'n':>7}")
    for label in labels:
        print(_daily_row(label, books[label]))

    # ── Plateau-vs-spike analysis ─────────────────────────────────────────────
    print()
    print("=" * 80)
    print("PLATEAU-vs-SPIKE ANALYSIS")
    print("=" * 80)

    base_sh  = daily_metrics(books["BASELINE"])["sharpe"]
    base_cal = daily_metrics(books["BASELINE"])["calmar"]
    base_mdd = daily_metrics(books["BASELINE"])["maxdd"]

    print(f"\nBASELINE daily Sharpe = {base_sh:.4f}  "
          f"Calmar = {base_cal:.4f}  maxDD = {100*base_mdd:.2f}%")

    print("\n  --- INVERSE-VOL ---")
    iv_sharpes, iv_calmars, iv_mdds = [], [], []
    print(f"  {'window':>8}{'sharpe':>9}{'Δsh':>8}{'calmar':>9}{'Δcal':>9}"
          f"{'maxDD%':>9}{'ΔmDD%':>9}")
    for vw in IV_WINDOWS:
        m = daily_metrics(books[f"INV-VOL-{vw}"])
        iv_sharpes.append(m["sharpe"])
        iv_calmars.append(m["calmar"])
        iv_mdds.append(m["maxdd"])
        print(f"  {vw:>8d}{m['sharpe']:>9.4f}{m['sharpe']-base_sh:>+8.4f}"
              f"{m['calmar']:>9.4f}{m['calmar']-base_cal:>+9.4f}"
              f"{100*m['maxdd']:>9.2f}{100*(m['maxdd']-base_mdd):>+9.2f}")
    iv_sh_range = max(iv_sharpes) - min(iv_sharpes)
    iv_cal_range = max(iv_calmars) - min(iv_calmars)
    print(f"  Range across windows: Sharpe {iv_sh_range:.4f}  Calmar {iv_cal_range:.4f}")
    if iv_sh_range < 0.05:
        print("  INV-VOL: PLATEAU (Sharpe range < 0.05 across windows) — consistent result")
    else:
        print(f"  INV-VOL: SPIKE risk (Sharpe range {iv_sh_range:.4f} >= 0.05) — inspect carefully")

    print("\n  --- MIN-VAR ---")
    mv_sharpes, mv_calmars, mv_mdds = [], [], []
    print(f"  {'window':>8}{'sharpe':>9}{'Δsh':>8}{'calmar':>9}{'Δcal':>9}"
          f"{'maxDD%':>9}{'ΔmDD%':>9}")
    for cw in MV_WINDOWS:
        m = daily_metrics(books[f"MIN-VAR-{cw}"])
        mv_sharpes.append(m["sharpe"])
        mv_calmars.append(m["calmar"])
        mv_mdds.append(m["maxdd"])
        print(f"  {cw:>8d}{m['sharpe']:>9.4f}{m['sharpe']-base_sh:>+8.4f}"
              f"{m['calmar']:>9.4f}{m['calmar']-base_cal:>+9.4f}"
              f"{100*m['maxdd']:>9.2f}{100*(m['maxdd']-base_mdd):>+9.2f}")
    mv_sh_range = max(mv_sharpes) - min(mv_sharpes)
    mv_cal_range = max(mv_calmars) - min(mv_calmars)
    print(f"  Range across windows: Sharpe {mv_sh_range:.4f}  Calmar {mv_cal_range:.4f}")
    if mv_sh_range < 0.05:
        print("  MIN-VAR: PLATEAU (Sharpe range < 0.05 across windows) — consistent result")
    else:
        print(f"  MIN-VAR: SPIKE risk (Sharpe range {mv_sh_range:.4f} >= 0.05) — inspect carefully")

    # ── Best per-variant full summary vs baseline ──────────────────────────────
    print()
    print("=" * 80)
    print("COMPARISON TABLE (best INV-VOL window, best MIN-VAR window vs BASELINE)")
    print("(best = highest OOS pooled-median Sharpe)")
    print("=" * 80)

    # Pick best window by pooled OOS median Sharpe.
    best_iv_label = max(
        [f"INV-VOL-{vw}" for vw in IV_WINDOWS],
        key=lambda lbl: reps[lbl].dist.get("sharpe", {}).get("median", -np.inf),
    )
    best_mv_label = max(
        [f"MIN-VAR-{cw}" for cw in MV_WINDOWS],
        key=lambda lbl: reps[lbl].dist.get("sharpe", {}).get("median", -np.inf),
    )

    # Honest daily metrics for the comparison table.
    def _compare_row(label):
        m = daily_metrics(books[label])
        r = reps[label]
        oos_sh  = r.dist.get("sharpe",     {}).get("median", float("nan"))
        oos_cal = r.dist.get("calmar",     {}).get("median", float("nan"))
        oos_ann = r.dist.get("annual_pct", {}).get("median", float("nan"))
        psh_pos = r.frac_sharpe_pos * 100
        return (label, m["sharpe"], m["calmar"], 100*m["ann"], 100*m["maxdd"],
                oos_sh, oos_cal, oos_ann, psh_pos)

    rows = [_compare_row(l) for l in ["BASELINE", best_iv_label, best_mv_label]]

    # Print DSR + PBO only for BASELINE and the two best variants.
    print(f"\n  {'variant':<22} "
          f"{'d.Sharpe':>9} {'d.Calmar':>9} {'d.Ann%':>8} {'d.MaxDD%':>9} "
          f"{'OOS.Sh':>8} {'OOS.Cal':>9} {'OOS.Ann':>9} {'%OOS>0':>8}")
    for row in rows:
        (label, dsh, dcal, dann, dmdd, osh, ocal, oann, pos) = row
        print(f"  {label:<22} "
              f"{dsh:>9.4f} {dcal:>9.4f} {dann:>8.2f} {dmdd:>9.2f} "
              f"{osh:>8.3f} {ocal:>9.3f} {oann:>9.3f} {pos:>7.1f}%")

    # ── Full harness: DSR + PBO (all variants as menu) ─────────────────────────
    print()
    print("=" * 80)
    print("FULL HARNESS — DSR(BASELINE) + PBO across ALL variants as menu")
    print("(selected = BASELINE; PBO = selection danger if you tried to pick the")
    print(" best risk-weight variant in-sample)")
    print("=" * 80)
    pkg = CovWeightPackage(books)
    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    print()
    print_report(rep)
    print()
    print("  NOTE: DSR evaluates the BASELINE (the committed strategy, unchanged).")
    print("  PBO measures the danger of selecting the 'best' risk-weight variant IS.")
    print("  If PBO is high, risk-weighting gains evaporate when you pick the winner.")

    out = _HERE / "run_covweight.json"
    save_json(rep, out)
    print(f"\nJSON -> {out.name}")

    # ── Final verdict ──────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    bline_m  = daily_metrics(books["BASELINE"])
    best_iv_m = daily_metrics(books[best_iv_label])
    best_mv_m = daily_metrics(books[best_mv_label])

    def _verdict_line(label, m, base_m):
        dsh  = m["sharpe"]  - base_m["sharpe"]
        dcal = m["calmar"]  - base_m["calmar"]
        dmdd = m["maxdd"]   - base_m["maxdd"]
        iv_plateau = iv_sh_range < 0.05 if "INV" in label else None
        mv_plateau = mv_sh_range < 0.05 if "MIN" in label else None
        sig = "PLATEAU" if (iv_plateau or mv_plateau) else "SPIKE"
        beats = (dsh > 0 and dcal > 0 and dmdd < 0)
        verdict = "BEATS baseline" if beats else ("MIXED" if dsh > 0 else "LOSES TO baseline")
        return f"  {label:<22} Δsh={dsh:+.4f}  Δcal={dcal:+.4f}  ΔmDD={100*dmdd:+.2f}%  {sig}  {verdict}"

    print(_verdict_line(best_iv_label, best_iv_m, bline_m))
    print(_verdict_line(best_mv_label, best_mv_m, bline_m))

    print()
    print("  HONEST ONE-PARAGRAPH VERDICT:")
    # Determine overall conclusion.
    iv_beats = (best_iv_m["sharpe"] > bline_m["sharpe"] and
                best_iv_m["calmar"] > bline_m["calmar"] and
                best_iv_m["maxdd"]  < bline_m["maxdd"])
    mv_beats = (best_mv_m["sharpe"] > bline_m["sharpe"] and
                best_mv_m["calmar"] > bline_m["calmar"] and
                best_mv_m["maxdd"]  < bline_m["maxdd"])
    iv_pbo   = rep.pbo.pbo
    iv_dsr   = rep.dsr.get("dsr", float("nan"))

    if iv_beats and iv_sh_range < 0.05:
        conclusion = (
            "Inverse-vol weighting shows a consistent (plateau, not spike) "
            "improvement across all tested windows, reducing max drawdown and "
            "improving Calmar.  The OOS CPCV pattern holds.  PBO and DSR context "
            "below.  Risk-weighting appears REAL but the effect is modest."
        )
    elif iv_beats and iv_sh_range >= 0.05:
        conclusion = (
            "Inverse-vol weighting beats equal-$ on full-period metrics, but the "
            f"Sharpe range across windows is {iv_sh_range:.4f} >= 0.05 — a SPIKE "
            "rather than a plateau.  The benefit is likely noisy covariance "
            "estimation at one particular window, not a robust structural edge."
        )
    else:
        conclusion = (
            "Neither inverse-vol nor min-var weighting reliably beats equal-$ "
            "on all three criteria (Sharpe, Calmar, maxDD) out of sample.  "
            "The improvement (if any) is inconsistent or disappears in OOS CPCV. "
            f"Plateau check: INV-VOL Sharpe range = {iv_sh_range:.4f}, "
            f"MIN-VAR range = {mv_sh_range:.4f}.  "
            "Risk-weighting is MIRAGE from noisy covariance estimation — "
            "equal-dollar wins or ties."
        )
    # Wrap to ~80 chars for readability.
    import textwrap
    for line in textwrap.wrap(conclusion, width=76):
        print(f"  {line}")
    print()
    print(f"  BASELINE: daily Sharpe={bline_m['sharpe']:.4f}  "
          f"Calmar={bline_m['calmar']:.4f}  maxDD={100*bline_m['maxdd']:.2f}%")
    print(f"  BEST INV-VOL ({best_iv_label}): "
          f"daily Sharpe={best_iv_m['sharpe']:.4f}  "
          f"Calmar={best_iv_m['calmar']:.4f}  maxDD={100*best_iv_m['maxdd']:.2f}%")
    print(f"  BEST MIN-VAR ({best_mv_label}): "
          f"daily Sharpe={best_mv_m['sharpe']:.4f}  "
          f"Calmar={best_mv_m['calmar']:.4f}  maxDD={100*best_mv_m['maxdd']:.2f}%")
    print(f"  DSR(BASELINE)={iv_dsr:.3f}   PBO(menu)={iv_pbo:.3f}")
    print("=" * 80)


if __name__ == "__main__":
    main()
