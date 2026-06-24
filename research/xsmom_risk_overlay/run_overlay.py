"""
Run the XSMOM risk-overlay through the validation harness.

Evaluates baseline (incumbent) + Arm A (vol-target) + Arm B (paired stop) +
Arm C (paired take-profit) against the pre-registered verdict criteria from PLAN.md.

VERDICT CRITERIA (pre-committed, applied below):
  An overlay is GO only if vs baseline it:
    (1) improves OOS median Calmar / reduces OOS maxDD, AND
    (2) does NOT degrade OOS median Sharpe, AND
    (3) PBO stays low (< 0.5 ideally < 0.2).
  DSR informational only (harness calibration showed DSR wrongly fails profitable
  negative-skew strategies — see validation_harness DSR note in PLAN.md).
  High PBO = "improvement" is regime luck / overfit → NO-GO even if (1)+(2) pass.

ANNUALIZATION CAVEAT (same as run_crypto.py / run_unlock.py):
  engine.compute_metrics assumes 1 element = 1 HOUR.  Our PnL is DAILY.
  OOS annual_pct/sharpe/calmar are on the hourly scale (~×5.9 for Sharpe).
  Treat as SIGN + RELATIVE shape, not literal annual %. PBO and DSR are
  period-agnostic (rank-based / per-period Sharpe) and are CORRECT as shown.

PURGE = MAX_LOOKBACK_DAYS = 60 days (momentum_ensemble max lookback).
  Seam-safety requires purge >= max lookback so the bars that fed a test-day
  return lie outside the train window.

N_GROUPS = 6, K = 2 → C(6,2) = 15 CPCV splits.
EMBARGO = 7 days (one weekly rebalance cycle).

Run:
  cd research/xsmom_risk_overlay
  /path/to/.venv/bin/python run_overlay.py
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
_xsec_dir    = str(_HERE.parent / "cross_sectional")
_research_dir = str(_HERE.parent)
for _d in [_harness_dir, _research_dir, _crypto_dir, _xsec_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from harness import run_harness, save_json, to_dict
from report import print_report
from costs import TAKER

from overlay_pkg import (
    OverlayPackage, SELECTED, MAX_LOOKBACK_DAYS,
    A_TARGET_VOLS, A_VOL_WINDOWS, B_STOPS, C_TAKES, PAIR_RULES, REENTRIES,
    D_STOPS, E_TAKES, FG_KS,
    COSTS_BPS, REBAL_EVERY,
)

N_GROUPS = 6
K        = 2
PURGE    = MAX_LOOKBACK_DAYS   # = 60 days
EMBARGO  = 7                   # days (one rebalance cadence)


def _daily_metrics(s: pd.Series) -> dict:
    """Honest √252 full-period daily metrics (NOT harness hourly scale)."""
    r = s.dropna().values
    if len(r) < 10:
        return {}
    ann  = float(r.mean() * 252)
    vol  = float(r.std() * np.sqrt(252))
    sr   = ann / vol if vol > 0 else 0.0
    cum  = np.cumprod(1 + r)
    roll = np.maximum.accumulate(cum)
    dd   = cum / roll - 1
    maxdd = float(dd.min())
    calmar = ann / abs(maxdd) if maxdd < 0 else float("inf")
    return {
        "ann_return": round(ann, 4),
        "ann_vol": round(vol, 4),
        "sharpe": round(sr, 3),
        "max_drawdown": round(maxdd, 4),
        "calmar": round(calmar, 3),
        "n_days": len(r),
    }


def _arm_label(nm: str) -> str:
    """Map config name to arm label for the verdict table."""
    if nm == "baseline":
        return "Baseline"
    if nm.startswith("A_"):
        return "Arm A"
    if nm.startswith("B_"):
        return "Arm B"
    if nm.startswith("C_"):
        return "Arm C"
    if nm.startswith("D_"):
        return "Arm D"
    if nm.startswith("E_"):
        return "Arm E"
    if nm.startswith("F_"):
        return "Arm F"
    if nm.startswith("G_"):
        return "Arm G"
    return "?"


def _verdict(nm: str, m_cfg: dict, m_base: dict, pbo: float) -> str:
    """Apply pre-committed criteria. Returns GO / NO-GO / BASELINE."""
    if nm == "baseline":
        return "BASELINE"
    calmar_ok  = m_cfg.get("calmar", -9e9) > m_base.get("calmar", 0)
    sharpe_ok  = m_cfg.get("sharpe", -9e9) >= m_base.get("sharpe", 0) - 0.05
    pbo_ok     = pbo < 0.5
    if calmar_ok and sharpe_ok and pbo_ok:
        return "GO"
    return "NO-GO"


def main() -> None:
    print("#" * 72)
    print("##### XSMOM Risk-Overlay Validation Harness #####")
    print("#" * 72)
    print(f"\nPURGE={PURGE}d  N_GROUPS={N_GROUPS}  K={K}  EMBARGO={EMBARGO}d")
    print(f"COSTS_BPS={COSTS_BPS}/leg  REBAL_EVERY={REBAL_EVERY}d")
    n_a = len(A_TARGET_VOLS) * len(A_VOL_WINDOWS)
    n_bc = 2 * len(B_STOPS) * len(PAIR_RULES) * len(REENTRIES)
    n_defg = len(D_STOPS) + len(E_TAKES) + len(FG_KS) + len(FG_KS)
    print(f"Menu: 1 baseline + {n_a} Arm-A + {n_bc} Arm-B/C "
          f"+ {n_defg} Arm-D/E/F/G = {1+n_a+n_bc+n_defg} configs total\n")

    # ── Build package ──────────────────────────────────────────────────────────
    pkg = OverlayPackage()
    print(f"Frozen universe: {len(pkg._frozen)} coins")

    df   = pkg.load("XSMOM_OVL")
    menu = pkg.menu("XSMOM_OVL", df)
    print(f"Panel: {len(df)} days  "
          f"{df.index.min().date()} → {df.index.max().date()}")
    print(f"Menu: {len(menu)} configs\n")

    # ── Full-period daily metrics for each config ──────────────────────────────
    full_metrics = {nm: _daily_metrics(s) for nm, s in menu.items()}
    base_m = full_metrics["baseline"]

    print(f"{'config':<28}{'arm':<10}{'ann':>8}{'sharpe':>8}{'maxDD':>8}{'calmar':>8}")
    print("-" * 72)
    for nm in sorted(menu):
        m = full_metrics[nm]
        arm = _arm_label(nm)
        print(f"  {nm:<26}{arm:<10}{m.get('ann_return',0):>+7.2%}"
              f"{m.get('sharpe',0):>8.2f}{m.get('max_drawdown',0):>8.2%}"
              f"{m.get('calmar',0):>8.2f}")

    # ── Run harness ────────────────────────────────────────────────────────────
    print(f"\n=== Running CPCV harness ({N_GROUPS} groups, k={K}, "
          f"purge={PURGE}d, embargo={EMBARGO}d) ===")
    print("(This may take a few minutes — 31 configs × 15 CPCV splits)")

    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    print()
    print_report(rep)

    # ── OOS distribution per arm ───────────────────────────────────────────────
    # The harness reports the POOLED OOS for the SELECTED config (baseline).
    # For arm-level comparison we pull per-config full-period metrics (daily correct)
    # and compare against the PBO which reflects the WHOLE menu.
    pbo_val = rep.pbo.pbo

    print("\n=== Pre-committed verdict per arm (vs baseline full-period daily) ===")
    print(f"{'arm':<8}{'config':<28}{'calmar>base?':<14}{'sharpe>=base?':<15}"
          f"{'PBO ok?':<10}{'verdict':<10}")
    print("-" * 85)
    for nm in sorted(menu):
        m = full_metrics[nm]
        arm = _arm_label(nm)
        calmar_ok = m.get("calmar", -9e9) > base_m.get("calmar", 0)
        sharpe_ok = m.get("sharpe", -9e9) >= base_m.get("sharpe", 0) - 0.05
        pbo_ok    = pbo_val < 0.5
        if nm == "baseline":
            print(f"  {arm:<6}  {nm:<26}  {'---':<12}  {'---':<13}  "
                  f"{'---':<8}  BASELINE")
        else:
            verdict = "GO" if (calmar_ok and sharpe_ok and pbo_ok) else "NO-GO"
            print(f"  {arm:<6}  {nm:<26}  "
                  f"{'YES' if calmar_ok else 'NO':<12}  "
                  f"{'YES' if sharpe_ok else 'NO':<13}  "
                  f"{'YES' if pbo_ok else 'NO':<8}  {verdict}")

    # ── Arm-level summary ──────────────────────────────────────────────────────
    print("\n=== Arm-level summary (best cell per arm by full-period Calmar) ===")
    arm_prefixes = [
        ("A_", "Arm A"), ("B_", "Arm B"), ("C_", "Arm C"),
        ("D_", "Arm D"), ("E_", "Arm E"), ("F_", "Arm F"), ("G_", "Arm G"),
    ]
    for arm_prefix, arm_name in arm_prefixes:
        arm_configs = {nm: m for nm, m in full_metrics.items()
                       if nm.startswith(arm_prefix)}
        if not arm_configs:
            continue
        best_nm = max(arm_configs, key=lambda n: arm_configs[n].get("calmar", -9e9))
        best_m = arm_configs[best_nm]
        calmar_ok = best_m.get("calmar", -9e9) > base_m.get("calmar", 0)
        sharpe_ok = best_m.get("sharpe", -9e9) >= base_m.get("sharpe", 0) - 0.05
        pbo_ok    = pbo_val < 0.5
        verdict   = "GO" if (calmar_ok and sharpe_ok and pbo_ok) else "NO-GO"
        print(f"\n  {arm_name}: best cell = {best_nm}")
        print(f"    full-period Calmar={best_m.get('calmar',0):.2f}  "
              f"Sharpe={best_m.get('sharpe',0):.2f}  "
              f"maxDD={best_m.get('max_drawdown',0):.2%}")
        print(f"    baseline Calmar={base_m.get('calmar',0):.2f}  "
              f"Sharpe={base_m.get('sharpe',0):.2f}")
        print(f"    calmar_improves={calmar_ok}  sharpe_ok={sharpe_ok}  "
              f"pbo_ok={pbo_ok}(PBO={pbo_val:.3f})  → {verdict}")

    # ── OOS distribution summary for context ──────────────────────────────────
    oos_d = rep.pooled_oos.dist
    oos_sr  = oos_d.get("sharpe", {}).get("median", float("nan"))
    oos_cal = oos_d.get("calmar", {}).get("median", float("nan"))
    oos_dd  = oos_d.get("max_dd_pct", {}).get("median", float("nan"))
    frac_pos = rep.pooled_oos.frac_sharpe_pos

    print(f"\n=== OOS CPCV summary (baseline, SELECTED) ===")
    print(f"  OOS median Sharpe (hourly scale): {oos_sr:.2f}")
    print(f"  OOS median Calmar (hourly scale): {oos_cal:.2f}")
    print(f"  OOS median maxDD (hourly scale):  {oos_dd:.2f}%")
    print(f"  Frac segments Sharpe>0:           {frac_pos:.0%}")
    print(f"  PBO: {pbo_val:.3f}  DSR: {rep.dsr.get('dsr', float('nan')):.3f}")

    # ── Save JSON ──────────────────────────────────────────────────────────────
    out = {
        "strategy": "xsmom_risk_overlay",
        "selected_config": SELECTED,
        "costs_bps_per_leg": COSTS_BPS,
        "rebal_every_days": REBAL_EVERY,
        "universe_n_coins": len(pkg._frozen),
        "universe_coins": pkg._frozen,
        "panel_days": len(df),
        "panel_start": str(df.index.min().date()),
        "panel_end":   str(df.index.max().date()),
        "harness_params": {
            "n_groups": N_GROUPS, "k": K,
            "purge_days": PURGE, "embargo_days": EMBARGO,
        },
        "full_period_daily_metrics": full_metrics,
        "baseline_daily": base_m,
        "harness": to_dict(rep),
        "pbo": rep.pbo.pbo,
        "dsr": rep.dsr.get("dsr", None),
        "oos_summary": {
            "median_sharpe_hourly_scale": oos_sr,
            "median_calmar_hourly_scale": oos_cal,
            "median_maxdd_hourly_scale":  oos_dd,
            "frac_sharpe_pos": frac_pos,
        },
        "verdict_notes": (
            "Pre-committed criteria (PLAN.md): GO requires vs baseline: "
            "(1) Calmar improves OR maxDD reduces, "
            "(2) Sharpe does NOT degrade (tolerance 0.05), "
            "(3) PBO < 0.5. "
            "DSR informational only. "
            "OOS Sharpe/Calmar/maxDD are on harness hourly scale (~×5.9 Sharpe); "
            "full_period_daily_metrics are √252-annualized (daily-correct). "
            "PBO reflects the full menu (baseline + all Arm A/B/C/D/E/F/G cells), "
            "correctly penalising multiple testing across arms."
        ),
    }

    out_path = _HERE / "run_overlay.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON saved → {out_path}")

    # ── Final verdict ──────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("FINAL VERDICT SUMMARY")
    print("=" * 72)
    print(f"Baseline full-period (daily √252): "
          f"Sharpe={base_m.get('sharpe',0):.2f}  "
          f"Calmar={base_m.get('calmar',0):.2f}  "
          f"maxDD={base_m.get('max_drawdown',0):.2%}")
    print(f"OOS (CPCV, hourly scale): median Sharpe={oos_sr:.2f}  "
          f"Calmar={oos_cal:.2f}  frac>0={frac_pos:.0%}")
    print(f"PBO={pbo_val:.3f}  DSR={rep.dsr.get('dsr', float('nan')):.3f}  "
          f"(DSR informational only)")
    print()

    go_configs = [nm for nm in menu if nm != "baseline"
                  and _verdict(nm, full_metrics[nm], base_m, pbo_val) == "GO"]
    if go_configs:
        print(f"GO configs ({len(go_configs)}): {go_configs}")
    else:
        print("NO configs passed all pre-committed criteria → ALL arms: NO-GO")

    for arm_prefix, arm_name in arm_prefixes:
        arm_go = [n for n in go_configs if n.startswith(arm_prefix)]
        arm_all = [n for n in menu if n.startswith(arm_prefix)]
        if not arm_all:
            continue
        if arm_go:
            print(f"  {arm_name}: GO ({len(arm_go)}/{len(arm_all)} cells pass)")
        else:
            print(f"  {arm_name}: NO-GO (0/{len(arm_all)} cells pass)")
    print("=" * 72)


if __name__ == "__main__":
    main()
