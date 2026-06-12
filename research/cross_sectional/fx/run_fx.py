"""
Run the G10 FX cross-sectional long-short book through the validation harness.
Mirrors run_crypto.py: run_harness → print_report → save_json.

Run:
  cd research/cross_sectional/fx
  PYTHONPATH=<research>:<research/validation_harness>:<research/cross_sectional>:<this dir> \
    python run_fx.py

CONFIG / JUDGEMENT CALLS
------------------------
purge = MAX_LOOKBACK_DAYS = 1260  (= the longest menu lookback, value's 5y REER
  change = 5*252 business days).  Seam-safety REQUIRES purge >= max menu lookback
  (days): signals & portfolio pnl are precomputed on the FULL panel; CPCV only
  selects rows, so the bar that fed a test-day value score lies up to 1260 business
  days earlier and must be purged out of train. This is the SAME seam rule crypto
  used (purge=90 for mom90), but in FX the deepest lookback is 14× longer.

THE PURGE TENSION (read before trusting the OOS distribution)
  The panel is ~5240 business days. purge=1260 cuts 1260 bars on EACH side of EACH
  test group — ~24% of the whole series per side. That is FAR heavier than crypto's
  90/1101≈8%. Consequence (measured, printed below):
    - k=2 (test = 2 ADJACENT groups) → the two purge zones meet in the middle and
      eat almost the entire remaining train budget: several splits drop to ~0 train
      days (degenerate). With purge=1260 a k=2 CPCV produces GARBAGE train sets.
    - k=1 (Purged K-Fold, one test group at a time) keeps the train budget healthy:
      min single-split train ≈ 35% of the series, median ≈ 42%. Still a non-trivial
      OOS distribution over 6 splits.
  So we run n_groups=6, k=1 (Purged K-Fold). We PRINT the realized per-split
  train/test sizes so the cost of purge=1260 is explicit and auditable. If any
  split's train collapses below 300 days it is flagged loudly.

  IMPORTANT — what purge actually degrades here: `selected` does NO in-sample
  fitting (blend_fx is a FIXED equal-weight config), so the train budget is NOT
  consumed by a parameter search. The train budget matters for (a) the HONESTY of
  the CPCV structure and (b) nothing in DSR/PBO: BOTH DSR and PBO are computed on
  the FULL-PERIOD menu matrix (period-agnostic, purge-independent). It is only the
  pooled-OOS *distribution* (per test-segment metrics) that suffers from a thin/odd
  train split — and with k=1 it stays valid.

n_groups = 6, k = 1  → C(6,1) = 6 Purged-K-Fold splits.
embargo = 21 days (one rebal cadence ≈ monthly) — buffer after each test group.

costs object: the harness signature needs a `costs` arg, so we reuse the shared
  `from costs import TAKER`. BUT — exactly like crypto — the xsec adapter's
  `simulate` returns PRECOMPUTED pnl, so the harness `costs` arg is NOT what bakes
  FX costs. FX costs are baked by FXXSecPackage.costs_bps (2.0 bps spot-FX spread
  per leg) inside xsec.portfolio_returns when the menu is built. The TAKER passed
  to run_harness only flows to run_cpcv's fit()/simulate(), which ignore it here.

DAILY-vs-HOURLY ANNUALIZATION CAVEAT (read before interpreting OOS numbers):
  engine.compute_metrics annualizes with HOURS_PER_YEAR=8760, i.e. it assumes one
  pnl element == one HOUR. Our book is DAILY. We must NOT edit the harness, so the
  pooled-OOS `annual_pct`/`sharpe`/`calmar` are reported on that hourly scale and
  are INFLATED relative to a daily-annualized read: annual_pct ~ ×(8760/252)≈35,
  sharpe/calmar ~ ×sqrt(8760/252)≈5.9. The HARNESS VERDICT — DSR and PBO — is
  period-agnostic (per-period Sharpe / rank-based) and therefore correct as shown.
  Treat the OOS distribution as SIGN + RELATIVE shape, not literal annual %. (For
  honest daily levels per factor, see fx_pkg.py's self-test via daily_metrics.)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from harness import run_harness, save_json
from report import print_report
from splitter import cpcv
from costs import TAKER

from fx_pkg import FXXSecPackage, MAX_LOOKBACK_DAYS

N_GROUPS = 6
K = 1
PURGE = MAX_LOOKBACK_DAYS   # = 1260 business days
EMBARGO = 21                # business days ≈ one (monthly) rebal cadence
MIN_TRAIN_FLOOR = 300       # below this a split's train is flagged as collapsed


def _print_split_sizes(n: int) -> None:
    """Print realized per-split train/test sizes under purge=PURGE so the cost of
    the seam-safety purge is explicit. Flags any split whose train collapses."""
    splits = cpcv(n, n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    print(f"\n— realized CPCV splits (n={n}, N={N_GROUPS}, k={K}, "
          f"purge={PURGE}, embargo={EMBARGO}) —")
    print(f"  {'split':<7}{'test_groups':<14}{'train':>8}{'train%':>8}"
          f"{'test':>8}{'test%':>8}")
    trains = []
    collapsed = []
    for i, s in enumerate(splits):
        tr, te = len(s.train_idx), len(s.test_idx)
        trains.append(tr)
        flag = "  <-- COLLAPSED" if tr < MIN_TRAIN_FLOOR else ""
        if tr < MIN_TRAIN_FLOOR:
            collapsed.append(i)
        print(f"  {i:<7}{str(s.test_groups):<14}{tr:>8}{tr/n*100:>7.1f}%"
              f"{te:>8}{te/n*100:>7.1f}%{flag}")
    tr_arr = np.array(trains)
    print(f"  train days  min={tr_arr.min()} ({tr_arr.min()/n*100:.1f}%)  "
          f"median={int(np.median(tr_arr))} ({np.median(tr_arr)/n*100:.1f}%)  "
          f"max={tr_arr.max()} ({tr_arr.max()/n*100:.1f}%)")
    if collapsed:
        print(f"  !! WARNING: {len(collapsed)} split(s) have train < "
              f"{MIN_TRAIN_FLOOR} days (collapsed train budget) — OOS distribution "
              f"from these is unreliable. Splits: {collapsed}")
    else:
        print(f"  OK: every split has train >= {MIN_TRAIN_FLOOR} days "
              f"(non-degenerate under purge={PURGE}).")
    # Show what k=2 WOULD do, to make the purge tension concrete (not run).
    if K == 1:
        sp2 = cpcv(n, n_groups=N_GROUPS, k=2, purge=PURGE, embargo=EMBARGO)
        tr2 = np.array([len(s.train_idx) for s in sp2])
        n_bad = int((tr2 < MIN_TRAIN_FLOOR).sum())
        print(f"  (context: at k=2 the train budget collapses — min={tr2.min()}, "
              f"{n_bad}/{len(sp2)} splits < {MIN_TRAIN_FLOOR} days → we use k=1.)")


def main() -> None:
    print("#" * 72)
    print("##### FX cross-sectional long-short через стенд — spot-FX 2.0bps/leg #####")
    print("#" * 72)

    pkg = FXXSecPackage(rebal_every=21, costs=TAKER)
    df = pkg.load("XSEC")
    n = len(df)
    print(f"panel: {n} business days  "
          f"{df.index.min().date()} -> {df.index.max().date()}")
    print(f"rebal_every={pkg.rebal_every}d   costs_bps={pkg.costs_bps:.2f}/leg "
          f"(spot-FX spread, NOT crypto taker)")
    print(f"purge={PURGE}d (= MAX_LOOKBACK_DAYS = value 5y)   n_groups={N_GROUPS}   "
          f"k={K}   embargo={EMBARGO}d")

    _print_split_sizes(n)

    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    print()
    print_report(rep)

    out = Path(__file__).parent / "run_fx.json"
    save_json(rep, out)
    print(f"\nJSON → {out.name}")


if __name__ == "__main__":
    main()
