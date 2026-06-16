"""
trend_validation.py — Rigorous validation of the directional TREND-FOLLOWING menu
(TSMOM per-lookback + TSMOM-ENSEMBLE + Donchian breakout) on the survivorship-debiased
PT panel, through the shared validation harness (CPCV + DSR + PBO). Task C of
research/trend_following/PLAN.md.

THE QUESTION
------------
Is there a REAL standalone edge in time-series momentum / Donchian trend-following on
the HL crypto universe, once you account for multiple testing (a menu of lookbacks /
channels), out-of-sample robustness, and overfitting? The committed book is the
equal-weight TSMOM ENSEMBLE over lookbacks (30,60,90,120) — FIXED BEFORE looking at OOS
(per PLAN) so the lookback is not cherry-picked.

METHODOLOGY
-----------
Mirrors event_driven_validation.py (the cross-sec template) EXACTLY in:
  - PT panel build (survivorship-debiased, SAME universe → apples-to-apples with XSMOM),
  - CPCV parameters (n_groups=6, k=2, purge=max-lookback=120, embargo=7),
  - DSR (N=menu_size deflation + N=1 per book),
  - PBO across the full menu,
  - metrics_daily.daily_metrics for honest IS metrics (sqrt(365)),
  - JSON output shape.

The ONLY structural divergence from the template: the template's two mandatory asserts
reproduce the cross-sec scheduled R=7 book == survivorship.run_book. Those are book-
specific to XSEC and DO NOT apply to trend. We replace them with a TREND-appropriate
sanity assert (see CORRECTNESS GUARD below).

CORRECTNESS GUARD (mandatory assert)
------------------------------------
The committed TSMOM_ENS pnl we feed the harness MUST be EXACTLY (max abs diff < 1e-9)
the same series characterize.py (Task B) builds for TSMOM_ENS. We rebuild it here with
the IDENTICAL constants (VOL_TARGET=0.02, LEVERAGE_CAP=3.0, COSTS_BPS=survivorship.COSTS_BPS,
vol=realized_vol(price,30), accrual=-funding.shift(-1), lookbacks (30,60,90,120)) and
re-run characterize.build_book to compare. This proves the harness validates the SAME book
B characterized — i.e. we are deflating the committed candidate, not a different series.

ANNUALIZATION CAVEAT (stated prominently in output AND json)
------------------------------------------------------------
The harness internally annualizes with HOURS_PER_YEAR=8760 (it models an HOURLY book).
Our pnl is DAILY. So any pooled-OOS annual_pct / Sharpe printed BY THE HARNESS would be
INFLATED (~×35 on annual, ~×5.9 on Sharpe). We therefore compute every honest ABSOLUTE
level via metrics_daily (sqrt(365)) only — exactly as the template does. DSR and PBO are
ratio/ranking based (period-agnostic) and ARE valid as-is. Every harness-annualized
number is marked INFLATED with its metrics_daily honest counterpart given.

Run:
  cd /Users/d/prj/funding-rate-arbitrage && \\
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/trend_following \\
  .venv/bin/python research/trend_following/trend_validation.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── crypto-local data + honest metrics ─────────────────────────────────────────
import survivorship
from metrics_daily import daily_metrics

# ── trend engine (Task A, frozen) ──────────────────────────────────────────────
from trend import (
    tsmom_signal,
    tsmom_ensemble,
    donchian_signal,
    realized_vol,
    portfolio_returns_directional,
)

# ── Task B characterization (for the provenance sanity assert) ─────────────────
import characterize

# ── validation harness ─────────────────────────────────────────────────────────
from metrics import dsr_from_returns, moments
from pbo import pbo

_HERE = Path(__file__).parent

# ══════════════════════════════════════════════════════════════════════════════
# FIXED DESIGN CONSTANTS — IDENTICAL to characterize.py (Task B) so the books match
# ══════════════════════════════════════════════════════════════════════════════

COSTS_BPS    = survivorship.COSTS_BPS  # 8.5 bps/leg — imported, not hardcoded.
VOL_TARGET   = 0.02                    # 2%/day per-asset vol target (== Task B).
LEVERAGE_CAP = 3.0                     # gross Σ|held| cap (== Task B).
VOL_WINDOW   = 30                      # causal realized-vol window (== Task B).

TSMOM_LOOKBACKS   = (30, 60, 90, 120)  # menu lookbacks (longer than cross-sec).
DONCHIAN_CHANNELS = (20, 55, 100)      # Turtle 20/55 + slower 100.

# Menu of candidate books (labels), and the COMMITTED pick (fixed BEFORE OOS).
MENU = (
    [f"TSMOM_L{L}" for L in TSMOM_LOOKBACKS]
    + ["TSMOM_ENS"]
    + [f"DONCH_N{N}" for N in DONCHIAN_CHANNELS]
)
COMMITTED = "TSMOM_ENS"  # committed per PLAN: the ensemble avoids lookback cherry-pick.

# CPCV parameters (same as the template; purge = MAX lookback in the menu = 120).
N_GROUPS     = 6
K_CPCV       = 2
PURGE_DAYS   = 120   # = max(TSMOM lookback 120, Donchian channel 100) → seam-safe.
EMBARGO_DAYS = 7
PBO_S        = 16


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Build the PT panel (survivorship-debiased) — identical to template
# ══════════════════════════════════════════════════════════════════════════════

def _build_pt_panel() -> dict:
    surv_json = json.loads(
        (_HERE.parent / "cross_sectional" / "crypto" / "survivorship.json").read_text()
    )
    all_coins = sorted(
        set(surv_json["frozen_survivor_coins"])
        | set(surv_json["extra_dead_coins_included"])
    )
    print(f"PT universe: {len(all_coins)} coins "
          f"({len(surv_json['frozen_survivor_coins'])} survivors + "
          f"{len(surv_json['extra_dead_coins_included'])} dead/delisted)")
    return survivorship.build_pt_panel(all_coins)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Build all books (one daily pnl series each), identical economics to B
# ══════════════════════════════════════════════════════════════════════════════

def _build_book_pnl(label: str, positions: pd.DataFrame, panel: dict,
                    vol: pd.DataFrame, accrual: pd.DataFrame) -> pd.Series:
    """Net daily pnl series for ONE directional book — IDENTICAL call to Task B."""
    return portfolio_returns_directional(
        positions, panel["fwd_ret"], costs_bps=COSTS_BPS, accrual=accrual,
        vol=vol, vol_target=VOL_TARGET, leverage_cap=LEVERAGE_CAP,
    )


def _build_all_books(panel: dict, vol: pd.DataFrame, accrual: pd.DataFrame) -> dict:
    """Build every menu book → dict label -> pd.Series pnl. Same constants as B."""
    books: dict[str, pd.Series] = {}
    for L in TSMOM_LOOKBACKS:
        sig = tsmom_signal(panel, lookback=L, vol_window=VOL_WINDOW)
        books[f"TSMOM_L{L}"] = _build_book_pnl(f"TSMOM_L{L}", sig, panel, vol, accrual)
    ens_sig = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW)
    books["TSMOM_ENS"] = _build_book_pnl("TSMOM_ENS", ens_sig, panel, vol, accrual)
    for N in DONCHIAN_CHANNELS:
        sig = donchian_signal(panel, channel=N)
        books[f"DONCH_N{N}"] = _build_book_pnl(f"DONCH_N{N}", sig, panel, vol, accrual)
    return books


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS GUARD — committed pnl == characterize.py's TSMOM_ENS (trend-appropriate)
# ══════════════════════════════════════════════════════════════════════════════

def _assert_committed_equals_characterize(committed_pnl: pd.Series, panel: dict,
                                          vol: pd.DataFrame, accrual: pd.DataFrame) -> float:
    """Assert the committed TSMOM_ENS pnl we feed the harness is EXACTLY the series
    Task B (characterize.py) builds for TSMOM_ENS, to <1e-9 max abs diff.

    Replaces the template's two XSEC-specific reproduction asserts (scheduled R=7 ==
    xsec.portfolio_returns and R=7 == survivorship.run_book), which do not apply to the
    directional trend book. We rebuild the ENS book through characterize.build_book
    (which uses the SAME trend.py engine + constants) and compare its pnl to ours.
    """
    ens_sig = tsmom_ensemble(panel, lookbacks=characterize.TSMOM_LOOKBACKS,
                             vol_window=characterize.VOL_WINDOW)
    # characterize.build_book uses characterize's module-level VOL_TARGET / LEVERAGE_CAP /
    # COSTS_BPS — which we assert equal ours below, so this is the same book B produced.
    b = characterize.build_book("TSMOM_ENS", ens_sig, panel, vol, accrual)
    diff = float((committed_pnl - b["pnl"]).abs().max())
    print(f"    ASSERT: committed TSMOM_ENS vs characterize.build_book => max diff = {diff:.2e}")
    assert characterize.VOL_TARGET == VOL_TARGET, "VOL_TARGET diverged from Task B"
    assert characterize.LEVERAGE_CAP == LEVERAGE_CAP, "LEVERAGE_CAP diverged from Task B"
    assert characterize.COSTS_BPS == COSTS_BPS, "COSTS_BPS diverged from Task B"
    assert characterize.VOL_WINDOW == VOL_WINDOW, "VOL_WINDOW diverged from Task B"
    assert tuple(characterize.TSMOM_LOOKBACKS) == tuple(TSMOM_LOOKBACKS), \
        "TSMOM_LOOKBACKS diverged from Task B"
    assert diff < 1e-9, (
        f"ASSERT FAILED: committed pnl != characterize.py TSMOM_ENS (diff={diff:.2e}); "
        "the harness would be validating a DIFFERENT book than Task B characterized. "
        "Check the constants are identical to characterize.py."
    )
    print("    ASSERT PASSED: harness validates the SAME committed book B characterized.")
    return diff


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — CPCV OOS distribution (copied from template, purge=120)
# ══════════════════════════════════════════════════════════════════════════════

def _cpcv_oos_dist(pnl_vals: np.ndarray, n: int) -> dict:
    from splitter import cpcv

    splits = cpcv(n, n_groups=N_GROUPS, k=K_CPCV,
                  purge=PURGE_DAYS, embargo=EMBARGO_DAYS)

    oos_sharpes, oos_anns, oos_maxdds = [], [], []
    for sp in splits:
        test_sorted = np.sort(sp.test_idx)
        breaks = np.where(np.diff(test_sorted) > 1)[0] + 1
        segs = np.split(test_sorted, breaks)
        for seg_idx in segs:
            seg_pnl = pnl_vals[seg_idx]
            seg_pnl = seg_pnl[np.isfinite(seg_pnl)]
            if len(seg_pnl) < 10:
                continue
            m = daily_metrics(pd.Series(seg_pnl))
            if not m:
                continue
            oos_sharpes.append(m["sharpe"])
            oos_anns.append(m["ann"] * 100)
            oos_maxdds.append(m["maxdd"] * 100)

    oos_sharpes = np.array(oos_sharpes)
    oos_anns    = np.array(oos_anns)
    oos_maxdds  = np.array(oos_maxdds)

    def _dist(arr: np.ndarray) -> dict:
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return {}
        return {
            "median": float(np.median(arr)),
            "iqr_lo": float(np.percentile(arr, 25)),
            "iqr_hi": float(np.percentile(arr, 75)),
            "mean":   float(np.mean(arr)),
        }

    return {
        "n_segments": len(oos_sharpes),
        "sharpe":     _dist(oos_sharpes),
        "ann_pct":    _dist(oos_anns),
        "maxdd_pct":  _dist(oos_maxdds),
        "frac_sharpe_pos": float((oos_sharpes > 0).mean()) if oos_sharpes.size else float("nan"),
        "all_oos_sharpes": oos_sharpes.tolist(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — DSR helpers (copied from template)
# ══════════════════════════════════════════════════════════════════════════════

def _dsr_with_menu(pnl: pd.Series, trial_sharpes: np.ndarray) -> dict:
    r = pnl.dropna().values
    return dsr_from_returns(r, trial_sharpes)


def _dsr_n1(pnl: pd.Series) -> dict:
    r = pnl.dropna().values
    m = moments(r)
    return dsr_from_returns(r, np.array([m.sr]))


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — PBO across the full menu (adapted from template)
# ══════════════════════════════════════════════════════════════════════════════

def _pbo_across_menu(books_aligned: dict[str, np.ndarray]) -> dict:
    """CSCV PBO across the full trend menu (8 books). IS-best selection vs OOS rank."""
    labels = MENU
    R = np.column_stack([books_aligned[lbl] for lbl in labels])
    T, N = R.shape

    S = min(PBO_S, (T // 20) * 2)
    S = max(4, S - (S % 2))
    if S >= T:
        S = 4

    res = pbo(R, S=S, names=labels)
    return {
        "pbo": res.pbo,
        "n_splits": res.n_splits,
        "n_configs": res.n_configs,
        "S": res.S,
        "median_oos_rank": res.median_oos_rank,
        "is_best_counts": res.is_best_counts,
        "note": (f"N={N} configs → PBO={res.pbo:.3f}: fraction of splits where "
                 "IS-best config ranks below median OOS."),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TURNOVER — annual turnover on the SCALED held book (reuse characterize replica)
# ══════════════════════════════════════════════════════════════════════════════

def _annual_turnover(label: str, positions: pd.DataFrame, panel: dict,
                     vol: pd.DataFrame, common_idx: pd.Index) -> dict:
    """Annual turnover on the actual SCALED held book over the common window.

    Reuses characterize.scale_positions (the provably-correct replica of the engine's
    internal vol-target+cap) and characterize.turnover_and_gross — same accounting as
    Task B so turnover/yr cross-checks 1:1 with characterize.json.
    """
    held = characterize.scale_positions(
        positions, panel["fwd_ret"], vol, VOL_TARGET, LEVERAGE_CAP
    ).loc[common_idx]
    n_years = len(common_idx) / characterize.PPY
    tg = characterize.turnover_and_gross(held, n_years)
    return tg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 92)
    print("TREND-FOLLOWING VALIDATION — TSMOM/Donchian menu through CPCV+DSR+PBO")
    print(f"Menu: {MENU}   committed = {COMMITTED} (fixed BEFORE OOS)")
    print("=" * 92)

    # ── Build PT panel ────────────────────────────────────────────────────────
    print("\n[1] Building survivorship-debiased PT panel (identical to XSEC/Task B)...")
    panel = _build_pt_panel()
    px    = panel["price"]
    n_days = len(px)
    date_min = px.index.min().date()
    date_max = px.index.max().date()
    n_coins  = len(panel["coins"])
    print(f"    Panel: {date_min} → {date_max}  ({n_days} days, {n_coins} coins)")

    # Shared inputs — IDENTICAL to characterize.py.
    vol = realized_vol(px, vol_window=VOL_WINDOW)
    accrual = -panel["funding"].shift(-1)

    print(f"\n    Constants (IDENTICAL to Task B): VOL_TARGET={VOL_TARGET}  "
          f"LEVERAGE_CAP={LEVERAGE_CAP}  COSTS_BPS={COSTS_BPS}  VOL_WINDOW={VOL_WINDOW}")
    print(f"    CPCV: n_groups={N_GROUPS} k={K_CPCV} purge={PURGE_DAYS} "
          f"embargo={EMBARGO_DAYS}  (purge = max lookback in menu)")

    # ── Build all books ───────────────────────────────────────────────────────
    print("\n[2] Building all menu books (one daily pnl series each)...")
    books = _build_all_books(panel, vol, accrual)
    for lbl in MENU:
        print(f"    {lbl}: shape={books[lbl].shape}  nan_count={books[lbl].isna().sum()}")

    # ── Correctness guard (trend-appropriate provenance assert) ───────────────
    print("\n[3] Correctness guard (committed pnl == characterize.py TSMOM_ENS)...")
    sanity_diff = _assert_committed_equals_characterize(
        books["TSMOM_ENS"], panel, vol, accrual
    )

    # ── Align on common non-NaN window ────────────────────────────────────────
    common_idx = books[MENU[0]].dropna().index
    for lbl in MENU[1:]:
        common_idx = common_idx.intersection(books[lbl].dropna().index)
    n_common = len(common_idx)
    print(f"\n[4] Common non-NaN window: {common_idx.min().date()} → "
          f"{common_idx.max().date()}  ({n_common} days)")

    books_clean: dict[str, pd.Series] = {lbl: books[lbl].loc[common_idx] for lbl in MENU}

    # ── Turnover per book (scaled held book, == Task B accounting) ────────────
    print("\n[5] Annual turnover per book (scaled held book, common window):")
    turnover: dict[str, dict] = {}
    for L in TSMOM_LOOKBACKS:
        lbl = f"TSMOM_L{L}"
        sig = tsmom_signal(panel, lookback=L, vol_window=VOL_WINDOW)
        turnover[lbl] = _annual_turnover(lbl, sig, panel, vol, common_idx)
    ens_sig = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW)
    turnover["TSMOM_ENS"] = _annual_turnover("TSMOM_ENS", ens_sig, panel, vol, common_idx)
    for N in DONCHIAN_CHANNELS:
        lbl = f"DONCH_N{N}"
        sig = donchian_signal(panel, channel=N)
        turnover[lbl] = _annual_turnover(lbl, sig, panel, vol, common_idx)
    print(f"  {'Book':>11}  {'turn/yr':>9}  {'gross_avg':>9}  {'gross_p95':>9}")
    for lbl in MENU:
        t = turnover[lbl]
        print(f"  {lbl:>11}  {t['turn_per_yr']:>9.1f}  {t['gross_mean']:>9.3f}  "
              f"{t['gross_p95']:>9.3f}")

    # ── Full-period IS metrics (honest daily, sqrt(365)) ──────────────────────
    print("\n[6] Full-period IS metrics (HONEST daily, sqrt(365) via metrics_daily):")
    print(f"  {'Book':>11}  {'Sharpe':>8}  {'Ann%':>8}  {'MaxDD%':>8}  "
          f"{'Calmar':>8}  {'Vol%':>8}  {'Hit%':>7}  {'n':>5}")
    is_metrics: dict[str, dict] = {}
    for lbl in MENU:
        m = daily_metrics(books_clean[lbl])
        is_metrics[lbl] = m
        cal_str = f"{m['calmar']:>8.2f}" if not np.isnan(m.get("calmar", float("nan"))) else "     nan"
        print(f"  {lbl:>11}  {m['sharpe']:>8.3f}  {100*m['ann']:>8.2f}  "
              f"{100*m['maxdd']:>8.2f}  {cal_str}  "
              f"{100*m['vol_ann']:>8.2f}  {100*m['hit']:>7.1f}  {m['n']:>5d}")

    # ── Per-period Sharpe array for DSR deflation (N = menu size) ─────────────
    trial_sr = np.array([moments(books_clean[lbl].values).sr for lbl in MENU])
    n_menu = len(MENU)
    print(f"\n  Per-day Sharpe array (for DSR N={n_menu} deflation):")
    for lbl, sr in zip(MENU, trial_sr):
        print(f"    {lbl}: {sr:.6f}")

    # ── DSR for each book ─────────────────────────────────────────────────────
    print(f"\n[7] DSR (deflated against N={n_menu} = full menu):")
    print(f"  {'Book':>11}  {'per-day SR':>11}  "
          f"{'DSR(N={})'.format(n_menu):>11}  {'DSR(N=1)':>9}  "
          f"{'PSR_vs0':>9}  {'T':>6}  {'skew':>7}  {'kurt':>7}")
    dsr_menu_results: dict[str, dict] = {}
    dsr_n1_results:   dict[str, dict] = {}
    for lbl in MENU:
        d_menu = _dsr_with_menu(books_clean[lbl], trial_sr)
        d_n1   = _dsr_n1(books_clean[lbl])
        dsr_menu_results[lbl] = d_menu
        dsr_n1_results[lbl]   = d_n1
        print(f"  {lbl:>11}  {d_menu['sr_hat']:>11.6f}  "
              f"{d_menu['dsr']:>11.4f}  {d_n1['dsr']:>9.4f}  "
              f"{d_menu['psr_vs_zero']:>9.4f}  {d_menu['T']:>6d}  "
              f"{d_menu['skew']:>7.3f}  {d_menu['kurt']:>7.3f}")

    # ── CPCV OOS distributions ────────────────────────────────────────────────
    print(f"\n[8] CPCV OOS distribution (n_groups={N_GROUPS}, k={K_CPCV}, "
          f"purge={PURGE_DAYS}d, embargo={EMBARGO_DAYS}d):")
    print("    Note: OOS Sharpe/Ann below are computed via metrics_daily (sqrt(365)) =")
    print("    HONEST DAILY levels. (Harness's own HOURS_PER_YEAR=8760 annualization")
    print("    would INFLATE these ~×5.9 on Sharpe / ~×35 on ann — NOT used here.)")
    oos_results: dict[str, dict] = {}
    for lbl in MENU:
        print(f"\n    --- {lbl} ---")
        oos = _cpcv_oos_dist(books_clean[lbl].values, n_common)
        oos_results[lbl] = oos
        sh = oos["sharpe"]; an = oos["ann_pct"]; dd = oos["maxdd_pct"]
        print(f"    OOS segments: {oos['n_segments']}")
        print(f"    Sharpe(honest) — median={sh.get('median', float('nan')):+.3f}  "
              f"IQR=[{sh.get('iqr_lo', float('nan')):+.3f}, "
              f"{sh.get('iqr_hi', float('nan')):+.3f}]")
        print(f"    Ann%(honest)   — median={an.get('median', float('nan')):+.2f}%  "
              f"IQR=[{an.get('iqr_lo', float('nan')):+.2f}%, "
              f"{an.get('iqr_hi', float('nan')):+.2f}%]")
        print(f"    MaxDD%         — median={dd.get('median', float('nan')):.2f}%")
        print(f"    frac_sharpe_pos: {100*oos['frac_sharpe_pos']:.1f}%")

    # ── PBO across full menu ──────────────────────────────────────────────────
    print(f"\n[9] PBO across the {n_menu}-book menu (CSCV):")
    pbo_result = _pbo_across_menu({lbl: books_clean[lbl].values for lbl in MENU})
    print(f"    PBO = {pbo_result['pbo']:.4f}  (n_splits={pbo_result['n_splits']}, "
          f"S={pbo_result['S']}, n_configs={pbo_result['n_configs']})")
    print(f"    Median OOS rank of IS-best: {pbo_result['median_oos_rank']:.3f} "
          f"(1.0=best, 0.0=worst)")
    if pbo_result["is_best_counts"]:
        print(f"    IS-best frequency: {pbo_result['is_best_counts']}")

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY TABLE
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 104)
    print("SUMMARY TABLE — per book")
    print("  IS Sharpe = honest daily (sqrt365).  OOS med Sh(honest) = metrics_daily;")
    print("  the harness's own hourly annualization would INFLATE that ~×5.9 (shown).")
    print("=" * 104)
    hdr = (f"  {'Book':>11}  {'IS Sharpe':>10}  {'DSR(N=1)':>9}  "
           f"{f'DSR(N={n_menu})':>10}  {'OOSmedSh(h)':>12}  {'OOSmedSh(infl)':>14}  "
           f"{'OOS%Sh>0':>9}  {'turn/yr':>8}")
    print(hdr)
    print("  " + "-" * 100)
    SHARPE_INFL = float(np.sqrt(8760.0 / 365.0))  # ~4.899 harness-hourly inflation
    for lbl in MENU:
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl]
        sh_oos = oo["sharpe"].get("median", float("nan"))
        pct_pos = oo["frac_sharpe_pos"] * 100
        to = turnover[lbl]["turn_per_yr"]
        star = " *" if lbl == COMMITTED else "  "
        print(f"  {lbl:>9}{star}  {m['sharpe']:>10.3f}  {d_1['dsr']:>9.4f}  "
              f"{d_m['dsr']:>10.4f}  {sh_oos:>+12.3f}  {sh_oos*SHARPE_INFL:>+14.3f}  "
              f"{pct_pos:>8.1f}%  {to:>8.1f}")
    print(f"\n  ( * = committed book.  OOSmedSh(infl) = harness-hourly INFLATED view, "
          f"shown only to flag the ~×{SHARPE_INFL:.2f} distortion; honest = (h).)")

    # ══════════════════════════════════════════════════════════════════════════
    # COMMITTED BLOCK
    # ══════════════════════════════════════════════════════════════════════════
    c_dsr_menu = dsr_menu_results[COMMITTED]
    c_dsr_n1   = dsr_n1_results[COMMITTED]
    c_oos      = oos_results[COMMITTED]
    c_is       = is_metrics[COMMITTED]
    c_oos_sh   = c_oos["sharpe"]
    print("\n" + "=" * 92)
    print(f"COMMITTED BOOK = {COMMITTED}")
    print("=" * 92)
    print(f"  IS Sharpe (honest daily):  {c_is['sharpe']:+.3f}   "
          f"Ann {100*c_is['ann']:+.2f}%   MaxDD {100*c_is['maxdd']:.2f}%   "
          f"turn {turnover[COMMITTED]['turn_per_yr']:.1f}/yr")
    print(f"  DSR(committed, N={n_menu}):     {c_dsr_menu['dsr']:.4f}   "
          f"(PSR_vs0={c_dsr_menu['psr_vs_zero']:.4f})")
    print(f"  DSR(committed, N=1):       {c_dsr_n1['dsr']:.4f}")
    print(f"  Pooled-OOS Sharpe (HONEST daily): median={c_oos_sh.get('median', float('nan')):+.3f}  "
          f"IQR=[{c_oos_sh.get('iqr_lo', float('nan')):+.3f}, "
          f"{c_oos_sh.get('iqr_hi', float('nan')):+.3f}]  "
          f"(frac>0 = {100*c_oos['frac_sharpe_pos']:.1f}%)")
    print(f"  PBO(menu):                 {pbo_result['pbo']:.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    dsr_threshold = 0.95
    committed_dsr_menu = c_dsr_menu["dsr"]
    committed_dsr_n1   = c_dsr_n1["dsr"]
    committed_oos_med  = c_oos_sh.get("median", float("nan"))
    committed_oos_pos  = c_oos["frac_sharpe_pos"]
    pbo_val            = pbo_result["pbo"]

    if committed_dsr_menu > dsr_threshold:
        dsr_status = "PASS (>0.95)"
    elif committed_dsr_menu >= 0.5:
        dsr_status = "WARN (0.5-0.95)"
    else:
        dsr_status = "FAIL (<0.5)"

    oos_survives = (np.isfinite(committed_oos_med) and committed_oos_med > 0
                    and committed_oos_pos > 0.5)
    pbo_low  = pbo_val < 0.2
    pbo_high = pbo_val > 0.5

    if committed_dsr_menu > dsr_threshold and oos_survives and pbo_low:
        edge_call = "REAL"
    elif committed_dsr_menu >= 0.5 or (oos_survives and not pbo_high):
        edge_call = "MARGINAL"
    else:
        edge_call = "ABSENT"

    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)
    print(f"\n  [DSR]  committed {COMMITTED}: DSR(N={n_menu})={committed_dsr_menu:.4f} "
          f"[{dsr_status}]   DSR(N=1)={committed_dsr_n1:.4f}")
    print(f"  [OOS]  pooled-OOS median Sharpe (honest) = {committed_oos_med:+.3f}, "
          f"frac>0 = {100*committed_oos_pos:.1f}%  → "
          f"{'SURVIVES (positive, >50% splits)' if oos_survives else 'does NOT cleanly survive OOS'}")
    print(f"  [PBO]  {pbo_val:.4f}  → "
          f"{'LOW overfit (<0.2)' if pbo_low else ('HIGH overfit (>0.5)' if pbo_high else 'MODERATE (0.2-0.5)')}")

    verdict = (
        f"STANDALONE TREND EDGE: {edge_call}.\n"
        f"\n"
        f"  Committed book = {COMMITTED} (equal-weight TSMOM ensemble over "
        f"{TSMOM_LOOKBACKS}, fixed BEFORE OOS to avoid lookback cherry-picking).\n"
        f"  DSR(N={n_menu}) = {committed_dsr_menu:.4f} [{dsr_status}], "
        f"DSR(N=1) = {committed_dsr_n1:.4f}. Honest IS daily Sharpe "
        f"{c_is['sharpe']:+.3f} (ann {100*c_is['ann']:+.2f}%, maxDD "
        f"{100*c_is['maxdd']:.1f}%, turnover "
        f"{turnover[COMMITTED]['turn_per_yr']:.1f}/yr).\n"
        f"  Pooled-OOS median Sharpe (HONEST daily, sqrt365) = "
        f"{committed_oos_med:+.3f} with {100*committed_oos_pos:.1f}% of OOS segments "
        f"positive → {'survives' if oos_survives else 'does not cleanly survive'} OOS. "
        f"PBO(menu) = {pbo_val:.4f} "
        f"({'low' if pbo_low else ('high' if pbo_high else 'moderate')} overfit risk).\n"
    )
    if edge_call == "REAL":
        verdict += (
            "  The standalone directional trend edge clears the multi-test bar "
            "(DSR>0.95), survives CPCV OOS, and shows low overfitting. It is a REAL "
            "standalone return stream — proceed to Task D (the DECISIVE decorrelation-"
            "with-XSMOM test), since trend's value as a third stream rests primarily on "
            "low correlation with the live momentum book, not on standalone DSR alone."
        )
    elif edge_call == "MARGINAL":
        verdict += (
            "  The edge is MARGINAL: the committed ensemble does not clear the "
            "DSR>0.95 multi-test bar with only ~3 years of (mostly up-market) data, but "
            "it is not statistically dead either (DSR in the WARN zone and/or OOS "
            "positive without high PBO). On its own this is too thin to deploy as a "
            "standalone motor — consistent with the PLAN's sober prior that no robust "
            "25% CAGR exists in crypto. The DECISIVE question is Task D: does trend "
            "DECORRELATE from the live XSMOM book? A marginal-but-uncorrelated stream "
            "can still earn its place in a risk-parity carry+momentum+trend basket. "
            "Carry the committed TSMOM_ENS book forward to Task D for the correlation / "
            "risk-parity blend test."
        )
    else:
        verdict += (
            "  The standalone edge is effectively ABSENT: DSR(N=menu)<0.5 and/or the "
            "pooled-OOS Sharpe does not survive (non-positive or <50% of segments "
            "positive). Trend-following does not stand on its own as a return source on "
            "this universe/window. It may STILL be worth Task D purely as a "
            "decorrelating crisis-alpha overlay (its structural value), but it should "
            "NOT be built as a standalone motor."
        )
    print(f"\n  {verdict}")

    # ══════════════════════════════════════════════════════════════════════════
    # HONESTY CAVEATS
    # ══════════════════════════════════════════════════════════════════════════
    surv_json = json.loads(
        (_HERE.parent / "cross_sectional" / "crypto" / "survivorship.json").read_text()
    )
    n_dead = len(surv_json["extra_dead_coins_included"])
    inflation_caveat = (
        "HARNESS ANNUALIZATION IS HOURLY (HOURS_PER_YEAR=8760); our pnl is DAILY. "
        "Any annual_pct/Sharpe the HARNESS itself annualizes is INFLATED ~×35 on annual "
        f"and ~×{SHARPE_INFL:.2f} on Sharpe. ALL honest ABSOLUTE Sharpe/ann/OOS levels in "
        "this report and JSON are computed via metrics_daily (sqrt(365)) ONLY. DSR and PBO "
        "are ratio/ranking-based (period-agnostic) and are valid as-is."
    )
    caveats = [
        inflation_caveat,
        f"~3 years of data only ({n_common} days on the common window); low statistical "
        "power for clearing DSR>0.95, especially for a directional book whose payoff is "
        "concentrated in a few sustained trends.",
        f"Survivorship-debiased PT panel ({n_coins} coins, includes {n_dead} dead/delisted) "
        "→ conservative; the SAME panel as the XSMOM book → apples-to-apples for Task D.",
        "No sustained multi-quarter crypto bear market in the 2023-06→present window. "
        "Trend's structural crisis-alpha (net-short in bear) is therefore under-sampled; "
        "the standalone Sharpe here may understate trend's diversification value in a "
        "regime it never fully saw.",
        f"DSR deflation uses N={n_menu} (full menu) — conservative: the TSMOM legs share "
        "signal and are highly correlated, so the effective number of independent trials "
        f"is < {n_menu}. N=1 DSR is the more honest single-strategy view.",
        "purge=120d (= max menu lookback) on CPCV guarantees no signal leakage across "
        "train/test seams; embargo=7d. Signals + realized vol are causal (trend.py "
        "invariant); accrual = -funding.shift(-1) has no look-ahead.",
        "The committed pick (TSMOM_ENS) was fixed BEFORE looking at OOS per the PLAN, so "
        "the OOS/DSR reported for it is an honest held-out read, not a post-hoc winner.",
        "The harness (CPCV/DSR/PBO) was validated on reference series previously and "
        "imported UNCHANGED. The provenance assert proves the committed book fed here is "
        f"EXACTLY the TSMOM_ENS book characterize.py produced (max diff {sanity_diff:.1e}).",
        "STANDALONE DSR is NOT the deciding metric: per the PLAN, trend's value as a third "
        "stream rests primarily on DECORRELATION with the live XSMOM book (Task D). A "
        "marginal standalone edge can still be worth building as a diversifier.",
    ]
    print("\n[Honesty Caveats]")
    for i, c in enumerate(caveats, 1):
        print(f"  {i}. {c}")

    # ══════════════════════════════════════════════════════════════════════════
    # WRITE JSON OUTPUT
    # ══════════════════════════════════════════════════════════════════════════
    def _safe(v):
        if isinstance(v, float) and np.isnan(v):
            return None
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, (np.integer,)):
            return int(v)
        return v

    def _safe_dict(d):
        if not isinstance(d, dict):
            return d
        return {k: (_safe_dict(v) if isinstance(v, dict)
                    else ([_safe(x) for x in v] if isinstance(v, list)
                          else _safe(v)))
                for k, v in d.items()}

    per_book_out = {}
    for lbl in MENU:
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl].copy()
        oo.pop("all_oos_sharpes", None)
        t   = turnover[lbl]
        per_book_out[lbl] = {
            "type": ("tsmom" if lbl.startswith("TSMOM_L") else
                     "tsmom_ensemble" if lbl == "TSMOM_ENS" else "donchian"),
            "committed": (lbl == COMMITTED),
            "is_sharpe_daily": float(m["sharpe"]),
            "is_ann_pct": float(100 * m["ann"]),
            "is_vol_ann_pct": float(100 * m["vol_ann"]),
            "is_maxdd_pct": float(100 * m["maxdd"]),
            "is_calmar": _safe(m.get("calmar")),
            "is_hit_pct": float(100 * m["hit"]),
            "is_n_days": int(m["n"]),
            "turn_per_yr": float(t["turn_per_yr"]),
            "gross_mean": float(t["gross_mean"]),
            "gross_p95": float(t["gross_p95"]),
            f"dsr_n{n_menu}": float(d_m["dsr"]),
            f"dsr_n{n_menu}_sr_hat": float(d_m["sr_hat"]),
            f"dsr_n{n_menu}_sr_star": float(d_m["sr_star_deflated"]),
            f"dsr_n{n_menu}_psr_vs_zero": float(d_m["psr_vs_zero"]),
            "dsr_n1": float(d_1["dsr"]),
            "dsr_n1_psr_vs_zero": float(d_1["psr_vs_zero"]),
            "oos_cpcv_honest_daily": _safe_dict(oo),
        }

    out = {
        "test": "trend_following_validation",
        "task": "Task C of research/trend_following/PLAN.md",
        "description": (
            "CPCV+DSR+PBO validation of the directional trend-following menu "
            "(TSMOM L30/L60/L90/L120 + TSMOM ensemble + Donchian N20/N55/N100) on the "
            "survivorship-debiased PT panel (same universe as the XSMOM book). "
            f"Committed = {COMMITTED} (equal-weight ensemble, fixed BEFORE OOS)."
        ),
        "menu": MENU,
        "committed_pick": COMMITTED,
        "committed_reason": (
            "Equal-weight TSMOM ensemble over lookbacks (30,60,90,120) avoids "
            "cherry-picking a single lookback (per PLAN); committed BEFORE looking at OOS."
        ),
        "constants": {
            "VOL_TARGET": VOL_TARGET,
            "LEVERAGE_CAP": LEVERAGE_CAP,
            "COSTS_BPS": COSTS_BPS,
            "VOL_WINDOW": VOL_WINDOW,
            "tsmom_lookbacks": list(TSMOM_LOOKBACKS),
            "donchian_channels": list(DONCHIAN_CHANNELS),
            "annualization_honest": "metrics_daily PPY=365 sqrt(365) (honest absolute ONLY)",
        },
        "cpcv_params": {
            "n_groups": N_GROUPS, "k": K_CPCV,
            "purge_days": PURGE_DAYS, "embargo_days": EMBARGO_DAYS,
            "purge_rationale": "purge = max lookback in menu (TSMOM_L120 / DONCH_N100) = 120",
        },
        "pbo_S": int(pbo_result["S"]),
        "n_configs_in_menu": n_menu,
        "panel_window": {
            "start": str(date_min), "end": str(date_max),
            "n_days_full": int(n_days), "n_coins": int(n_coins),
        },
        "common_window": {
            "start": str(common_idx.min().date()),
            "end": str(common_idx.max().date()),
            "n_days": int(n_common),
        },
        "sanity_assert_committed_eq_characterize": {
            "passed": True,
            "max_abs_diff": float(sanity_diff),
            "note": ("Committed TSMOM_ENS pnl fed to the harness is EXACTLY the series "
                     "characterize.py (Task B) builds for TSMOM_ENS (replaces the "
                     "template's XSEC-specific run_book asserts, which do not apply)."),
        },
        "per_book": per_book_out,
        "committed_summary": {
            "is_sharpe_daily": float(c_is["sharpe"]),
            "is_ann_pct": float(100 * c_is["ann"]),
            "is_maxdd_pct": float(100 * c_is["maxdd"]),
            "turn_per_yr": float(turnover[COMMITTED]["turn_per_yr"]),
            f"dsr_n{n_menu}": float(committed_dsr_menu),
            "dsr_n1": float(committed_dsr_n1),
            "dsr_status": dsr_status,
            "oos_sharpe_honest_median": _safe(committed_oos_med),
            "oos_sharpe_honest_iqr": [
                _safe(c_oos_sh.get("iqr_lo")), _safe(c_oos_sh.get("iqr_hi"))
            ],
            "oos_frac_sharpe_pos": float(committed_oos_pos),
            "pbo_menu": float(pbo_val),
        },
        "pbo_across_menu": _safe_dict(pbo_result),
        "dsr_threshold": dsr_threshold,
        "edge_call": edge_call,
        "verdict": verdict,
        "inflation_caveat": inflation_caveat,
        "harness_sharpe_inflation_factor": SHARPE_INFL,
        "harness_ann_inflation_factor": 8760.0 / 365.0,
        "honesty_caveats": caveats,
    }

    out_path = _HERE / "trend_validation.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON written to {out_path}")
    return out


if __name__ == "__main__":
    main()
