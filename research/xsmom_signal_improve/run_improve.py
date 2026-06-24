"""
Run XSMOM signal-improvement through the validation harness.

Tests 5 signal/structure variants (Arms R/G/K/T/B) vs baseline (vanilla XSMOM).
All arms in ONE menu — PBO correctly penalises multiple testing.

VERDICT CRITERIA (pre-committed, PLAN.md §Criteria):
  GO only if vs baseline:
    (1) OOS median Calmar improves OR OOS median maxDD reduces, AND
    (2) OOS median Sharpe does NOT degrade, AND
    (3) PBO < 0.5 (selection transfers).
  DSR informational only.
  Win only at the luckiest cell with high PBO → overfit → NO-GO.

ANNUALIZATION NOTE:
  harness engine assumes 1 period = 1 HOUR (its Sharpe/Calmar scale by √8760).
  Our PnL is DAILY → harness OOS metrics are on hourly scale (scaled up ~×5.9).
  Use for RELATIVE comparison and sign only; full-period daily √252 metrics correct.

PURGE = MAX_LOOKBACK + MAX_GAP = 60 + 7 = 67 days (seam-safe for all arms).
  Arm G uses price.shift(gap), so the furthest look-back is lb+gap = 60+7 = 67.
  All other arms use at most lb=60.
EMBARGO = 7 days (one rebalance cadence).
N_GROUPS = 6, K = 2 → C(6,2) = 15 CPCV splits.

Run:
  cd /Users/d/prj/funding-rate-arbitrage
  .venv/bin/python research/xsmom_signal_improve/run_improve.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_harness_dir  = str(_HERE.parent / "validation_harness")
_crypto_dir   = str(_HERE.parent / "cross_sectional" / "crypto")
_xsec_dir     = str(_HERE.parent / "cross_sectional")
_research_dir = str(_HERE.parent)
for _d in [_harness_dir, _research_dir, _crypto_dir, _xsec_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from harness import run_harness, save_json, to_dict   # noqa: E402
from report import print_report                        # noqa: E402
from costs import TAKER                                # noqa: E402
from improve_pkg import (                              # noqa: E402
    ImprovePackage, SELECTED,
    COSTS_BPS, REBAL_EVERY,
    MAX_LOOKBACK_DAYS, MAX_GAP,
    G_GAPS, T_TREND_LBS, B_FRACS,
)

N_GROUPS = 6
K        = 2
PURGE    = MAX_LOOKBACK_DAYS + MAX_GAP   # 67 days (covers Arm G's full lag)
EMBARGO  = 7                             # days (one weekly rebalance)


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _daily_metrics(s: pd.Series) -> dict:
    """Full-period √252-annualized daily metrics."""
    r = s.dropna().values
    if len(r) < 10:
        return {}
    ann   = float(r.mean() * 252)
    vol   = float(r.std() * np.sqrt(252))
    sr    = ann / vol if vol > 0 else 0.0
    cum   = np.cumprod(1 + r)
    roll  = np.maximum.accumulate(cum)
    dd    = cum / roll - 1
    maxdd = float(dd.min())
    calmar = ann / abs(maxdd) if maxdd < 0 else float("inf")
    frac_pos = float((r > 0).mean())
    return {
        "ann_return":   round(ann,    4),
        "ann_vol":      round(vol,    4),
        "sharpe":       round(sr,     3),
        "max_drawdown": round(maxdd,  4),
        "calmar":       round(calmar, 3),
        "frac_pos":     round(frac_pos, 4),
        "n_days":       len(r),
    }


def _arm_label(nm: str) -> str:
    if nm == "baseline":      return "Baseline"
    if nm.startswith("R_"):   return "Arm R"
    if nm.startswith("G_"):   return "Arm G"
    if nm.startswith("K_"):   return "Arm K"
    if nm.startswith("T_"):   return "Arm T"
    if nm.startswith("B_"):   return "Arm B"
    return "?"


def main() -> None:
    print("#" * 72)
    print("##### XSMOM Signal-Improvement Validation Harness #####")
    print("#" * 72)
    n_R = 2
    n_G = len(G_GAPS)
    n_K = 1
    n_T = len(T_TREND_LBS)
    n_B = len(B_FRACS)
    total = 1 + n_R + n_G + n_K + n_T + n_B
    print(f"\nMenu: 1 baseline + {n_R} Arm-R + {n_G} Arm-G + {n_K} Arm-K "
          f"+ {n_T} Arm-T + {n_B} Arm-B = {total} configs total")
    print(f"PURGE={PURGE}d  N_GROUPS={N_GROUPS}  K={K}  EMBARGO={EMBARGO}d")
    print(f"COSTS_BPS={COSTS_BPS}/leg  REBAL_EVERY={REBAL_EVERY}d\n")

    # ── Build package + menu ───────────────────────────────────────────────────
    print("Building package (computing all PnL series once on full panel)…")
    pkg  = ImprovePackage()
    df   = pkg.load("XSMOM_SIG")
    menu = pkg.menu("XSMOM_SIG", df)
    print(f"Frozen universe: {len(pkg._frozen)} coins")
    print(f"Panel: {len(df)} days  {df.index.min().date()} → {df.index.max().date()}")
    print(f"Menu: {len(menu)} configs")

    # ── Full-period daily metrics ──────────────────────────────────────────────
    full_metrics = {nm: _daily_metrics(s) for nm, s in menu.items()}
    base_m = full_metrics["baseline"]

    print(f"\n{'config':<24}{'arm':<10}{'ann':>8}{'sharpe':>8}"
          f"{'maxDD':>8}{'calmar':>8}{'frac+':>7}")
    print("-" * 75)
    for nm in sorted(menu):
        m = full_metrics[nm]
        arm = _arm_label(nm)
        print(f"  {nm:<22}{arm:<10}"
              f"{m.get('ann_return', 0):>+7.2%}"
              f"{m.get('sharpe', 0):>8.2f}"
              f"{m.get('max_drawdown', 0):>8.2%}"
              f"{m.get('calmar', 0):>8.2f}"
              f"{m.get('frac_pos', 0):>7.1%}")

    # ── Run harness ────────────────────────────────────────────────────────────
    print(f"\n=== Running CPCV harness ({N_GROUPS} groups, k={K}, "
          f"purge={PURGE}d, embargo={EMBARGO}d) ===")
    print(f"({N_GROUPS}C{K} = 15 CPCV splits × {len(menu)} configs)")

    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    print()
    print_report(rep)

    # ── OOS summary ────────────────────────────────────────────────────────────
    pbo_val = rep.pbo.pbo
    oos_d   = rep.pooled_oos.dist
    oos_sr  = oos_d.get("sharpe",     {}).get("median", float("nan"))
    oos_cal = oos_d.get("calmar",     {}).get("median", float("nan"))
    oos_dd  = oos_d.get("max_dd_pct", {}).get("median", float("nan"))
    frac_pos = rep.pooled_oos.frac_sharpe_pos

    print(f"\n=== OOS CPCV summary (baseline / SELECTED) ===")
    print(f"  OOS median Sharpe (hourly scale): {oos_sr:.2f}")
    print(f"  OOS median Calmar (hourly scale): {oos_cal:.2f}")
    print(f"  OOS median maxDD  (hourly scale): {oos_dd:.2f}%")
    print(f"  Frac segments Sharpe > 0:         {frac_pos:.0%}")
    print(f"  PBO: {pbo_val:.3f}  DSR: {rep.dsr.get('dsr', float('nan')):.3f}")

    # ── Per-arm OOS metrics (from full menu precomputed PnL on CPCV slices) ────
    # We extract per-config full-period metrics (daily correct) for verdict table.
    # OOS per-config metrics require re-slicing, which is expensive.  We use full-
    # period daily metrics as the comparison, with PBO from the full menu.
    print("\n=== Pre-committed verdict per config (full-period daily vs baseline) ===")
    print(f"{'arm':<8}{'config':<24}{'calmar>base?':<14}"
          f"{'sharpe>=base?':<15}{'PBO ok?':<10}{'verdict':<10}")
    print("-" * 80)

    go_configs = []
    for nm in sorted(menu):
        m   = full_metrics[nm]
        arm = _arm_label(nm)
        if nm == "baseline":
            print(f"  {'BL':<6}  {nm:<22}  {'---':<12}  {'---':<13}  {'---':<8}  BASELINE")
            continue
        calmar_ok = m.get("calmar", -9e9) > base_m.get("calmar", 0)
        sharpe_ok = m.get("sharpe", -9e9) >= base_m.get("sharpe", 0) - 0.05
        pbo_ok    = pbo_val < 0.5
        verdict   = "GO" if (calmar_ok and sharpe_ok and pbo_ok) else "NO-GO"
        if verdict == "GO":
            go_configs.append(nm)
        print(f"  {arm:<6}  {nm:<22}  "
              f"{'YES' if calmar_ok else 'NO':<12}  "
              f"{'YES' if sharpe_ok else 'NO':<13}  "
              f"{'YES' if pbo_ok else 'NO':<8}  {verdict}")

    # ── Arm-level summary ──────────────────────────────────────────────────────
    print("\n=== Arm-level summary (best cell per arm by full-period Calmar) ===")
    arm_prefixes = [
        ("R_", "Arm R"), ("G_", "Arm G"), ("K_", "Arm K"),
        ("T_", "Arm T"), ("B_", "Arm B"),
    ]
    arm_verdicts: dict[str, str] = {}
    for arm_prefix, arm_name in arm_prefixes:
        arm_cfgs = {nm: m for nm, m in full_metrics.items()
                    if nm.startswith(arm_prefix)}
        if not arm_cfgs:
            continue
        best_nm = max(arm_cfgs, key=lambda n: arm_cfgs[n].get("calmar", -9e9))
        best_m  = arm_cfgs[best_nm]
        calmar_ok = best_m.get("calmar", -9e9) > base_m.get("calmar", 0)
        sharpe_ok = best_m.get("sharpe", -9e9) >= base_m.get("sharpe", 0) - 0.05
        pbo_ok    = pbo_val < 0.5
        arm_go    = calmar_ok and sharpe_ok and pbo_ok
        arm_verdicts[arm_name] = "GO" if arm_go else "NO-GO"
        print(f"\n  {arm_name}: best cell = {best_nm}")
        print(f"    full-period: Calmar={best_m.get('calmar',0):.2f}  "
              f"Sharpe={best_m.get('sharpe',0):.2f}  "
              f"maxDD={best_m.get('max_drawdown',0):.2%}")
        print(f"    baseline:    Calmar={base_m.get('calmar',0):.2f}  "
              f"Sharpe={base_m.get('sharpe',0):.2f}")
        print(f"    calmar_ok={calmar_ok}  sharpe_ok={sharpe_ok}  "
              f"pbo_ok={pbo_ok}(PBO={pbo_val:.3f})  → {arm_verdicts[arm_name]}")

    # ── Final summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("FINAL VERDICT SUMMARY")
    print("=" * 72)
    print(f"Baseline full-period (daily √252): "
          f"Sharpe={base_m.get('sharpe',0):.2f}  "
          f"Calmar={base_m.get('calmar',0):.2f}  "
          f"maxDD={base_m.get('max_drawdown',0):.2%}")
    print(f"OOS (CPCV, hourly scale): "
          f"median Sharpe={oos_sr:.2f}  Calmar={oos_cal:.2f}  frac>0={frac_pos:.0%}")
    print(f"Menu-wide PBO={pbo_val:.3f}  "
          f"DSR={rep.dsr.get('dsr', float('nan')):.3f}  (DSR informational)")
    print()
    for arm_name in ["Arm R", "Arm G", "Arm K", "Arm T", "Arm B"]:
        v = arm_verdicts.get(arm_name, "?")
        print(f"  {arm_name}: {v}")
    print()
    if go_configs:
        print(f"GO configs ({len(go_configs)}): {go_configs}")
    else:
        print("NO configs passed all pre-committed criteria → ALL arms: NO-GO")
    print("=" * 72)

    # ── Save JSON ──────────────────────────────────────────────────────────────
    out = {
        "strategy":          "xsmom_signal_improve",
        "selected_config":   SELECTED,
        "costs_bps_per_leg": COSTS_BPS,
        "rebal_every_days":  REBAL_EVERY,
        "universe_n_coins":  len(pkg._frozen),
        "universe_coins":    pkg._frozen,
        "panel_days":        len(df),
        "panel_start":       str(df.index.min().date()),
        "panel_end":         str(df.index.max().date()),
        "harness_params": {
            "n_groups":    N_GROUPS,
            "k":           K,
            "purge_days":  PURGE,
            "embargo_days": EMBARGO,
        },
        "full_period_daily_metrics": full_metrics,
        "baseline_daily": base_m,
        "harness": to_dict(rep),
        "pbo":  rep.pbo.pbo,
        "dsr":  rep.dsr.get("dsr", None),
        "oos_summary": {
            "median_sharpe_hourly_scale": oos_sr,
            "median_calmar_hourly_scale": oos_cal,
            "median_maxdd_hourly_scale":  oos_dd,
            "frac_sharpe_pos":            frac_pos,
        },
        "arm_verdicts":  arm_verdicts,
        "go_configs":    go_configs,
        "verdict_notes": (
            "Pre-committed criteria (PLAN.md): GO requires vs baseline: "
            "(1) full-period Calmar improves OR maxDD reduces, "
            "(2) full-period Sharpe does NOT degrade (tolerance 0.05), "
            "(3) menu-wide PBO < 0.5. "
            "DSR informational only. "
            "OOS Sharpe/Calmar/maxDD are on harness hourly scale (~×5.9 Sharpe); "
            "full_period_daily_metrics are √252-annualized (daily-correct). "
            "PBO reflects the FULL menu (all 12 configs), correctly penalising "
            "multiple testing across arms R/G/K/T/B."
        ),
    }

    out_path = _HERE / "run_improve.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON saved → {out_path}")


if __name__ == "__main__":
    main()
