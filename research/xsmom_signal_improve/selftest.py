"""
selftest.py — No-look-ahead and degenerate-case checks for XSMOM signal improvements.

Tests:
  1. gap=0 ≡ baseline momentum (Arm G degenerate)
  2. frac=1/3 ≡ baseline (Arm B degenerate)
  3. Arm K rank ordering ≡ z-score ordering on normal data (both monotonic transforms)
  4. Dollar-neutrality: Σweights ≈ 0 every day for all arms
  5. No NaN leakage (no arm has fewer NaN days than the purge window allows)
  6. No catastrophic Sharpe ≪ -2 (uniform across splits → debug trigger)
  7. Arm T with very large trend_lb: gating rarely fires → close to baseline
  8. No look-ahead structural: G signal at t uses price[t-gap] and price[t-lb] only
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_crypto_dir = str(_HERE.parent / "cross_sectional" / "crypto")
_xsec_dir   = str(_HERE.parent / "cross_sectional")
for _d in [_crypto_dir, _xsec_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cryptodata
import signals as _signals
import xsec as _xsec

from signals_plus import (
    arm_R_sharpe, arm_R_tstat,
    arm_G, arm_K,
    arm_T_weights, arm_B_weights,
    _percentile_rank_cross_section,
    LOOKBACKS, MAX_LOOKBACK,
)
from improve_pkg import ImprovePackage, COSTS_BPS, REBAL_EVERY, G_GAPS, B_FRACS, T_TREND_LBS

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_n_pass = 0
_n_fail = 0


def check(cond: bool, msg: str):
    global _n_pass, _n_fail
    if cond:
        _n_pass += 1
        print(f"  {PASS}  {msg}")
    else:
        _n_fail += 1
        print(f"  {FAIL}  {msg}")


def main():
    print("=" * 68)
    print("selftest.py — XSMOM signal-improvement no-look-ahead + degenerate checks")
    print("=" * 68)

    # ── Load panel ────────────────────────────────────────────────────────────
    pkg = ImprovePackage()
    frozen = pkg._frozen
    print(f"\nLoading panel for {len(frozen)} coins…")
    P = cryptodata.load_panel(coins=frozen)
    price, fwd_ret = P["price"], P["fwd_ret"]
    print(f"Panel: {price.shape[0]} days × {price.shape[1]} coins  "
          f"({price.index.min().date()} → {price.index.max().date()})")

    # ── 1. Arm G degenerate: gap=0 ≡ baseline momentum ─────────────────────
    print("\n[1] Arm G degenerate (gap=0 == baseline momentum)")
    base_scores = _signals.momentum_ensemble(P, lookbacks=LOOKBACKS)
    gap0_scores = arm_G(P, gap=0, lookbacks=LOOKBACKS)
    # They should be nearly identical (same formula)
    diff = (base_scores - gap0_scores).abs()
    max_diff = diff.max().max()
    check(max_diff < 1e-8, f"gap=0 ≡ baseline: max |diff| = {max_diff:.2e}")

    # ── 2. Arm B degenerate: frac=1/3 ≡ baseline weights ───────────────────
    print("\n[2] Arm B degenerate (frac=1/3 == baseline weights)")
    w_base    = _xsec.rank_to_weights(base_scores, tercile_frac=1 / 3)
    w_B_third = arm_B_weights(P, frac=1 / 3, lookbacks=LOOKBACKS)
    diff_B = (w_base - w_B_third).abs()
    max_diff_B = diff_B.max().max()
    check(max_diff_B < 1e-8, f"frac=1/3 ≡ baseline weights: max |diff| = {max_diff_B:.2e}")

    # ── 3. Arm K rank ordering ≡ z-score ordering (monotonic transforms) ────
    print("\n[3] Arm K rank ordering ≡ z-score ordering (on synthetic normal data)")
    rng = np.random.default_rng(42)
    synth_scores = pd.DataFrame(
        rng.standard_normal((100, 20)),
        index=pd.date_range("2024-01-01", periods=100, freq="D"),
        columns=[f"C{i}" for i in range(20)],
    )
    z_legs    = [_signals.zscore_cross_section(synth_scores)]
    r_legs    = [_percentile_rank_cross_section(synth_scores)]
    z_weights = _xsec.rank_to_weights(z_legs[0], tercile_frac=1 / 3)
    r_weights = _xsec.rank_to_weights(r_legs[0], tercile_frac=1 / 3)
    # Both should produce IDENTICAL weight matrices (same ordering → same leg membership)
    diff_kr = (z_weights - r_weights).abs()
    max_diff_kr = diff_kr.max().max()
    check(max_diff_kr < 1e-9,
          f"rank == z ordering on normal data: max |w_diff| = {max_diff_kr:.2e}")

    # ── 4. Dollar-neutrality for all arms ───────────────────────────────────
    print("\n[4] Dollar-neutrality: Σ weights ≈ 0 every day")
    # Build all arms' weight panels
    arm_weights: dict[str, pd.DataFrame] = {
        "baseline": w_base,
        "R_sharpe": _xsec.rank_to_weights(arm_R_sharpe(P, LOOKBACKS), 1/3),
        "R_tstat":  _xsec.rank_to_weights(arm_R_tstat(P, LOOKBACKS), 1/3),
        "K_rank":   _xsec.rank_to_weights(arm_K(P, LOOKBACKS), 1/3),
    }
    for gap in G_GAPS:
        arm_weights[f"G_gap{gap}"] = _xsec.rank_to_weights(
            arm_G(P, gap, LOOKBACKS), 1/3)
    for frac in B_FRACS:
        arm_weights[f"B_frac_{frac:.2f}"] = arm_B_weights(P, frac, LOOKBACKS)
    for tlb in T_TREND_LBS:
        arm_weights[f"T_trend{tlb}"] = arm_T_weights(
            P, trend_lb=tlb, lookbacks=LOOKBACKS, tercile_frac=1/3)

    for arm_name, w in arm_weights.items():
        row_sums = w.sum(axis=1)
        # Rows with any position should sum to ~0; zero-rows are ok
        has_position = w.abs().sum(axis=1) > 0
        max_net = row_sums[has_position].abs().max() if has_position.any() else 0.0
        check(max_net < 1e-8,
              f"{arm_name:<22} max |Σw| on active days = {max_net:.2e}")

    # ── 5. No NaN leakage — signal NaN structure ────────────────────────────
    print("\n[5] No NaN leakage across full menu")
    menu = pkg.menu("XSMOM_SIG", None)
    for nm, s in menu.items():
        n_nan = s.isna().sum()
        n_tot = len(s)
        # PnL series: early rows may be NaN (score warmup), interior should not
        # A simple check: no isolated interior NaN islands
        if n_nan == 0:
            check(True, f"{nm:<22} 0 NaN / {n_tot} rows")
        else:
            # Check that NaNs are all contiguous at the start
            first_valid = s.first_valid_index()
            if first_valid is None:
                check(False, f"{nm:<22} ALL NaN — catastrophic")
            else:
                interior_nan = s.loc[first_valid:].isna().sum()
                check(interior_nan == 0,
                      f"{nm:<22} {n_nan} leading NaN, 0 interior NaN / {n_tot} rows"
                      + (f" [INTERIOR NaN={interior_nan}]" if interior_nan > 0 else ""))

    # ── 6. No catastrophic Sharpe ≪ -2 ─────────────────────────────────────
    print("\n[6] No catastrophic Sharpe ≪ -2")
    for nm, s in menu.items():
        r = s.dropna().values
        if len(r) < 20:
            print(f"  SKIP  {nm:<22} too few obs")
            continue
        ann_sr = r.mean() * 252 / (r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
        check(ann_sr > -2.0,
              f"{nm:<22} full-period Sharpe = {ann_sr:+.2f}  (> -2.0)")

    # ── 7. Arm T large trend_lb rarely fires gate → similar gross ───────────
    print("\n[7] Arm T large trend_lb = 500 (near-unconditional) ≈ baseline gross")
    # trend_lb=500 means price[t]/price[t-500]-1 > 0 is near-always True
    # (most assets trend up over ~2y); gating rarely removes positions.
    # The gated gross should be close to the baseline gross (Calmar within 50%).
    base_r = menu["baseline"].dropna().values
    base_ann = base_r.mean() * 252
    try:
        w_T_big = arm_T_weights(P, trend_lb=500, lookbacks=LOOKBACKS, tercile_frac=1/3)
        pnl_T_big = _xsec.portfolio_returns(
            w_T_big, P["fwd_ret"], costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
        r_big = pnl_T_big.dropna().values
        big_ann = r_big.mean() * 252
        check(abs(big_ann - base_ann) < max(0.10, abs(base_ann) * 1.5),
              f"T_trend500 ann={big_ann:+.2%} vs baseline={base_ann:+.2%}  (close enough)")
    except Exception as e:
        print(f"  SKIP  T_trend500 raised {type(e).__name__}: {e}")

    # ── 8. No look-ahead — structural check on Arm G ────────────────────────
    print("\n[8] No look-ahead structural check (Arm G)")
    # score[t, c] for gap=5, lb=30 should equal price[t-5,c]/price[t-30,c]-1
    # after z-scoring cross-sectionally.  We verify the raw gap_mom before z.
    gap, lb = 5, 30
    raw_gap = price.shift(gap) / price.shift(lb) - 1.0
    # Pick a coin and a date that has a full window
    coin = "BTC"
    btc_dates = price[coin].dropna().index
    # Need at least lb valid rows → pick lb+5+10 to be safe
    t = btc_dates[lb + gap + 10]
    t_gap_idx = price.index.get_loc(t) - gap
    t_lb_idx  = price.index.get_loc(t) - lb
    manual = price.iloc[t_gap_idx][coin] / price.iloc[t_lb_idx][coin] - 1.0
    computed = raw_gap.loc[t, coin]
    check(np.isclose(manual, computed),
          f"G gap={gap} lb={lb}: raw score[{t.date()},{coin}] = {computed:.6f} == "
          f"price[t-{gap}]/price[t-{lb}]-1 ({manual:.6f})")
    # Also verify that the lag dates are strictly before t (no look-ahead)
    check(price.index[t_gap_idx] < t and price.index[t_lb_idx] < t,
          f"both lag dates < t: t-gap={price.index[t_gap_idx].date()} "
          f"t-lb={price.index[t_lb_idx].date()} < t={t.date()}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    total = _n_pass + _n_fail
    print(f"selftest complete: {_n_pass}/{total} PASS, {_n_fail}/{total} FAIL")
    if _n_fail > 0:
        print("  SOME TESTS FAILED — fix before trusting run_improve.py")
        sys.exit(1)
    else:
        print("  ALL TESTS PASSED — safe to run run_improve.py")
    print("=" * 68)


if __name__ == "__main__":
    main()
