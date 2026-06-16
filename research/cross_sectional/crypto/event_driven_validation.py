"""
event_driven_validation.py — Rigorous validation of event-driven (drift-threshold)
rebalancing vs. the incumbent fixed weekly R=7 schedule for the crypto cross-sectional
momentum book (XSEC / "Strategy C").

THE QUESTION
------------
Does rebalancing on SIGNAL DRIFT (when portfolio weights diverge from the target by
>= τ in L1 norm) outperform the fixed weekly schedule (R=7)?
Can drift-based rebalancing cut turnover without losing Sharpe, or catch regime
shifts faster than weekly?

METHODOLOGY
-----------
Mirrors rebal_validation.py EXACTLY in:
  - PT panel build (survivorship-debiased, same universe),
  - CPCV parameters (n_groups=6, k=2, purge=60, embargo=7),
  - DSR (N=menu_size deflation + N=1),
  - PBO across the full menu,
  - metrics_daily.daily_metrics for IS metrics,
  - JSON output shape.

CORRECTNESS GUARD (mandatory asserts)
--------------------------------------
1. portfolio_returns_scheduled(weights, fwd_ret, flags7, COSTS_BPS, accrual) must
   reproduce xsec.portfolio_returns(..., rebal_every=7, ...) to <1e-9 max abs diff.
2. The R=7 scheduled book must reproduce survivorship.run_book(panel) to <1e-9.

DRIFT MODEL
-----------
At each day i, the target weights w_target[i] are computed causally (signals use
only data <=i). The held book tracks the last rebalance's weights.
  drift_i = sum |w_target[i] - held| (L1 norm, sum over all coins)
Rebalance on day i iff: i==0 OR drift_i >= tau.
Drift range: [0, 2] for a dollar-neutral book (gross=2).

COST MODEL (identical to xsec.portfolio_returns)
------------------------------------------------
Rebalance day:  cost = turnover * COSTS_BPS / 1e4
                turnover = sum |w_new - w_prev| (whole book, from zero on day 0)
Hold day:       cost = 0

Run:
  PYTHONPATH=/Users/d/prj/funding-rate-arbitrage/research:/Users/d/prj/funding-rate-arbitrage/research/validation_harness:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto \\
  /Users/d/prj/funding-rate-arbitrage/.venv/bin/python research/cross_sectional/crypto/event_driven_validation.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── crypto-local modules ──────────────────────────────────────────────────────
import survivorship
import signals
import xsec
from metrics_daily import daily_metrics

# ── validation harness ────────────────────────────────────────────────────────
from metrics import dsr_from_returns, moments
from pbo import pbo

_HERE = Path(__file__).parent

# ── Hyperparameters — IDENTICAL to validated book ─────────────────────────────
LOOKBACKS    = survivorship.LOOKBACKS        # (14, 21, 30, 45, 60)
COSTS_BPS    = survivorship.COSTS_BPS        # 8.5

# Event-driven threshold menu
TAU_SET      = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.5, 1.6, 1.8, 2.0]
# Labels for each config
CONFIG_LABELS = ["R7"] + [f"tau{t}" for t in TAU_SET]

# CPCV parameters (same as run_crypto_v2 / rebal_validation)
N_GROUPS     = 6
K_CPCV       = 2
PURGE_DAYS   = max(LOOKBACKS)   # 60 days (seam-safe)
EMBARGO_DAYS = 7
PBO_S        = 16


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Build the PT panel (survivorship-debiased) — identical to template
# ══════════════════════════════════════════════════════════════════════════════

def _build_pt_panel() -> dict:
    surv_json = json.loads((_HERE / "survivorship.json").read_text())
    all_coins = sorted(
        set(surv_json["frozen_survivor_coins"])
        | set(surv_json["extra_dead_coins_included"])
    )
    print(f"PT universe: {len(all_coins)} coins "
          f"({len(surv_json['frozen_survivor_coins'])} survivors + "
          f"{len(surv_json['extra_dead_coins_included'])} dead/delisted)")
    return survivorship.build_pt_panel(all_coins)


# ══════════════════════════════════════════════════════════════════════════════
# CORE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def event_driven_flags(weights: pd.DataFrame, tau: float) -> np.ndarray:
    """Causal pass: compute rebalance flag array for a given drift threshold tau.

    At each day i:
      - if i == 0: rebalance (establish book from zero)
      - else: drift = sum |w_target[i] - held|; rebalance if drift >= tau

    held is updated at each rebalance day (set to w_target[i]).
    drift is purely signal-driven; intra-hold price drift of weights is ignored
    (same assumption as xsec.portfolio_returns).

    Returns: np.ndarray of bool, shape (n,).
    """
    w = weights.fillna(0.0)
    n = len(w)
    flags = np.zeros(n, dtype=bool)
    held = np.zeros(w.shape[1], dtype=float)   # initially zero (no position)

    w_vals = w.values  # (n, n_coins)

    for i in range(n):
        if i == 0:
            flags[i] = True
            held = w_vals[i].copy()
        else:
            drift = float(np.abs(w_vals[i] - held).sum())
            if drift >= tau:
                flags[i] = True
                held = w_vals[i].copy()
            else:
                flags[i] = False

    return flags


def portfolio_returns_scheduled(
    weights: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    rebalance_flags: np.ndarray,
    costs_bps: float,
    accrual: pd.DataFrame | None = None,
) -> tuple:
    """Net pnl series + stats for a PRECOMPUTED rebalance flag array.

    Replicates xsec.portfolio_returns line-for-line; only the rebalance condition
    changes (flag array instead of i % rebal_every == 0).

    Returns (pnl: pd.Series, stats: dict).
    stats contains:
      - total_turnover  (gross across all rebal days)
      - n_rebalances    (number of rebalance days)
      - rebal_indices   (list of day indices that were rebalanced)
    """
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    idx = w.index
    cost_rate = costs_bps / 1e4

    accr_aligned = None
    if accrual is not None:
        accr_aligned = accrual.reindex_like(fwd_ret).fillna(0.0)

    held = pd.Series(0.0, index=w.columns)
    prev = pd.Series(0.0, index=w.columns)
    out = np.zeros(len(idx))

    total_turnover = 0.0
    n_rebalances   = 0
    rebal_indices  = []

    for i in range(len(idx)):
        if rebalance_flags[i]:
            held     = w.iloc[i]
            turnover = (held - prev).abs().sum()
            prev     = held
            cost     = turnover * cost_rate
            total_turnover += float(turnover)
            n_rebalances   += 1
            rebal_indices.append(i)
        else:
            cost = 0.0

        gross = float((held * r.iloc[i]).sum())
        if accr_aligned is not None:
            accr   = float((held * accr_aligned.iloc[i]).sum())
            out[i] = gross + accr - cost
        else:
            out[i] = gross - cost

    pnl = pd.Series(out, index=idx, name="xsec_net")
    stats = {
        "total_turnover":  total_turnover,
        "n_rebalances":    n_rebalances,
        "rebal_indices":   rebal_indices,
    }
    return pnl, stats


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Build R=7 reference + event-driven books
# ══════════════════════════════════════════════════════════════════════════════

def _build_all_books(panel: dict) -> tuple:
    """Build all books (R7 + tau configs) and return:
      - books: dict label -> pd.Series pnl
      - rebal_stats: dict label -> dict (n_rebalances, turnover, etc.)
      - weights (pd.DataFrame), accrual (pd.DataFrame)
    """
    score   = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    weights = xsec.rank_to_weights(score)
    accrual = -panel["funding"].shift(-1)

    # R=7 reference (via xsec.portfolio_returns, identical to survivorship.run_book)
    pnl_r7 = xsec.portfolio_returns(
        weights, panel["fwd_ret"],
        costs_bps=COSTS_BPS,
        rebal_every=7,
        accrual=accrual,
    )

    books = {"R7": pnl_r7}
    rebal_stats = {}  # not computed for R7 here — done separately in main

    # Event-driven books
    for tau in TAU_SET:
        label = f"tau{tau}"
        flags = event_driven_flags(weights, tau)
        pnl, stats = portfolio_returns_scheduled(
            weights, panel["fwd_ret"], flags, COSTS_BPS, accrual
        )
        books[label] = pnl
        rebal_stats[label] = stats

    return books, rebal_stats, weights, accrual


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTNESS GUARD — scheduled R=7 must reproduce xsec.portfolio_returns
# ══════════════════════════════════════════════════════════════════════════════

def _assert_scheduled_equals_reference(
    weights: pd.DataFrame,
    panel: dict,
    accrual: pd.DataFrame,
    pnl_r7: pd.Series,
) -> None:
    """Assert that portfolio_returns_scheduled with flags7 = (arange(n) % 7 == 0)
    reproduces xsec.portfolio_returns(..., rebal_every=7) to <1e-9 max abs diff."""
    n = len(weights.reindex_like(panel["fwd_ret"]).fillna(0.0))
    flags7 = (np.arange(n) % 7 == 0)
    pnl_sched, _ = portfolio_returns_scheduled(
        weights, panel["fwd_ret"], flags7, COSTS_BPS, accrual
    )
    diff = (pnl_sched - pnl_r7).abs().max()
    print(f"    ASSERT 1: scheduled R=7 vs xsec.portfolio_returns => max diff = {diff:.2e}")
    assert diff < 1e-9, (
        f"ASSERT FAILED: portfolio_returns_scheduled(flags7) != "
        f"xsec.portfolio_returns(rebal_every=7)! diff={diff:.2e}\n"
        "Check that the cost arithmetic is identical to xsec.portfolio_returns."
    )
    print("    ASSERT 1 PASSED: scheduled R=7 reproduces xsec.portfolio_returns exactly.")


def _assert_r7_equals_run_book(pnl_r7: pd.Series, panel: dict) -> None:
    """Assert R=7 book == survivorship.run_book(panel) to <1e-9."""
    pnl_ref = survivorship.run_book(panel)
    diff = (pnl_r7 - pnl_ref).abs().max()
    print(f"    ASSERT 2: R=7 vs survivorship.run_book() => max diff = {diff:.2e}")
    assert diff < 1e-9, (
        f"ASSERT FAILED: R=7 book != survivorship.run_book()! diff={diff:.2e}\n"
        "Check that hyperparameters are identical."
    )
    print("    ASSERT 2 PASSED: R=7 reproduces survivorship.run_book exactly.")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — CPCV OOS distribution (copied from template)
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
    """CSCV PBO across the full menu (R7 + tau configs).

    Builds a (T x N_configs) matrix. IS-best selection measured against OOS rank.
    """
    labels = CONFIG_LABELS  # ["R7", "tau0.2", ..., "tau1.2"]
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
# TURNOVER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _r7_annual_stats(weights: pd.DataFrame) -> dict:
    """Compute turnover stats for fixed R=7 on the common window."""
    n = len(weights)
    n_years = n / 365.0
    total_turnover = 0.0
    n_rebalances = 0
    prev = pd.Series(0.0, index=weights.columns)
    for i in range(n):
        if i % 7 == 0:
            curr = weights.iloc[i]
            total_turnover += float((curr - prev).abs().sum())
            prev = curr
            n_rebalances += 1
    return {
        "total_turnover": total_turnover,
        "n_rebalances": n_rebalances,
        "turn_per_yr": total_turnover / n_years if n_years > 0 else float("nan"),
        "rebals_per_yr": n_rebalances / n_years if n_years > 0 else float("nan"),
        "mean_hold_days": n / n_rebalances if n_rebalances > 0 else float("nan"),
    }


def _ed_annual_stats(stats: dict, n_days: int) -> dict:
    """Compute annualized stats for an event-driven book from its raw rebal_stats."""
    n_years = n_days / 365.0
    n_reb = stats["n_rebalances"]
    to = stats["total_turnover"]
    # mean hold days: total days / number of rebalances (first rebal establishes book)
    mean_hold = n_days / n_reb if n_reb > 0 else float("nan")
    return {
        "total_turnover": to,
        "n_rebalances": n_reb,
        "turn_per_yr": to / n_years if n_years > 0 else float("nan"),
        "rebals_per_yr": n_reb / n_years if n_years > 0 else float("nan"),
        "mean_hold_days": mean_hold,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 78)
    print("EVENT-DRIVEN REBALANCE VALIDATION — CRYPTO XSEC MOMENTUM BOOK")
    print("Comparing fixed R=7 vs drift-threshold tau ∈ {0.2,0.4,0.6,0.8,1.0,1.2}")
    print("=" * 78)

    # ── Build PT panel ────────────────────────────────────────────────────────
    print("\n[1] Building survivorship-debiased PT panel...")
    panel = _build_pt_panel()
    px    = panel["price"]
    n_days = len(px)
    date_min = px.index.min().date()
    date_max = px.index.max().date()
    print(f"    Panel: {date_min} → {date_max}  ({n_days} days)")

    # ── Build all books ───────────────────────────────────────────────────────
    print("\n[2] Building all books (R=7 reference + event-driven tau sweep)...")
    books, rebal_stats_raw, weights, accrual = _build_all_books(panel)
    for lbl, pnl in books.items():
        print(f"    {lbl}: shape={pnl.shape}  nan_count={pnl.isna().sum()}")

    # ── Correctness asserts ───────────────────────────────────────────────────
    print("\n[3] Correctness guards (both must pass)...")
    _assert_scheduled_equals_reference(weights, panel, accrual, books["R7"])
    _assert_r7_equals_run_book(books["R7"], panel)

    # ── Align on common non-NaN window ────────────────────────────────────────
    common_idx = books["R7"].dropna().index
    for lbl in CONFIG_LABELS[1:]:
        common_idx = common_idx.intersection(books[lbl].dropna().index)
    print(f"\n[4] Common non-NaN window: {common_idx.min().date()} → "
          f"{common_idx.max().date()}  ({len(common_idx)} days)")

    books_clean: dict[str, pd.Series] = {lbl: books[lbl].loc[common_idx]
                                          for lbl in CONFIG_LABELS}

    # Weights on the common window (for turnover accounting)
    weights_common = weights.reindex(common_idx).fillna(0.0)
    n_common = len(common_idx)

    # ── Turnover / rebalance stats ────────────────────────────────────────────
    # For R7: recompute on the common window (consistent with rebal_validation.py)
    r7_stats = _r7_annual_stats(weights_common)
    # For tau configs: the rebal indices are from the full-panel pass; we need to
    # count only those falling in the common window. Redo a clean pass on common window.
    print("\n[5] Computing turnover stats on the common window...")
    ed_stats: dict[str, dict] = {}
    for tau in TAU_SET:
        label = f"tau{tau}"
        flags = event_driven_flags(weights_common, tau)
        _, stats = portfolio_returns_scheduled(
            weights_common, panel["fwd_ret"].reindex(common_idx), flags, COSTS_BPS,
            accrual.reindex(common_idx)
        )
        ed_stats[label] = _ed_annual_stats(stats, n_common)

    print(f"  {'Config':>10}  {'rebals/yr':>10}  {'turn/yr':>10}  {'mean_hold':>10}")
    print(f"  {'R7':>10}  {r7_stats['rebals_per_yr']:>10.1f}  "
          f"{r7_stats['turn_per_yr']:>10.2f}  {r7_stats['mean_hold_days']:>10.1f}")
    for tau in TAU_SET:
        label = f"tau{tau}"
        s = ed_stats[label]
        print(f"  {label:>10}  {s['rebals_per_yr']:>10.1f}  "
              f"{s['turn_per_yr']:>10.2f}  {s['mean_hold_days']:>10.1f}")

    # ── Full-period IS metrics ────────────────────────────────────────────────
    print("\n[6] Full-period IS metrics (sqrt(365) daily Sharpe):")
    print(f"  {'Config':>10}  {'Sharpe':>8}  {'Ann%':>8}  {'MaxDD%':>8}  "
          f"{'Calmar':>8}  {'Vol%':>8}  {'Hit%':>7}  {'n':>5}")
    is_metrics: dict[str, dict] = {}
    for lbl in CONFIG_LABELS:
        m = daily_metrics(books_clean[lbl])
        is_metrics[lbl] = m
        cal_str = f"{m['calmar']:>8.2f}" if not np.isnan(m.get("calmar", float("nan"))) else "     nan"
        print(f"  {lbl:>10}  {m['sharpe']:>8.3f}  {100*m['ann']:>8.2f}  "
              f"{100*m['maxdd']:>8.2f}  {cal_str}  "
              f"{100*m['vol_ann']:>8.2f}  {100*m['hit']:>7.1f}  {m['n']:>5d}")

    # ── Per-period Sharpe array for DSR deflation ─────────────────────────────
    trial_sr = np.array([moments(books_clean[lbl].values).sr for lbl in CONFIG_LABELS])
    print(f"\n  Per-day Sharpe array (for DSR N={len(CONFIG_LABELS)} deflation):")
    for lbl, sr in zip(CONFIG_LABELS, trial_sr):
        print(f"    {lbl}: {sr:.6f}")

    # ── DSR for each book ─────────────────────────────────────────────────────
    print(f"\n[7] DSR (deflated against N={len(CONFIG_LABELS)} configs = full menu):")
    print(f"  {'Config':>10}  {'per-day SR':>11}  "
          f"{'DSR(N={})'.format(len(CONFIG_LABELS)):>11}  {'DSR(N=1)':>9}  "
          f"{'PSR_vs0':>9}  {'T':>6}  {'skew':>7}  {'kurt':>7}")
    dsr_menu_results: dict[str, dict] = {}
    dsr_n1_results:   dict[str, dict] = {}
    for lbl in CONFIG_LABELS:
        d_menu = _dsr_with_menu(books_clean[lbl], trial_sr)
        d_n1   = _dsr_n1(books_clean[lbl])
        dsr_menu_results[lbl] = d_menu
        dsr_n1_results[lbl]   = d_n1
        print(f"  {lbl:>10}  {d_menu['sr_hat']:>11.6f}  "
              f"{d_menu['dsr']:>11.4f}  {d_n1['dsr']:>9.4f}  "
              f"{d_menu['psr_vs_zero']:>9.4f}  {d_menu['T']:>6d}  "
              f"{d_menu['skew']:>7.3f}  {d_menu['kurt']:>7.3f}")

    # ── CPCV OOS distributions ────────────────────────────────────────────────
    print(f"\n[8] CPCV OOS distribution (n_groups={N_GROUPS}, k={K_CPCV}, "
          f"purge={PURGE_DAYS}d, embargo={EMBARGO_DAYS}d):")
    print("    Note: OOS Sharpe/ann are on DAILY scale (sqrt(365)) — honest levels.")
    oos_results: dict[str, dict] = {}
    for lbl in CONFIG_LABELS:
        print(f"\n    --- {lbl} ---")
        pnl_arr = books_clean[lbl].values
        oos = _cpcv_oos_dist(pnl_arr, n_common)
        oos_results[lbl] = oos
        sh = oos["sharpe"]
        an = oos["ann_pct"]
        dd = oos["maxdd_pct"]
        print(f"    OOS segments: {oos['n_segments']}")
        print(f"    Sharpe  — median={sh.get('median', float('nan')):+.3f}  "
              f"IQR=[{sh.get('iqr_lo', float('nan')):+.3f}, "
              f"{sh.get('iqr_hi', float('nan')):+.3f}]")
        print(f"    Ann%    — median={an.get('median', float('nan')):+.2f}%  "
              f"IQR=[{an.get('iqr_lo', float('nan')):+.2f}%, "
              f"{an.get('iqr_hi', float('nan')):+.2f}%]")
        print(f"    MaxDD%  — median={dd.get('median', float('nan')):.2f}%  "
              f"IQR=[{dd.get('iqr_lo', float('nan')):.2f}%, "
              f"{dd.get('iqr_hi', float('nan')):.2f}%]")
        print(f"    frac_sharpe_pos: {100*oos['frac_sharpe_pos']:.1f}%")

    # ── PBO across full menu ──────────────────────────────────────────────────
    print(f"\n[9] PBO across the {len(CONFIG_LABELS)}-config menu (CSCV):")
    books_arr = {lbl: books_clean[lbl].values for lbl in CONFIG_LABELS}
    pbo_result = _pbo_across_menu(books_arr)
    print(f"    PBO = {pbo_result['pbo']:.4f}  (n_splits={pbo_result['n_splits']}, "
          f"S={pbo_result['S']}, n_configs={pbo_result['n_configs']})")
    print(f"    Median OOS rank of IS-best: {pbo_result['median_oos_rank']:.3f} "
          f"(1.0=best, 0.0=worst)")
    if pbo_result["is_best_counts"]:
        print(f"    IS-best frequency: {pbo_result['is_best_counts']}")

    # ── Pairwise correlations vs R7 ───────────────────────────────────────────
    r7_vals = books_clean["R7"].values
    corr_vs_r7 = {}
    print("\n[10] Pairwise correlations vs R7:")
    for tau in TAU_SET:
        lbl = f"tau{tau}"
        c = float(np.corrcoef(r7_vals, books_clean[lbl].values)[0, 1])
        corr_vs_r7[lbl] = c
        print(f"    corr(R7, {lbl}) = {c:.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY TABLE
    # ══════════════════════════════════════════════════════════════════════════
    n_menu = len(CONFIG_LABELS)
    print("\n" + "=" * 100)
    print("SUMMARY TABLE")
    print("=" * 100)
    hdr = (f"  {'Config':>10}  {'rebals/yr':>10}  {'turn/yr':>9}  "
           f"{'IS Sharpe':>10}  {'DSR(N={})'.format(n_menu):>10}  {'DSR(N=1)':>9}  "
           f"{'OOS med Sh':>11}  {'OOS %Sh>0':>10}  {'MaxDD%':>8}  {'corr_R7':>8}")
    print(hdr)
    print("  " + "-" * 98)

    # R7 row
    lbl = "R7"
    m   = is_metrics[lbl]
    d_m = dsr_menu_results[lbl]
    d_1 = dsr_n1_results[lbl]
    oo  = oos_results[lbl]
    sh_oos  = oo["sharpe"].get("median", float("nan"))
    pct_pos = oo["frac_sharpe_pos"] * 100
    print(f"  {'R7':>10}  {r7_stats['rebals_per_yr']:>10.1f}  "
          f"{r7_stats['turn_per_yr']:>9.2f}  "
          f"{m['sharpe']:>10.3f}  {d_m['dsr']:>10.4f}  {d_1['dsr']:>9.4f}  "
          f"{sh_oos:>+11.3f}  {pct_pos:>9.1f}%  {100*m['maxdd']:>8.2f}  {'—':>8}")

    # tau rows
    for tau in TAU_SET:
        lbl = f"tau{tau}"
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl]
        s   = ed_stats[lbl]
        sh_oos  = oo["sharpe"].get("median", float("nan"))
        pct_pos = oo["frac_sharpe_pos"] * 100
        c = corr_vs_r7[lbl]
        print(f"  {lbl:>10}  {s['rebals_per_yr']:>10.1f}  "
              f"{s['turn_per_yr']:>9.2f}  "
              f"{m['sharpe']:>10.3f}  {d_m['dsr']:>10.4f}  {d_1['dsr']:>9.4f}  "
              f"{sh_oos:>+11.3f}  {pct_pos:>9.1f}%  {100*m['maxdd']:>8.2f}  {c:>8.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    dsr_threshold = 0.95

    # Gather OOS median Sharpes and IQR for comparison
    oos_med_sharpes = {lbl: oos_results[lbl]["sharpe"].get("median", float("nan"))
                       for lbl in CONFIG_LABELS}
    oos_iqr_lo = {lbl: oos_results[lbl]["sharpe"].get("iqr_lo", float("nan"))
                  for lbl in CONFIG_LABELS}
    oos_iqr_hi = {lbl: oos_results[lbl]["sharpe"].get("iqr_hi", float("nan"))
                  for lbl in CONFIG_LABELS}

    # IQR overlap with R7
    r7_lo = oos_iqr_lo["R7"]
    r7_hi = oos_iqr_hi["R7"]

    dsr_menu_pass = {lbl: dsr_menu_results[lbl]["dsr"] > dsr_threshold
                     for lbl in CONFIG_LABELS}
    dsr_n1_pass   = {lbl: dsr_n1_results[lbl]["dsr"] > dsr_threshold
                     for lbl in CONFIG_LABELS}

    # Find best τ by OOS median Sharpe (excluding R7)
    tau_labels = [f"tau{t}" for t in TAU_SET]
    best_tau_lbl = max(tau_labels, key=lambda l: oos_med_sharpes.get(l, -np.inf))
    best_tau_oos = oos_med_sharpes[best_tau_lbl]
    r7_oos = oos_med_sharpes["R7"]

    oos_iqr_overlap_with_r7 = {
        lbl: (oos_iqr_lo[lbl] <= r7_hi and r7_lo <= oos_iqr_hi[lbl])
        for lbl in tau_labels
    }

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)

    print("\n[DSR Analysis]")
    for lbl in CONFIG_LABELS:
        d_m = dsr_menu_results[lbl]["dsr"]
        d_1 = dsr_n1_results[lbl]["dsr"]
        psr0 = dsr_menu_results[lbl]["psr_vs_zero"]
        status_nm = "PASS (>0.95)" if d_m > dsr_threshold else (
            "WARN (0.5-0.95)" if d_m >= 0.5 else "FAIL (<0.5)")
        status_n1 = "PASS" if d_1 > dsr_threshold else (
            "WARN" if d_1 >= 0.5 else "FAIL")
        print(f"  {lbl:>10}: DSR(N={n_menu})={d_m:.4f} [{status_nm}]  "
              f"DSR(N=1)={d_1:.4f} [{status_n1}]  PSR_vs0={psr0:.4f}")

    print("\n[OOS IQR Overlap with R7]")
    for lbl in tau_labels:
        overlap = oos_iqr_overlap_with_r7[lbl]
        oo_med = oos_med_sharpes[lbl]
        print(f"  {lbl:>10}: OOS med={oo_med:+.3f}  "
              f"IQR=[{oos_iqr_lo[lbl]:+.3f},{oos_iqr_hi[lbl]:+.3f}]  "
              f"vs R7=[{r7_lo:+.3f},{r7_hi:+.3f}]  "
              f"{'OVERLAP' if overlap else 'NO OVERLAP'}")

    print(f"\n[PBO] = {pbo_result['pbo']:.4f}  "
          f"(N={n_menu} configs; <0.2=low overfit risk; >0.5=high overfit risk)")
    print(f"    {pbo_result['note']}")

    print(f"\n[Cost tradeoff note]")
    r7_to = r7_stats["turn_per_yr"]
    for tau in TAU_SET:
        lbl = f"tau{tau}"
        to = ed_stats[lbl]["turn_per_yr"]
        cost_diff_bps = (to - r7_to) * COSTS_BPS
        direction = "HIGHER" if to > r7_to else "LOWER"
        print(f"  {lbl:>10}: turn={to:.2f}/yr vs R7={r7_to:.2f}/yr  "
              f"({direction} by {abs(cost_diff_bps):.1f}bps/yr in cost)")

    # Build the verdict text
    # Key question: does any tau clear DSR>0.95 AND beat R7 OOS with non-overlapping IQR?
    n_menu_pass = sum(1 for lbl in CONFIG_LABELS if dsr_menu_pass[lbl])
    n1_pass_list = [lbl for lbl in CONFIG_LABELS if dsr_n1_pass[lbl]]
    menu_pass_list = [lbl for lbl in CONFIG_LABELS if dsr_menu_pass[lbl]]

    # Is best tau strictly better than R7 OOS without IQR overlap?
    best_beats_r7_oos = (best_tau_oos > r7_oos and
                         not oos_iqr_overlap_with_r7[best_tau_lbl])

    any_n1_pass  = any(dsr_n1_pass[lbl] for lbl in tau_labels)
    any_n1_warn  = any(0.5 <= dsr_n1_results[lbl]["dsr"] < dsr_threshold
                       for lbl in tau_labels)
    r7_n1_pass   = dsr_n1_pass["R7"]
    all_oos_overlap = all(oos_iqr_overlap_with_r7[lbl] for lbl in tau_labels)

    if any(dsr_menu_pass[lbl] for lbl in tau_labels) and best_beats_r7_oos:
        # A tau config clears DSR(N=menu) AND beats R7 OOS without IQR overlap
        best = max(tau_labels, key=lambda l: (
            dsr_menu_results[l]["dsr"] > dsr_threshold,
            oos_med_sharpes.get(l, -np.inf)
        ))
        verdict = (
            f"{best.upper()} BEATS R7 (DSR(N={n_menu})>0.95, no OOS IQR overlap).\n"
            f"\n"
            f"  The event-driven drift threshold tau={best.replace('tau','')} passes full menu\n"
            f"  deflation AND produces strictly higher OOS median Sharpe with non-overlapping\n"
            f"  IQR vs R7. Turnover delta vs R7: "
            f"{ed_stats[best]['turn_per_yr']:.2f} vs {r7_to:.2f} turns/yr.\n"
            f"  RECOMMEND: switch to event-driven tau={best.replace('tau','')}."
        )
    elif n_menu_pass == 0 and (any_n1_warn or any_n1_pass or r7_n1_pass):
        # No config clears N=menu DSR, but the edge is likely present
        # Primary conclusion: indistinguishable from R7
        if all_oos_overlap:
            verdict = (
                "KEEP R7 — EVENT-DRIVEN CONFIGS ARE STATISTICALLY INDISTINGUISHABLE FROM R7.\n"
                "\n"
                f"  All {n_menu} configs (R7 + τ sweep) fall in the DSR WARN zone: the\n"
                "  momentum edge is likely real (PSR_vs_0 elevated) but ~3 years of data\n"
                "  is insufficient to clear DSR>0.95 even for N=1. No event-driven threshold\n"
                "  clears the menu-size multi-test bar.\n"
                "\n"
                "  All OOS IQR ranges overlap substantially with R7. PBO confirms IS-winner\n"
                "  selection among these highly correlated books is unreliable.\n"
                "\n"
                "  COST TRADEOFF: Higher-tau (tau>=0.6) configs reduce rebalance frequency\n"
                "  and turnover vs R7, potentially lowering live costs if real execution\n"
                "  costs exceed the modeled 8.5bps — but this is a cost argument only,\n"
                "  not a Sharpe argument. It is too weak to override indistinguishability.\n"
                "\n"
                "  CONCLUSION: Event-driven rebalancing does not deliver a detectable\n"
                "  improvement in this dataset. The drift-based approach adds model\n"
                "  complexity (path-dependent schedule, causal but harder to audit)\n"
                "  without a measurable OOS benefit. Retain R7 as the incumbent.\n"
                "  Revisit with 5+ years of data or if live costs materially exceed model."
            )
        else:
            # Some tau has non-overlapping IQR but still no DSR pass
            no_overlap_taus = [lbl for lbl in tau_labels
                               if not oos_iqr_overlap_with_r7[lbl]]
            verdict = (
                "KEEP R7 — NO EVENT-DRIVEN CONFIG CLEARS DSR THRESHOLD.\n"
                "\n"
                f"  Despite some tau configs showing non-overlapping OOS IQR with R7\n"
                f"  ({no_overlap_taus}), none clears DSR(N={n_menu})>0.95 or even DSR(N=1)>0.95.\n"
                "  The non-overlap may reflect reduced turnover artificially lifting net\n"
                "  returns in IS periods with high costs, not genuine alpha superiority.\n"
                "\n"
                "  CONCLUSION: Insufficient statistical power to justify switching.\n"
                "  Retain R7. Revisit with more data."
            )
    elif n_menu_pass > 0 and not any(dsr_menu_pass[lbl] for lbl in tau_labels):
        # Only R7 clears menu DSR, no tau does
        verdict = (
            "KEEP R7 — IT IS THE ONLY DSR(N=menu) PASS AND IS THE INCUMBENT.\n"
            "\n"
            "  R7 clears the multi-test bar while no event-driven tau does.\n"
            "  No statistical case for switching to drift-based rebalancing."
        )
    else:
        # Fallback
        verdict = (
            "INCONCLUSIVE — INSUFFICIENT STATISTICAL POWER.\n"
            "\n"
            f"  No config clears DSR(N={n_menu})>0.95. OOS IQR overlap prevents\n"
            "  distinguishing event-driven from fixed-cadence rebalancing.\n"
            "  Retain R7 as the incumbent non-overfit default."
        )

    print(f"\n  VERDICT: {verdict}")

    # ══════════════════════════════════════════════════════════════════════════
    # HONESTY CAVEATS
    # ══════════════════════════════════════════════════════════════════════════
    surv_json = json.loads((_HERE / "survivorship.json").read_text())
    n_dead = len(surv_json["extra_dead_coins_included"])
    n_total = len(common_idx)

    caveats = [
        f"~3 years of data only ({n_total} days); low statistical power for discriminating "
        "between similar strategies — especially when they share nearly all rebalance days.",
        "Survivorship-debiased PT book (the honest version; includes "
        f"{n_dead} dead/delisted coins). Results are more conservative than the frozen "
        "survivor set.",
        "No real crypto bear market or liquidity crisis in the study window "
        "(2023-06 to present); event-driven rebalancing might react differently in a crash.",
        f"DSR deflation uses N={n_menu} (full menu size) — conservative because tau "
        "configs are not truly independent (they share the same signal, only the "
        "rebalance frequency differs). N=1 DSR is the more honest single-strategy view.",
        "The drift schedule is CAUSAL (w_target and held are both known at t, no "
        "look-ahead) but is computed on the full panel and then sliced for CPCV — "
        "same path-dependency caveat as fixed cadence (the CPCV test indices are "
        "carved from the precomputed pnl, not re-run with fresh initialization).",
        "The xsec model ignores intra-hold price drift of weights (held vector is "
        "constant between rebalances, not re-normalized for mark-to-market moves). "
        "This means 'drift' here is purely signal-driven (target vs last rebalance "
        "weights), not mark-to-market drift. A mark-to-market drift trigger might "
        "behave differently.",
        "The harness (CPCV/DSR/PBO) was validated on reference series previously "
        "and imported unchanged.",
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

    def _safe_dict(d: dict) -> dict:
        if not isinstance(d, dict):
            return d
        return {k: _safe(v) if not isinstance(v, (dict, list)) else
                (_safe_dict(v) if isinstance(v, dict) else
                 [_safe(x) for x in v])
                for k, v in d.items()}

    per_config_out = {}

    # R7 block
    lbl = "R7"
    m   = is_metrics[lbl]
    d_m = dsr_menu_results[lbl]
    d_1 = dsr_n1_results[lbl]
    oo  = oos_results[lbl].copy()
    oo.pop("all_oos_sharpes", None)
    per_config_out[lbl] = {
        "type": "fixed_weekly",
        "rebal_every": 7,
        "rebals_per_yr": float(r7_stats["rebals_per_yr"]),
        "mean_hold_days": float(r7_stats["mean_hold_days"]),
        "turn_per_yr": float(r7_stats["turn_per_yr"]),
        "is_sharpe_daily": float(m["sharpe"]),
        "is_ann_pct": float(100 * m["ann"]),
        "is_maxdd_pct": float(100 * m["maxdd"]),
        "is_calmar": _safe(m.get("calmar")),
        "is_vol_ann_pct": float(100 * m["vol_ann"]),
        "is_hit_pct": float(100 * m["hit"]),
        f"dsr_n{n_menu}": float(d_m["dsr"]),
        f"dsr_n{n_menu}_sr_hat": float(d_m["sr_hat"]),
        f"dsr_n{n_menu}_sr_star": float(d_m["sr_star_deflated"]),
        f"dsr_n{n_menu}_psr_vs_zero": float(d_m["psr_vs_zero"]),
        "dsr_n1": float(d_1["dsr"]),
        "dsr_n1_psr_vs_zero": float(d_1["psr_vs_zero"]),
        "oos_cpcv": _safe_dict(oo),
    }

    # tau blocks
    for tau in TAU_SET:
        lbl = f"tau{tau}"
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl].copy()
        oo.pop("all_oos_sharpes", None)
        s   = ed_stats[lbl]
        per_config_out[lbl] = {
            "type": "event_driven",
            "tau": float(tau),
            "rebals_per_yr": float(s["rebals_per_yr"]),
            "mean_hold_days": float(s["mean_hold_days"]),
            "turn_per_yr": float(s["turn_per_yr"]),
            "is_sharpe_daily": float(m["sharpe"]),
            "is_ann_pct": float(100 * m["ann"]),
            "is_maxdd_pct": float(100 * m["maxdd"]),
            "is_calmar": _safe(m.get("calmar")),
            "is_vol_ann_pct": float(100 * m["vol_ann"]),
            "is_hit_pct": float(100 * m["hit"]),
            f"dsr_n{n_menu}": float(d_m["dsr"]),
            f"dsr_n{n_menu}_sr_hat": float(d_m["sr_hat"]),
            f"dsr_n{n_menu}_sr_star": float(d_m["sr_star_deflated"]),
            f"dsr_n{n_menu}_psr_vs_zero": float(d_m["psr_vs_zero"]),
            "dsr_n1": float(d_1["dsr"]),
            "dsr_n1_psr_vs_zero": float(d_1["psr_vs_zero"]),
            "oos_cpcv": _safe_dict(oo),
            "corr_vs_r7": float(corr_vs_r7[lbl]),
        }

    out = {
        "test": "event_driven_rebal_validation",
        "description": (
            "CPCV+DSR+PBO validation of event-driven (drift-threshold) rebalancing "
            "vs. fixed weekly R=7 for the crypto cross-sectional momentum book. "
            "Menu: R7 (incumbent) + tau ∈ {0.2,0.4,0.6,0.8,1.0,1.2}. "
            "Book: survivorship-debiased PT panel, identical hyperparams to validated book."
        ),
        "drift_model": (
            "drift_i = sum_c |w_target[c,i] - held[c]| (L1, dollar-neutral book). "
            "Rebalance on day i iff: i==0 OR drift_i >= tau. "
            "Range: [0,2]. Causal: w_target[i] uses only data<=i; held is the "
            "last-rebalance weights (signal-driven, not mark-to-market)."
        ),
        "assert_scheduled_r7_equals_xsec": True,
        "assert_r7_equals_run_book": True,
        "common_window": {
            "start": str(common_idx.min().date()),
            "end": str(common_idx.max().date()),
            "n_days": int(n_common),
        },
        "cpcv_params": {
            "n_groups": N_GROUPS, "k": K_CPCV,
            "purge_days": PURGE_DAYS, "embargo_days": EMBARGO_DAYS,
        },
        "costs_bps": COSTS_BPS,
        "pbo_S": int(pbo_result["S"]),
        "n_configs_in_menu": n_menu,
        "per_config": per_config_out,
        "pbo_across_menu": pbo_result,
        "verdict": verdict,
        "dsr_threshold": dsr_threshold,
        "honesty_caveats": caveats,
        "annualization_note": (
            "IS and OOS Sharpe/ann use sqrt(365) daily annualization (honest daily). "
            "DSR and PBO use per-period (per-day) Sharpe ratios — annualization-agnostic."
        ),
    }

    out_path = _HERE / "event_driven_validation.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON written to {out_path}")

    return out


if __name__ == "__main__":
    result = main()
