"""
Run the token-unlock short-book through the validation harness.

Steps:
1. UnlockPackage → run_harness → OOS CPCV + DSR + PBO
2. Event-study bootstrap CI on CAR (size thresholds 0.5%, 1%, 2%)
3. Orthogonality gate: corr to momentum-30d proxy and carry (funding) proxy
4. Save run_unlock.json

ANNUALIZATION CAVEAT (same as run_crypto.py):
engine.compute_metrics assumes 1 element = 1 HOUR. Our PnL is DAILY.
OOS annual_pct / sharpe / calmar are on the hourly scale (~×5.9 for Sharpe).
PRIMARY VERDICT = OOS distribution (frac>0, median shape) + PBO + orthogonality.
DSR is informational (tends to penalize negatively-skewed strategies harshly).

PURGE: max(W) = 14 days. Since panel is daily and harness uses daily rows,
purge=14 means 14 daily rows. This ensures no test-day bar is within the entry
window of a training-adjacent unlock event.

N_GROUPS = 6, K = 2 → C(6,2) = 15 CPCV splits.
Panel ~1102 days. Min train segment ~350 days — adequate.
EMBARGO = 14 days (one full max-window cycle buffer).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_harness_dir = str(_HERE.parent / "validation_harness")
_crypto_dir  = str(_HERE.parent / "cross_sectional" / "crypto")
_research_dir = str(_HERE.parent)
for _d in [_harness_dir, _research_dir, _crypto_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from harness import run_harness, save_json, to_dict
from report import print_report
from costs import TAKER

from unlock_pkg import UnlockPackage, SELECTED, MAX_LOOKBACK_DAYS
from unlock_data import load_events
from unlock_strategy import build_book, event_study_car

N_GROUPS = 6
K = 2
PURGE = MAX_LOOKBACK_DAYS   # = 14 days
EMBARGO = 14                # days


def compute_orthogonality(
    book_pnl: pd.Series,
    events: pd.DataFrame,
) -> dict:
    """Compute correlation of the unlock book to momentum and carry proxies.

    Momentum proxy: 30d cross-sectional momentum return (equal-weight top vs bottom)
    Carry proxy: mean daily funding rate across universe (proxy for funding carry)

    Returns dict with corr_momentum, corr_carry, n_overlap.
    """
    import cryptodata
    from pathlib import Path as P

    data_dir = P(_HERE.parent / "cross_sectional" / "crypto" / "data")
    ev_coins = [c for c in events["coin"].unique()
                if (data_dir / f"{c}_1h.csv").exists()]
    panel = cryptodata.load_panel(coins=ev_coins)
    price   = panel["price"]
    fwd_ret = panel["fwd_ret"]
    funding = panel["funding"]

    # ── Carry proxy: mean daily funding across universe ──────────────────────
    carry_proxy = funding.mean(axis=1)          # daily mean funding rate

    # ── Momentum proxy: 30d cross-sectional long-short ─────────────────────
    # Score: past-30d return. Top third long, bottom third short. EW.
    mom30 = price.pct_change(30)                # 30d rolling return (backward)
    # Dollar-neutral: long top, short bottom tercile → EW weights
    def _xsec_ls_ret(score_row: pd.Series, ret_row: pd.Series) -> float:
        """Long top-third, short bottom-third, EW, market-neutral return."""
        valid = score_row.dropna().index.intersection(ret_row.dropna().index)
        if len(valid) < 4:
            return float("nan")
        sc = score_row[valid]
        r  = ret_row[valid]
        q33, q67 = sc.quantile(0.33), sc.quantile(0.67)
        longs  = r[sc >= q67]
        shorts = r[sc <= q33]
        if longs.empty or shorts.empty:
            return float("nan")
        return float(longs.mean() - shorts.mean())

    mom_pnl = pd.Series(index=fwd_ret.index, dtype=float)
    for t in fwd_ret.index:
        if t in mom30.index and t in fwd_ret.index:
            mom_pnl[t] = _xsec_ls_ret(mom30.loc[t], fwd_ret.loc[t])

    # ── Align all three series ───────────────────────────────────────────────
    shared = book_pnl.index.intersection(carry_proxy.index).intersection(mom_pnl.index)
    book_a    = book_pnl.loc[shared].fillna(0.0)
    carry_a   = carry_proxy.loc[shared].fillna(0.0)
    mom_a     = mom_pnl.loc[shared].fillna(0.0)

    # Only non-zero book days (on-event days) for the correlation
    active = book_a != 0.0
    n_active = int(active.sum())

    corr_carry  = float(book_a[active].corr(carry_a[active])) if n_active > 10 else float("nan")
    corr_mom    = float(book_a[active].corr(mom_a[active]))   if n_active > 10 else float("nan")

    # Also full-sample (including flat days, which dilutes toward 0 — less informative)
    corr_carry_full = float(book_a.corr(carry_a))
    corr_mom_full   = float(book_a.corr(mom_a))

    return {
        "corr_momentum_active": corr_mom,
        "corr_carry_active": corr_carry,
        "corr_momentum_full": corr_mom_full,
        "corr_carry_full": corr_carry_full,
        "n_active_days": n_active,
        "n_total_days": len(shared),
    }


def event_study_table(events: pd.DataFrame) -> dict:
    """Run event-study CAR at multiple size thresholds, return structured results."""
    results = {}
    for thr in [0.005, 0.01, 0.02]:
        study = event_study_car(events, pre=10, post=5, thr=thr, n_bootstrap=2000)
        if study.empty:
            continue
        # Key stats: CAR at day -1 (end of pre-window) and day 0 (unlock day)
        def _get(day: int, col: str) -> float:
            rows = study[study["day"] == day]
            return float(rows[col].values[0]) if len(rows) else float("nan")

        car_pre  = _get(-1, "mean_car")   # CAR cumulated from -10 to -1
        ci_lo    = _get(-1, "ci_lo_95")
        ci_hi    = _get(-1, "ci_hi_95")
        n_events = int(_get(-1, "n_events"))
        car_day0 = _get(0, "mean_car")   # CAR including unlock day

        results[f"thr_{thr:.3f}"] = {
            "n_events": n_events,
            "car_pre": round(car_pre, 5),
            "car_pre_ci_lo_95": round(ci_lo, 5),
            "car_pre_ci_hi_95": round(ci_hi, 5),
            "car_day0": round(car_day0, 5),
            "significant": bool(ci_hi < 0),   # CI excludes 0 on the negative side
        }

    return results


def daily_metrics(s: pd.Series) -> dict:
    """Full-period daily metrics (honest √252 annualization, not √8760)."""
    r = s.dropna().values
    if len(r) < 10:
        return {}
    ann = float(r.mean() * 252)
    vol = float(r.std() * np.sqrt(252))
    sr  = ann / vol if vol > 0 else 0.0
    cum = np.cumprod(1 + r)
    roll_max = np.maximum.accumulate(cum)
    dd = cum / roll_max - 1
    maxdd = float(dd.min())
    calmar = ann / abs(maxdd) if maxdd < 0 else float("inf")
    skew = float(pd.Series(r).skew())
    kurt = float(pd.Series(r).kurt())
    return {
        "ann_return": round(ann, 4),
        "ann_vol": round(vol, 4),
        "sharpe": round(sr, 3),
        "max_drawdown": round(maxdd, 4),
        "calmar": round(calmar, 3),
        "skew": round(skew, 3),
        "kurtosis": round(kurt, 3),
        "n_days": len(r),
    }


def main() -> None:
    print("#" * 72)
    print("##### Token-Unlock Cliff Short Book — Validation Harness #####")
    print("#" * 72)

    # ── Load events & build selected book for orthogonality ───────────────────
    print("\nLoading emission data...")
    events = load_events(verbose=False)
    n_coins = events["coin"].nunique()
    n_events = len(events)
    n_large  = len(events[events["size"] >= 0.01])
    print(f"Events: {n_events} total cliff events across {n_coins} coins")
    print(f"  ≥1% supply: {n_large}   ≥2% supply: {len(events[events['size'] >= 0.02])}")

    print("\nBuilding selected book (W=10, thr=1%, prop)...")
    book_pnl = build_book(events, W=10, thr=0.01, sizing="prop")
    full_metrics = daily_metrics(book_pnl)
    print(f"Full-period metrics (√252 annualization):")
    for k, v in full_metrics.items():
        print(f"  {k}: {v}")

    # ── Event study with bootstrap CI ─────────────────────────────────────────
    print("\n=== Event-Study CAR (market-adjusted, bootstrap CI 95%) ===")
    ev_study = event_study_table(events)
    for thr_key, res in ev_study.items():
        sig = "** SIGNIFICANT **" if res["significant"] else "(not sig at 95%)"
        print(f"  {thr_key}: n={res['n_events']}  "
              f"CAR[-10,-1]={res['car_pre']:.3%}  "
              f"CI=[{res['car_pre_ci_lo_95']:.3%}, {res['car_pre_ci_hi_95']:.3%}]  "
              f"{sig}")

    # ── Harness ───────────────────────────────────────────────────────────────
    print(f"\n=== Harness (CPCV: n_groups={N_GROUPS}, k={K}, "
          f"purge={PURGE}d, embargo={EMBARGO}d) ===")
    pkg = UnlockPackage(costs=TAKER)
    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    print()
    print_report(rep)

    # ── Orthogonality gate ────────────────────────────────────────────────────
    print("\n=== Orthogonality Gate ===")
    orth = compute_orthogonality(book_pnl, events)
    print(f"  Correlation to momentum-30d (active days): "
          f"{orth['corr_momentum_active']:+.3f}")
    print(f"  Correlation to carry/funding  (active days): "
          f"{orth['corr_carry_active']:+.3f}")
    print(f"  Correlation to momentum-30d (all days):     "
          f"{orth['corr_momentum_full']:+.3f}")
    print(f"  Correlation to carry/funding  (all days):   "
          f"{orth['corr_carry_full']:+.3f}")
    print(f"  Active days (book non-zero): {orth['n_active_days']} / "
          f"{orth['n_total_days']} total")

    # ── Sizing comparison ─────────────────────────────────────────────────────
    print("\n=== Sizing comparison (same W=10, thr=1%) ===")
    pnl_eq  = build_book(events, W=10, thr=0.01, sizing="equal")
    m_prop  = daily_metrics(book_pnl)
    m_equal = daily_metrics(pnl_eq)
    print(f"  prop:  Sharpe={m_prop['sharpe']:.2f}  "
          f"Ann={m_prop['ann_return']:.2%}  Calmar={m_prop['calmar']:.2f}")
    print(f"  equal: Sharpe={m_equal['sharpe']:.2f}  "
          f"Ann={m_equal['ann_return']:.2%}  Calmar={m_equal['calmar']:.2f}")
    sharpe_improvement = (m_prop["sharpe"] - m_equal["sharpe"]) / abs(m_equal["sharpe"]) * 100
    print(f"  Prop improvement over equal-weight: {sharpe_improvement:+.1f}% Sharpe")

    # ── Save JSON ─────────────────────────────────────────────────────────────
    out = {
        "strategy": "token_unlock_cliff_short",
        "selected_config": SELECTED,
        "data": {
            "n_coins": n_coins,
            "n_cliff_events_total": n_events,
            "n_cliff_events_ge1pct": n_large,
            "date_range": f"{book_pnl.index.min().date()} → {book_pnl.index.max().date()}",
        },
        "full_period_metrics_selected": full_metrics,
        "event_study_car": ev_study,
        "harness": to_dict(rep),
        "orthogonality": orth,
        "sizing_comparison": {
            "prop": m_prop,
            "equal": m_equal,
            "sharpe_improvement_pct": round(sharpe_improvement, 1),
        },
        "verdict_notes": (
            "Primary verdict: OOS frac>0 + PBO + orthogonality. "
            "DSR informational (negative skew penalizes DSR). "
            "Skew negative (short-squeeze tail risk). "
            "Harness Sharpe/Calmar on hourly scale (×5.9 vs daily); "
            "full_period_metrics are daily-correct."
        ),
    }

    out_path = _HERE / "run_unlock.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON saved → {out_path}")

    # ── Final verdict ─────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("FINAL VERDICT SUMMARY")
    print("=" * 72)
    print(f"Full-period (W=10, thr=1%, prop) Sharpe: {m_prop['sharpe']:.2f}  "
          f"Calmar: {m_prop['calmar']:.2f}  Skew: {m_prop['skew']:.2f}")
    oos_d = rep.pooled_oos.dist
    oos_sr = oos_d.get("sharpe", {}).get("median", float("nan"))
    oos_cal = oos_d.get("calmar", {}).get("median", float("nan"))
    frac_pos = rep.pooled_oos.frac_sharpe_pos
    print(f"OOS (CPCV) median Sharpe: {oos_sr:.2f}  "
          f"median Calmar: {oos_cal:.2f}  frac>0: {frac_pos:.0%}")
    print(f"PBO: {rep.pbo.pbo:.3f}  DSR: {rep.dsr.get('dsr', float('nan')):.3f}")
    print(f"Orthogonality: corr_mom={orth['corr_momentum_active']:+.3f}  "
          f"corr_carry={orth['corr_carry_active']:+.3f}")
    print(f"Event CAR (≥1% supply): "
          f"{ev_study.get('thr_0.010', {}).get('car_pre', float('nan')):.2%} "
          f"(pre-window); significant="
          f"{ev_study.get('thr_0.010', {}).get('significant', False)}")
    print("=" * 72)


if __name__ == "__main__":
    main()
