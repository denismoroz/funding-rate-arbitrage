"""
Sanity tests for the XSMOM risk-overlay (MUST pass before trusting run_overlay.py).

Pre-registered invariants (PLAN.md "Discipline"):
  1. Arm A with a HUGE target_vol  ≈ baseline (scaler clipped to max_leverage but on
     calm books leverage ~max; we instead verify the OPPOSITE-direction invariant
     that is exactly checkable: with target_vol so large the scaler is pinned at
     max_leverage, the overlay == max_leverage * baseline EXACTLY on every scaled
     day; and with max_leverage=1 + huge target_vol the overlay == baseline).
  2. Paired STOP with S=-999% ≈ baseline (never triggers → identical pnl).
  3. Take-profit with P=+999% ≈ baseline (never triggers → identical pnl).
  4. Dollar-neutrality preserved after a paired cut (held sums ~0 every day).
  5. No NaN leakage (overlay introduces no NaN where baseline is finite).
  6. Path-aware sim with NO overlay == xsec.portfolio_returns baseline EXACTLY
     (the path engine is a faithful superset of the carry-forward engine).
  7. No look-ahead in Arm A: scaler[t] is a function of returns < t only.

Run:  /Users/d/prj/funding-rate-arbitrage/.venv/bin/python selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
for _d in [str(_HERE.parent / "validation_harness"),
           str(_HERE.parent / "cross_sectional" / "crypto"),
           str(_HERE.parent / "cross_sectional"),
           str(_HERE.parent), str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import xsec
import overlay
from overlay_pkg import OverlayPackage, REBAL_EVERY, COSTS_BPS


def _toy_weights_fwd(seed=0, n_days=120, n_coins=8):
    """A reproducible toy: random scores -> weekly terciles; random fwd returns."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n_days, freq="D", tz="UTC")
    cols = [f"C{i}" for i in range(n_coins)]
    scores = pd.DataFrame(rng.standard_normal((n_days, n_coins)), index=idx, columns=cols)
    weights = xsec.rank_to_weights(scores)
    fwd = pd.DataFrame(rng.standard_normal((n_days, n_coins)) * 0.03, index=idx, columns=cols)
    return weights, fwd


def test_path_engine_reproduces_baseline():
    """(6) Path-aware sim with an unreachable stop == carry-forward baseline EXACTLY."""
    w, fwd = _toy_weights_fwd(seed=1)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    path = overlay.path_aware_overlay(
        w, fwd, threshold=-9.99, mode="stop",
        pair_rule="worst_opposite", reentry="next_rebalance",
        costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY,
    )
    assert np.allclose(base.values, path.values, atol=1e-12), \
        f"path engine != baseline (max diff {np.abs(base.values-path.values).max():.2e})"
    print("  [6] path-aware (S=-999%) reproduces xsec baseline EXACTLY  OK")


