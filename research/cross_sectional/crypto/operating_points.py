"""
Operating points for the validated crypto cross-sectional ensemble book.

The user wants two de-levered targets (14% and 25% annualized) from the same
validated ensemble book (momentum ensemble, costs=TAKER 8.5 bps/leg,
rebal_every=7, WITH perp funding drag via -funding.shift(-1) accrual).

Linear leverage scaling of a market-neutral book preserves Sharpe/Calmar but
scales return/vol/maxDD proportionally.  If the full-tilt (k=1) book earns
ann_1 per year, then k*ann_1 = target → k = target / ann_1.

NO LOOK-AHEAD: leverage multipliers are computed from the FULL-PERIOD mean, then
APPLIED to the same series — this is a post-hoc rescaling of a validated
univariate return stream, not a fit on future data.

Gross notional of the scaled book at any point in time is k * Σ|weights|.
For a dollar-neutral tercile book Σlong = Σshort = 1 → Σ|weights| = 2, so
gross_notional = 2k and per-side gross = k.

Run:
  cd research/cross_sectional/crypto
  PYTHONPATH=<repo>/research:<repo>/research/validation_harness:<repo>/research/cross_sectional:<this dir> \
    python operating_points.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
from funding_impact import funding_accrual, FROZEN, FWD, CB, REBAL
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent

# ── Sanity thresholds (per-coin per-side leverage beyond which HL margin gets
#    uncomfortable; not exact caps, just a flag) ──────────────────────────────
# Typical HL max leverage for mid-cap perps: 10-20x; book is 34 coins, equal-
# weight tercile → per-coin allocation = k / (n_tercile) ≈ k/11; flagging
# if per-side gross k > 3 (i.e. 3x the notional budget on each side) as a
# conservative reminder — actual constraints depend on per-coin caps.
_GROSS_FLAG_THRESHOLD = 3.0     # per-side gross leverage flag

# ── TARGET annualized returns ─────────────────────────────────────────────────
TARGET_14 = 0.14
TARGET_25 = 0.25


# ── Rebuild the ensemble NET daily return series (WITH funding drag) ──────────
def build_net_pnl() -> pd.Series:
    """
    Ensemble momentum NET daily return series with HL perp funding accrual.

    Wiring is verbatim from funding_impact.py:
      - frozen 34-coin universe (FROZEN)
      - rebal_every=7 (REBAL)
      - costs_bps=8.5 one-way (CB)
      - accrual = -funding.shift(-1)  (PRIMARY alignment, t→t+1 hold)
    Returns a daily pd.Series of net book returns, same as ens_fund in
    funding_impact.py's main block.
    """
    P = cryptodata.load_panel(coins=FROZEN)
    score = signals.momentum_ensemble(P, lookbacks=(14, 21, 30, 45, 60))
    w = xsec.rank_to_weights(score)
    accr = funding_accrual(-1)   # = -funding.shift(-1), PRIMARY
    pnl = xsec.portfolio_returns(w, FWD, costs_bps=CB, rebal_every=REBAL,
                                 accrual=accr)
    return pnl


def compute_leverage(pnl: pd.Series, target_ann: float) -> float:
    """Return k so that k * mean(pnl) * 365 == target_ann."""
    m = daily_metrics(pnl)
    ann_1 = m["ann"]
    if ann_1 <= 0:
        raise ValueError(f"full-tilt ann={ann_1:.4f} is non-positive; cannot de-lever to {target_ann:.0%}")
    return target_ann / ann_1


def metrics_of_scaled(pnl: pd.Series, k: float) -> dict:
    """daily_metrics on k * pnl (linear scaling)."""
    return daily_metrics(pnl * k)


def by_half_metrics(pnl: pd.Series, k: float) -> tuple[dict, dict]:
    """(1st-half metrics, 2nd-half metrics) for a scaled book."""
    r = (pnl * k).dropna()
    h = len(r) // 2
    m1 = daily_metrics(r.iloc[:h])
    m2 = daily_metrics(r.iloc[h:])
    return m1, m2


def gross_notional(k: float) -> float:
    """Gross notional of the scaled dollar-neutral book (Σ|weights|≈2 → 2k)."""
    return 2.0 * k


def format_row(label: str, k: float, m: dict) -> str:
    return (f"  {label:<22} k={k:5.3f}  ann={100*m['ann']:+7.2f}%  "
            f"vol={100*m['vol_ann']:5.2f}%  maxDD={100*m['maxdd']:5.2f}%  "
            f"Calmar={m['calmar']:+6.2f}  Sharpe={m['sharpe']:+5.2f}")


def feasibility_note(k: float) -> str:
    per_side = k   # Σlong=1, scaled by k → per-side = k
    gross = gross_notional(k)
    flag = " [WARN: per-side gross > threshold]" if per_side > _GROSS_FLAG_THRESHOLD else " [OK]"
    return (f"  gross_notional={gross:.3f}x  per_side={per_side:.3f}x{flag}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("CRYPTO CROSS-SECTIONAL ENSEMBLE — OPERATING POINTS")
    print("full-tilt / 25%-target / 14%-target  (NET of costs + funding drag)")
    print("=" * 80)

    pnl = build_net_pnl()
    r = pnl.dropna()
    print(f"\npanel   : {r.index.min().date()} → {r.index.max().date()}  ({len(r)} days)")
    print(f"config  : ensemble momentum (14,21,30,45,60)  rebal_every={REBAL}  "
          f"costs_bps={CB:.2f}/leg  accrual=-funding.shift(-1)")

    # ── Leverage multipliers ──────────────────────────────────────────────────
    k1  = 1.0
    k25 = compute_leverage(pnl, TARGET_25)
    k14 = compute_leverage(pnl, TARGET_14)

    print(f"\nLeverage multipliers:")
    print(f"  full-tilt  k=1.000")
    print(f"  25%-target k={k25:.4f}")
    print(f"  14%-target k={k14:.4f}")

    # ── Full-period metrics ───────────────────────────────────────────────────
    m1  = metrics_of_scaled(pnl, k1)
    m25 = metrics_of_scaled(pnl, k25)
    m14 = metrics_of_scaled(pnl, k14)

    print("\n" + "─" * 80)
    print("FULL-PERIOD METRICS  (honest daily, PPY=365, maxDD on compounded equity)")
    print("─" * 80)
    for label, k, m in [("full-tilt (k=1)", k1, m1),
                         (f"25%-target (k={k25:.3f})", k25, m25),
                         (f"14%-target (k={k14:.3f})", k14, m14)]:
        print(format_row(label, k, m))

    # ── Gross leverage feasibility ────────────────────────────────────────────
    print("\n" + "─" * 80)
    print("GROSS LEVERAGE FEASIBILITY  (dollar-neutral: Σlong=Σshort=1, gross=2k)")
    print("─" * 80)
    for label, k in [("full-tilt", k1), ("25%-target", k25), ("14%-target", k14)]:
        print(f"  {label:<22}{feasibility_note(k)}")

    # ── By-half split ─────────────────────────────────────────────────────────
    h = len(r) // 2
    mid = r.index[h]
    print("\n" + "─" * 80)
    print(f"BY-HALF SPLIT  (1st: through {r.index[h-1].date()},  "
          f"2nd: from {mid.date()})")
    print("─" * 80)
    print(f"{'':50}{'1st half':>20}{'2nd half':>20}")
    print(f"{'':50}{'ann%  maxDD%':>20}{'ann%  maxDD%':>20}")
    for label, k in [("full-tilt (k=1)", k1),
                     (f"25%-target (k={k25:.3f})", k25),
                     (f"14%-target (k={k14:.3f})", k14)]:
        h1, h2 = by_half_metrics(pnl, k)
        a1 = 100 * h1.get("ann", float("nan"))
        d1 = 100 * h1.get("maxdd", float("nan"))
        a2 = 100 * h2.get("ann", float("nan"))
        d2 = 100 * h2.get("maxdd", float("nan"))
        print(f"  {label:<48}  {a1:+7.2f}%  {d1:5.2f}%    {a2:+7.2f}%  {d2:5.2f}%")

    # ── JSON output ───────────────────────────────────────────────────────────
    h1_1,  h2_1  = by_half_metrics(pnl, k1)
    h1_25, h2_25 = by_half_metrics(pnl, k25)
    h1_14, h2_14 = by_half_metrics(pnl, k14)

    results = {
        "meta": {
            "period_start": str(r.index.min().date()),
            "period_end":   str(r.index.max().date()),
            "n_days": len(r),
            "config": {
                "signal": "momentum_ensemble",
                "lookbacks": [14, 21, 30, 45, 60],
                "rebal_every": REBAL,
                "costs_bps_per_leg": CB,
                "funding_accrual": "-funding.shift(-1) (PRIMARY, t->t+1 hold)",
            },
            "half_split_mid": str(mid.date()),
        },
        "operating_points": {
            "full_tilt": {
                "k": k1,
                "gross_notional": gross_notional(k1),
                "per_side_gross": k1,
                **{f"full_{kk}": vv for kk, vv in m1.items()},
                "half1": {kk: vv for kk, vv in h1_1.items()},
                "half2": {kk: vv for kk, vv in h2_1.items()},
            },
            "target_25pct": {
                "k": k25,
                "gross_notional": gross_notional(k25),
                "per_side_gross": k25,
                **{f"full_{kk}": vv for kk, vv in m25.items()},
                "half1": {kk: vv for kk, vv in h1_25.items()},
                "half2": {kk: vv for kk, vv in h2_25.items()},
            },
            "target_14pct": {
                "k": k14,
                "gross_notional": gross_notional(k14),
                "per_side_gross": k14,
                **{f"full_{kk}": vv for kk, vv in m14.items()},
                "half1": {kk: vv for kk, vv in h1_14.items()},
                "half2": {kk: vv for kk, vv in h2_14.items()},
            },
        },
    }
    out_path = _HERE / "operating_points.json"
    out_path.write_text(json.dumps(results, indent=2, default=float))
    print(f"\nJSON written → {out_path}")
    print("=" * 80)
