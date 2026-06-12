"""
Run the crypto cross-sectional long-short book through the validation harness.
Mirrors run_b.py: run_harness → print_report → save_json.

Run:
  PYTHONPATH=<research>:<research/validation_harness>:<research/cross_sectional>:<this dir> \
    python run_crypto.py

CONFIG / JUDGEMENT CALLS
------------------------
purge = MAX_LOOKBACK_DAYS = 90  (= the longest menu lookback, mom90).
  Seam-safety REQUIRES purge >= max menu lookback (days): signals & portfolio pnl
  are precomputed on the FULL panel; CPCV only selects rows, so the bar that fed a
  test-day return lies up to 90 days earlier and must be purged out of train.

n_groups = 6, k = 2  → C(6,2) = 15 CPCV splits.
  Panel is ~1101 daily bars. With purge=90 the train budget is ~44% of the series
  (min single-split train ~360 days ≈ 33%) — non-trivial, so we KEEP mom90 and do
  NOT shrink the max lookback. (At N=4 train would drop to ~24%; N=6 is the sweet
  spot — same default as run_b, enough splits for a stable OOS distribution.)
  NB: `selected` does NO in-sample fitting, so the train budget is not consumed by
  parameter search here; it matters for the honesty of the CPCV structure and for
  PBO/DSR, both of which use the full-period menu matrix.

embargo = 7 days (one rebal cadence) — buffer after each test group.

DAILY-vs-HOURLY ANNUALIZATION CAVEAT (read before interpreting OOS numbers):
  engine.compute_metrics annualizes with HOURS_PER_YEAR=8760, i.e. it assumes one
  pnl element == one HOUR. Our book is DAILY. We must NOT edit the harness, so the
  pooled-OOS `annual_pct`/`sharpe`/`calmar` are reported on that hourly scale and
  are INFLATED relative to a daily-annualized read: annual_pct ~ ×(8760/252)≈35,
  sharpe/calmar ~ ×sqrt(8760/252)≈5.9. The HARNESS VERDICT — DSR and PBO — is
  period-agnostic (per-period Sharpe / rank-based) and therefore correct as shown.
  Treat the OOS distribution as SIGN + RELATIVE shape, not literal annual %.
"""

from __future__ import annotations

from pathlib import Path

from harness import run_harness, save_json
from report import print_report
from costs import TAKER

from crypto_pkg import CryptoXSecPackage, MAX_LOOKBACK_DAYS

N_GROUPS = 6
K = 2
PURGE = MAX_LOOKBACK_DAYS   # = 90 days
EMBARGO = 7                 # days


def main() -> None:
    print("#" * 72)
    print("##### Crypto cross-sectional long-short через стенд — TAKER (8.5bps/leg) #####")
    print("#" * 72)

    pkg = CryptoXSecPackage(rebal_every=7, costs=TAKER)
    print(f"frozen universe: {len(pkg._frozen)} coins   "
          f"rebal_every={pkg.rebal_every}d   costs_bps={pkg.costs_bps:.2f}/leg")
    print(f"purge={PURGE}d (= max menu lookback)   n_groups={N_GROUPS}   k={K}   "
          f"embargo={EMBARGO}d")

    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    print()
    print_report(rep)

    out = Path(__file__).parent / "run_crypto.json"
    save_json(rep, out)
    print(f"\nJSON → {out.name}")


if __name__ == "__main__":
    main()