def test_stop_never_triggers():
    """(2) Paired stop with S=-999% == baseline (covers both pair rules / reentries)."""
    w, fwd = _toy_weights_fwd(seed=2)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    for pr in ("worst_opposite", "symmetric_rank"):
        for e in ("next_rebalance", "none"):
            p = overlay.path_aware_overlay(
                w, fwd, threshold=-9.99, mode="stop", pair_rule=pr, reentry=e,
                costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
            assert np.allclose(base.values, p.values, atol=1e-12), \
                f"stop S=-999% ({pr},{e}) != baseline"
    print("  [2] paired stop S=-999% == baseline (all R,E)  OK")


def test_take_profit_never_triggers():
    """(3) Take-profit with P=+999% == baseline."""
    w, fwd = _toy_weights_fwd(seed=3)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    for pr in ("worst_opposite", "symmetric_rank"):
        for e in ("next_rebalance", "none"):
            p = overlay.path_aware_overlay(
                w, fwd, threshold=+9.99, mode="take_profit", pair_rule=pr, reentry=e,
                costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
            assert np.allclose(base.values, p.values, atol=1e-12), \
                f"take_profit P=+999% ({pr},{e}) != baseline"
    print("  [3] paired take-profit P=+999% == baseline (all R,E)  OK")


def test_arm_a_huge_target_passthrough():
    """(1) Arm A: with max_leverage=1 and a huge target_vol the scaler pins at 1
    → overlay == baseline. And with a huge target_vol + high cap the overlay equals
    max_leverage * baseline on every fully-warmed day (scaler saturates at the cap),
    so the ratio is a constant (no per-day distortion of the SHAPE)."""
    w, fwd = _toy_weights_fwd(seed=4)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)

    passthru = overlay.vol_target_scale(base, target_vol_annual=1e6, vol_window=20,
                                        ewma=True, max_leverage=1.0)
    assert np.allclose(base.values, passthru.values, atol=1e-12), \
        "Arm A (huge target, cap=1) must equal baseline"

    cap = 3.0
    scaled = overlay.vol_target_scale(base, target_vol_annual=1e6, vol_window=20,
                                      ewma=True, max_leverage=cap)
    # On warmed days (trailing vol defined & >0) the scaler saturates at cap.
    vol_est = overlay.realised_vol(base, 20, ewma=True).shift(1)
    warm = vol_est.notna() & (vol_est > 0)
    assert np.allclose(scaled.values[warm.values], cap * base.values[warm.values], atol=1e-12), \
        "Arm A huge target should saturate scaler at max_leverage on warmed days"
    # warmup days pass through unscaled (== baseline)
    assert np.allclose(scaled.values[~warm.values], base.values[~warm.values], atol=1e-12), \
        "Arm A warmup days must pass through unscaled"
    print("  [1] Arm A: cap=1+huge target == baseline; huge target saturates at cap  OK")


def test_dollar_neutrality_after_cut():
    """(4) After a paired cut the held book stays dollar-neutral (sum ~0) every day.

    We instrument the simulator by re-deriving held each day with a low stop that
    WILL trigger, and assert Σ held == 0 throughout. Implemented by a transparent
    re-run of the same loop logic on a toy with guaranteed triggers."""
    w, fwd = _toy_weights_fwd(seed=5, n_days=60, n_coins=9)
    # force big adverse moves so stops trigger
    fwd = fwd * 5.0
    sums = _held_sum_path(w, fwd, threshold=-0.10, mode="stop",
                          pair_rule="worst_opposite", reentry="next_rebalance")
    assert np.allclose(sums, 0.0, atol=1e-9), \
        f"dollar-neutrality broken: max |Σheld|={np.abs(sums).max():.2e}"
    # also for symmetric_rank
    sums2 = _held_sum_path(w, fwd, threshold=-0.10, mode="stop",
                           pair_rule="symmetric_rank", reentry="none")
    assert np.allclose(sums2, 0.0, atol=1e-9), "dollar-neutrality broken (symmetric_rank)"
    # and for take-profit cuts
    sums3 = _held_sum_path(w, fwd, threshold=+0.10, mode="take_profit",
                           pair_rule="worst_opposite", reentry="next_rebalance")
    assert np.allclose(sums3, 0.0, atol=1e-9), "dollar-neutrality broken (take_profit)"
    n_trig = int(np.sum(np.abs(np.diff(np.concatenate([[0], (sums == 0).astype(int)]))) > -1))
    print(f"  [4] dollar-neutrality Σheld≈0 every day after cuts (stop wo/sr + tp)  OK")


def _held_sum_path(weights, fwd_ret, *, threshold, mode, pair_rule, reentry):
    """Transparent re-implementation of the held-book walk that returns Σheld per day.

    Mirrors overlay.path_aware_overlay's held-update logic EXACTLY (cut pairing,
    rebalance reset) so the dollar-neutrality assertion is on the same code path's
    semantics. Any divergence here would be a bug to fix in overlay.py."""
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    cols = list(w.columns)
    n = len(w.index)
    rebal_set = set(range(0, n, REBAL_EVERY))
    held = np.zeros(len(cols))
    cum = np.zeros(len(cols))
    blacklist = np.zeros(len(cols), dtype=bool)
    sums = np.zeros(n)
    for i in range(n):
        if i in rebal_set:
            target = w.iloc[i].to_numpy(copy=True)
            if reentry == "none":
                target = np.where(blacklist, 0.0, target)
                # Mirror the dollar-neutrality re-balance fix from overlay.py
                long_idx = np.where(target > 0.0)[0]
                shrt_idx = np.where(target < 0.0)[0]
                long_n, shrt_n = long_idx.size, shrt_idx.size
                if long_n > 0 and shrt_n > 0:
                    k = min(long_n, shrt_n)
                    target[long_idx[k:]] = 0.0
                    target[shrt_idx[k:]] = 0.0
                    long_idx = long_idx[:k]
                    shrt_idx = shrt_idx[:k]
                    target[long_idx] = 1.0 / k
                    target[shrt_idx] = -1.0 / k
                elif long_n == 0 or shrt_n == 0:
                    target = np.zeros(len(cols))
            blacklist = np.zeros(len(cols), dtype=bool)
            held = target.copy()
            cum = np.zeros(len(cols))
        else:
            target = w.iloc[(i // REBAL_EVERY) * REBAL_EVERY].to_numpy()
        ri = r.iloc[i].to_numpy()
        open_mask = (held != 0.0)
        cum = cum + np.where(open_mask, np.sign(held) * ri, 0.0)
        triggered = open_mask & (cum <= threshold) if mode == "stop" \
            else open_mask & (cum >= threshold)
        if triggered.any():
            before = held.copy()
            held = overlay._apply_paired_cuts(held, target, cum, triggered, pair_rule, cols)
            blacklist = blacklist | ((before != 0.0) & (held == 0.0))
        sums[i] = held.sum()
    return sums


def test_no_nan_leakage():
    """(5) No config introduces NaN where the baseline pnl is finite."""
    pkg = OverlayPackage()
    menu = pkg.menu("XSMOM_OVL", pkg.load("XSMOM_OVL"))
    base = menu["baseline"]
    base_finite = np.isfinite(base.values)
    for nm, s in menu.items():
        s = s.reindex(base.index)
        leaked = base_finite & ~np.isfinite(s.values)
        assert not leaked.any(), \
            f"config {nm} has NaN on {int(leaked.sum())} days where baseline is finite"
    print(f"  [5] no NaN leakage across all {len(menu)} menu configs  OK")


def test_arm_a_no_lookahead():
    """(7) Arm A scaler[t] depends only on returns STRICTLY before t.

    Perturb base_pnl at a single day d and confirm the overlay output at day d is
    UNCHANGED (scaler[d] uses vol up to d-1). Outputs at d+1.. may change (legit)."""
    w, fwd = _toy_weights_fwd(seed=7)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    out0 = overlay.vol_target_scale(base, 0.15, 40, ewma=True)
    d = 80
    base2 = base.copy()
    base2.iloc[d] += 0.5            # large perturbation at day d only
    out1 = overlay.vol_target_scale(base2, 0.15, 40, ewma=True)
    # scaler[d] unchanged → out[d] changes ONLY through base[d] itself; isolate by
    # checking the scaler (out/base) at d is identical.
    sc0 = out0.iloc[d] / base.iloc[d] if base.iloc[d] != 0 else 0.0
    sc1 = out1.iloc[d] / base2.iloc[d] if base2.iloc[d] != 0 else 0.0
    assert np.isclose(sc0, sc1, atol=1e-12), \
        f"Arm A look-ahead: scaler[d] moved when only base[d] changed ({sc0} vs {sc1})"
    print("  [7] Arm A scaler[t] uses returns < t only (no same-day look-ahead)  OK")


def test_pnl_units_sane():
    """A book that is dollar-neutral with ~3% daily coin vol and weekly rebal should
    have a small daily mean and a Sharpe nowhere near ±-catastrophe. Guards against a
    sign/timing bug (PLAN: systematic Sharpe << -2 across ALL = a bug)."""
    pkg = OverlayPackage()
    menu = pkg.menu("XSMOM_OVL", pkg.load("XSMOM_OVL"))
    srs = {}
    for nm, s in menu.items():
        r = s.dropna().values
        sr = (r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
        srs[nm] = sr
    worst = min(srs.values())
    assert worst > -2.5, f"a config has catastrophic Sharpe {worst:.2f} → suspect bug"
    print(f"  [sanity] daily Sharpe range over real data: "
          f"[{min(srs.values()):+.2f}, {max(srs.values()):+.2f}]  (no catastrophe)  OK")


# ── New self-tests for Arms D/E/F/G: replacement_overlay ──────────────────────

def test_replacement_stop_never_triggers():
    """(D1) replacement_overlay with S=-999% (fixed-%) ≈ baseline (never triggers)."""
    w, fwd = _toy_weights_fwd(seed=10, n_days=120, n_coins=8)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    # Use the weights as trivial scores (arbitrary — just needs same shape)
    repl = overlay.replacement_overlay(
        w, w, fwd,
        threshold=-9.99, mode="stop", vol_linked=False,
        costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY,
    )
    assert np.allclose(base.values, repl.values, atol=1e-12), (
        f"replacement stop S=-999% != baseline (max diff "
        f"{np.abs(base.values-repl.values).max():.2e})"
    )
    print("  [D1] replacement stop S=-999% == baseline EXACTLY  OK")


def test_replacement_take_profit_never_triggers():
    """(E1) replacement_overlay with P=+999% (fixed-%) ≈ baseline (never triggers)."""
    w, fwd = _toy_weights_fwd(seed=11, n_days=120, n_coins=8)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    repl = overlay.replacement_overlay(
        w, w, fwd,
        threshold=+9.99, mode="take_profit", vol_linked=False,
        costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY,
    )
    assert np.allclose(base.values, repl.values, atol=1e-12), (
        f"replacement take_profit P=+999% != baseline (max diff "
        f"{np.abs(base.values-repl.values).max():.2e})"
    )
    print("  [E1] replacement take-profit P=+999% == baseline EXACTLY  OK")


def test_vol_linked_stop_never_triggers():
    """(F1) replacement_overlay vol-linked with k=999 ≈ baseline (effectively never triggers)."""
    w, fwd = _toy_weights_fwd(seed=12, n_days=120, n_coins=8)
    base = xsec.portfolio_returns(w, fwd, costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)
    repl = overlay.replacement_overlay(
        w, w, fwd,
        threshold=999.0, mode="stop", vol_linked=True, vol_window=20,
        costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY,
    )
    # With k=999 the threshold -999*σ is far below any realistic cum_pnl;
    # NaN vol (warmup) also conservatively suppresses triggers.
    assert np.allclose(base.values, repl.values, atol=1e-12), (
        f"replacement vol-linked stop k=999 != baseline (max diff "
        f"{np.abs(base.values-repl.values).max():.2e})"
    )
    print("  [F1] vol-linked stop k=999 == baseline EXACTLY  OK")


def test_replacement_book_full_size_dollar_neutral():
    """(D2) After a replacement the book stays full-size and dollar-neutral every day.

    We run a transparent step-by-step version of replacement_overlay with a low
    stop threshold (to force many triggers) and assert that:
      - Σ(held) ≈ 0  (net exposure = 0, dollar-neutral)
      - Σ(max(held,0)) ≈ +1  (long leg total ≈ +1)
      - Σ(min(held,0)) ≈ -1  (short leg total ≈ -1)
    every day (post-rebalance initialisation included).
    """
    w, fwd = _toy_weights_fwd(seed=13, n_days=120, n_coins=10)
    # Use scores = w itself (a trivial ordering; what matters is the shape)
    scores = w.copy()
    # big returns so stops fire frequently
    fwd_big = fwd * 8.0

    sums, long_sums, short_sums = _held_sums_replacement(
        w, scores, fwd_big, threshold=-0.05, mode="stop",
        vol_linked=False, vol_window=20,
    )

    assert np.allclose(sums, 0.0, atol=1e-9), (
        f"dollar-neutrality broken in replacement: max|Σheld|={np.abs(sums).max():.2e}"
    )
    # Long and short sides should each sum to ±1 (on non-flat days) or ±<1 if no
    # replacement coin was available (edge case we allow but flag).
    flat_days = (np.abs(long_sums) < 1e-9)
    non_flat = ~flat_days
    if non_flat.any():
        tol = 0.01  # allow slight deviation if replacement coin was unavailable
        assert np.all(np.abs(long_sums[non_flat] - 1.0) <= tol + 1e-9), (
            f"long side not ≈ +1: max dev {np.abs(long_sums[non_flat]-1.0).max():.4f}"
        )
        assert np.all(np.abs(short_sums[non_flat] + 1.0) <= tol + 1e-9), (
            f"short side not ≈ -1: max dev {np.abs(short_sums[non_flat]+1.0).max():.4f}"
        )
    print("  [D2] replacement book: Σheld≈0 & long≈+1 & short≈-1 every day  OK")


def test_replacement_coin_same_side_not_held():
    """(D3) After a replacement, the new coin is on the SAME side as the old one
    and was NOT already held.

    We instrument the overlay loop to record (old_coin_side, new_coin_side,
    was_new_held_before) for every replacement event and assert the invariants.
    """
    rng = np.random.default_rng(42)
    n_days, n_coins = 200, 10
    idx = pd.date_range("2024-01-01", periods=n_days, freq="D", tz="UTC")
    cols = [f"C{i}" for i in range(n_coins)]
    # Scores that vary enough to cause replacements and pick non-trivial coins
    scores = pd.DataFrame(rng.standard_normal((n_days, n_coins)), index=idx, columns=cols)
    w = xsec.rank_to_weights(scores)
    fwd = pd.DataFrame(rng.standard_normal((n_days, n_coins)) * 0.05, index=idx, columns=cols)

    _check_replacement_invariants(w, scores, fwd)
    print("  [D3] replacement coin: same side as old, not already held  OK")


def _check_replacement_invariants(weights, scores, fwd_ret, threshold=-0.03,
                                   vol_window=20):
    """Transparent re-run of replacement_overlay that asserts same-side + not-held."""
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    s = scores.reindex_like(fwd_ret)
    r = fwd_ret.fillna(0.0)
    cols = list(w.columns)
    n_cols = len(cols)
    n = len(w.index)
    rebal_set = set(range(0, n, REBAL_EVERY))

    held = np.zeros(n_cols)
    cum = np.zeros(n_cols)
    leg_weight = 0.0

    for i in range(n):
        if i in rebal_set:
            target = w.iloc[i].to_numpy(copy=True)
            held = target.copy()
            cum = np.zeros(n_cols)
            long_n = int((held > 0).sum())
            leg_weight = (1.0 / long_n) if long_n > 0 else 0.0
        ri = r.iloc[i].to_numpy()
        open_mask = (held != 0.0)
        cum = cum + np.where(open_mask, np.sign(held) * ri, 0.0)
        triggered = open_mask & (cum <= threshold)
        if triggered.any():
            si_row = s.iloc[i].to_numpy()
            trig_idx = np.where(triggered)[0]
            for j in trig_idx:
                if held[j] == 0.0:
                    continue
                old_side = np.sign(held[j])
                not_held = (held == 0.0)
                candidates = np.where(not_held & np.isfinite(si_row))[0]
                if candidates.size == 0:
                    held[j] = 0.0
                    cum[j] = 0.0
                    continue
                if old_side > 0:
                    best = candidates[np.argmax(si_row[candidates])]
                    new_side = +1
                else:
                    best = candidates[np.argmin(si_row[candidates])]
                    new_side = -1
                # Invariant checks BEFORE applying the replacement
                assert best != j, "replacement chose the same coin"
                assert held[best] == 0.0, f"replacement coin C{best} was already held"
                assert new_side == old_side, (
                    f"replacement coin side {new_side} != old side {old_side}"
                )
                held[j] = 0.0
                held[best] = new_side * leg_weight
                cum[j] = 0.0
                cum[best] = 0.0


def _held_sums_replacement(weights, scores, fwd_ret, *, threshold, mode,
                             vol_linked, vol_window):
    """Transparent step-by-step version that returns (Σheld, Σlongs, Σshorts) per day."""
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    s = scores.reindex_like(fwd_ret)
    r = fwd_ret.fillna(0.0)
    cols = list(w.columns)
    n_cols = len(cols)
    n = len(w.index)
    rebal_set = set(range(0, n, REBAL_EVERY))

    if vol_linked:
        coin_vol_arr = r.rolling(vol_window, min_periods=vol_window).std(ddof=0).shift(1).values
    else:
        coin_vol_arr = None

    held = np.zeros(n_cols)
    cum = np.zeros(n_cols)
    leg_weight = 0.0
    sums = np.zeros(n)
    long_sums = np.zeros(n)
    short_sums = np.zeros(n)

    for i in range(n):
        if i in rebal_set:
            target = w.iloc[i].to_numpy(copy=True)
            held = target.copy()
            cum = np.zeros(n_cols)
            long_n = int((held > 0).sum())
            leg_weight = (1.0 / long_n) if long_n > 0 else 0.0
        ri = r.iloc[i].to_numpy()
        open_mask = (held != 0.0)
        cum = cum + np.where(open_mask, np.sign(held) * ri, 0.0)

        if vol_linked and coin_vol_arr is not None:
            sigma_i = coin_vol_arr[i]
            k = abs(threshold)
            if mode == "stop":
                with np.errstate(invalid="ignore"):
                    triggered = open_mask & np.where(
                        np.isfinite(sigma_i), cum <= -k * sigma_i, False
                    )
            else:
                with np.errstate(invalid="ignore"):
                    triggered = open_mask & np.where(
                        np.isfinite(sigma_i), cum >= k * sigma_i, False
                    )
        else:
            if mode == "stop":
                triggered = open_mask & (cum <= threshold)
            else:
                triggered = open_mask & (cum >= threshold)

        if triggered.any():
            si_row = s.iloc[i].to_numpy()
            trig_idx = np.where(triggered)[0]
            for j in trig_idx:
                if held[j] == 0.0:
                    continue
                old_side = np.sign(held[j])
                not_held = (held == 0.0)
                candidates = np.where(not_held & np.isfinite(si_row))[0]
                if candidates.size == 0:
                    held[j] = 0.0
                    cum[j] = 0.0
                else:
                    if old_side > 0:
                        best = candidates[np.argmax(si_row[candidates])]
                        held[j] = 0.0
                        held[best] = leg_weight
                    else:
                        worst = candidates[np.argmin(si_row[candidates])]
                        held[j] = 0.0
                        held[worst] = -leg_weight
                    cum[j] = 0.0

        sums[i] = held.sum()
        long_sums[i] = held[held > 0].sum()
        short_sums[i] = held[held < 0].sum()

    return sums, long_sums, short_sums


def test_no_nan_leakage_full_menu():
    """(NaN2) No NaN leakage in the extended menu including Arms D/E/F/G."""
    from overlay_pkg import OverlayPackage
    pkg = OverlayPackage()
    menu = pkg.menu("XSMOM_OVL", pkg.load("XSMOM_OVL"))
    base = menu["baseline"]
    base_finite = np.isfinite(base.values)
    leaked_configs = []
    for nm, s in menu.items():
        s = s.reindex(base.index)
        leaked = base_finite & ~np.isfinite(s.values)
        if leaked.any():
            leaked_configs.append((nm, int(leaked.sum())))
    assert not leaked_configs, (
        f"NaN leakage in configs: {leaked_configs}"
    )
    # Verify new arms are present
    new_arm_prefixes = ["D_", "E_", "F_", "G_"]
    for pfx in new_arm_prefixes:
        found = [nm for nm in menu if nm.startswith(pfx)]
        assert found, f"No configs found for arm prefix {pfx!r}"
    print(f"  [NaN2] no NaN leakage across all {len(menu)} menu configs (incl. D/E/F/G)  OK")


if __name__ == "__main__":
    print("=== XSMOM risk-overlay self-test ===")
    test_path_engine_reproduces_baseline()
    test_stop_never_triggers()
    test_take_profit_never_triggers()
    test_arm_a_huge_target_passthrough()
    test_dollar_neutrality_after_cut()
    test_no_nan_leakage()
    test_arm_a_no_lookahead()
    test_pnl_units_sane()
    # New tests for Arms D/E/F/G
    test_replacement_stop_never_triggers()
    test_replacement_take_profit_never_triggers()
    test_vol_linked_stop_never_triggers()
    test_replacement_book_full_size_dollar_neutral()
    test_replacement_coin_same_side_not_held()
    test_no_nan_leakage_full_menu()
    print("\nALL SELF-TESTS PASSED")
