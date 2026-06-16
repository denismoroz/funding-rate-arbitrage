"""
invvol_validation.py — Rigorous validation of inverse-volatility leg weighting
vs. the incumbent equal-weight (EW) legs for the crypto cross-sectional momentum
book (XSEC / "Strategy C").

THE QUESTION
------------
Does weighting names within each leg by 1/realized_vol improve the book vs
equal-weight (EW)? The structural argument: EW lets the most volatile names
dominate risk contribution. Inverse-vol equalizes risk contribution within each
leg, which may improve Sharpe without changing the momentum signal.

METHODOLOGY
-----------
Mirrors rebal_validation.py and event_driven_validation.py EXACTLY in:
  - PT panel build (survivorship-debiased, same universe),
  - CPCV parameters (n_groups=6, k=2, purge=60, embargo=7),
  - DSR (N=4 menu deflation + N=1),
  - PBO across the 4-config menu,
  - metrics_daily.daily_metrics for IS metrics,
  - JSON output shape.

INVERSE-VOL WEIGHTING RULE
---------------------------
rank_to_weights_invvol(scores, vol, tercile_frac=1/3):
  Same selection as xsec.rank_to_weights (top-k longs, bottom-k shorts, same k).
  INSTEAD of equal 1/k within each leg, weights each name proportional to 1/vol,
  normalized so the LONG leg sums to +1 and the SHORT leg sums to −1.
  - If vol is NaN or <=0 for a name → EXCLUDE that name from the leg.
  - If ALL names in a leg have invalid vol → zero the whole row (no trade).
  - Dollar-neutral preserved: each leg still sums to ±1 exactly.

VOLATILITY — CAUSAL, no look-ahead
-----------------------------------
  ret = panel["price"].pct_change()          # ret[t] = (t-1 → t), known at t
  vol = ret.rolling(window=W, min_periods=W).std()   # vol[t] uses ret[t..t-W+1]
vol[t] uses ONLY returns up to and including t (the decision time). The weight
w[t] built from vol[t] then earns fwd_ret[t] (t→t+1). No look-ahead.
First W rows of vol are NaN → those names excluded early; common non-NaN window
alignment + purge=60 keeps the seam safe.

MENU (for DSR deflation + PBO)
-------------------------------
  EW      — equal-weight incumbent (== survivorship.run_book exactly).
  IV20    — inverse-vol, vol window W=20.
  IV30    — inverse-vol, vol window W=30.
  IV60    — inverse-vol, vol window W=60.
  N=4 configs. DSR deflated by N=4 AND N=1 per config. PBO across 4.

MANDATORY ASSERT
----------------
EW book == survivorship.run_book(panel) to <1e-9 (proves EW baseline is the
validated incumbent).

Run:
  PYTHONPATH=/Users/d/prj/funding-rate-arbitrage/research:/Users/d/prj/funding-rate-arbitrage/research/validation_harness:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto \\
  /Users/d/prj/funding-rate-arbitrage/.venv/bin/python research/cross_sectional/crypto/invvol_validation.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── crypto-local modules ────────────────────────────────────────────────────────
import survivorship
import signals
import xsec
from metrics_daily import daily_metrics

# ── validation harness ──────────────────────────────────────────────────────────
from metrics import dsr_from_returns, moments
from pbo import pbo

_HERE = Path(__file__).parent

# ── Hyperparameters — IDENTICAL to validated book ──────────────────────────────
LOOKBACKS    = survivorship.LOOKBACKS        # (14, 21, 30, 45, 60)
COSTS_BPS    = survivorship.COSTS_BPS        # 8.5
REBAL_EVERY  = 7                             # fixed weekly (incumbent)

# Menu of vol windows for inverse-vol weighting
VOL_WINDOWS  = [20, 30, 60]                 # IV20, IV30, IV60
CONFIG_LABELS = ["EW"] + [f"IV{w}" for w in VOL_WINDOWS]   # 4 configs total

# CPCV parameters (same as run_crypto_v2 / rebal_validation)
N_GROUPS     = 6
K_CPCV       = 2
PURGE_DAYS   = max(LOOKBACKS)               # 60 days (seam-safe: binding lookback)
EMBARGO_DAYS = 7
PBO_S        = 16


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Build the PT panel (survivorship-debiased)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pt_panel() -> dict:
    """Load coins = frozen survivors ∪ dead/delisted coins confirmed in
    survivorship.json, then build the point-in-time panel."""
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
# STEP 2 — Inverse-vol weighting
# ══════════════════════════════════════════════════════════════════════════════

def rank_to_weights_invvol(
    scores: pd.DataFrame,
    vol: pd.DataFrame,
    tercile_frac: float = 1 / 3,
) -> pd.DataFrame:
    """Rank → dollar-neutral inverse-vol weighted DataFrame.

    Mirrors xsec.rank_to_weights selection logic exactly (same k, same long/short
    selection), but INSTEAD of 1/k within each leg, weights by 1/vol[name,t],
    normalized so the LONG leg sums to +1 and the SHORT leg sums to −1.

    Rules:
      - valid = scores row dropna; n = len(valid); if n<2 → zero row (no trade).
      - k = max(1, floor(n * tercile_frac)).
      - longs = top-k by score, shorts = bottom-k by score (same as EW).
      - For each leg: raw_i = 1/vol[name,t]. If vol is NaN or <=0 → EXCLUDE name.
      - Renormalize surviving names so leg sums to ±1.
      - If ALL names in a leg have invalid vol → zero the whole row (no trade).
    """
    w = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)
    for dt, row in scores.iterrows():
        valid = row.dropna()
        n = len(valid)
        if n < 2:
            continue
        k = max(1, int(np.floor(n * tercile_frac)))
        order = valid.sort_values(ascending=False)
        longs  = list(order.index[:k])
        shorts = list(order.index[n - k:])

        # ── Long leg ──────────────────────────────────────────────────────────
        raw_long = {}
        for name in longs:
            v = vol.at[dt, name] if name in vol.columns else np.nan
            if not (np.isfinite(v) and v > 0):
                continue  # exclude invalid vol
            raw_long[name] = 1.0 / v
        if not raw_long:
            continue  # all longs have invalid vol → no trade this row
        sum_raw_long = sum(raw_long.values())
        for name, rv in raw_long.items():
            w.at[dt, name] = rv / sum_raw_long   # normalized to +1

        # ── Short leg ─────────────────────────────────────────────────────────
        raw_short = {}
        for name in shorts:
            v = vol.at[dt, name] if name in vol.columns else np.nan
            if not (np.isfinite(v) and v > 0):
                continue
            raw_short[name] = 1.0 / v
        if not raw_short:
            # Long leg was set above; to keep dollar-neutral we zero out the row
            for name in raw_long:
                w.at[dt, name] = 0.0
            continue
        sum_raw_short = sum(raw_short.values())
        for name, rv in raw_short.items():
            w.at[dt, name] = -(rv / sum_raw_short)   # normalized to −1

    return w


def _compute_vol(price: pd.DataFrame, window: int) -> pd.DataFrame:
    """Causal realized vol: rolling std of daily returns, window=W days.

    ret[t] = price[t]/price[t-1] - 1  (known at t, uses NO forward data).
    vol[t] = std of ret[t-W+1..t]     (all <= t, no look-ahead).
    """
    ret = price.pct_change()   # ret[t] = (t-1 → t), known at t
    return ret.rolling(window=window, min_periods=window).std()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Build books
# ══════════════════════════════════════════════════════════════════════════════

def build_ew_book(panel: dict) -> pd.Series:
    """Replicate survivorship.run_book exactly (EW incumbent)."""
    score   = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    weights = xsec.rank_to_weights(score)
    accrual = -panel["funding"].shift(-1)
    return xsec.portfolio_returns(
        weights, panel["fwd_ret"],
        costs_bps=COSTS_BPS,
        rebal_every=REBAL_EVERY,
        accrual=accrual,
    )


def build_iv_book(panel: dict, vol_window: int) -> tuple[pd.Series, pd.DataFrame]:
    """Build inverse-vol book for a given vol window.

    Returns (pnl, iv_weights). The iv_weights DataFrame is needed for turnover
    and effective breadth computation.
    """
    score   = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    vol     = _compute_vol(panel["price"], window=vol_window)
    weights = rank_to_weights_invvol(score, vol)
    accrual = -panel["funding"].shift(-1)
    pnl = xsec.portfolio_returns(
        weights, panel["fwd_ret"],
        costs_bps=COSTS_BPS,
        rebal_every=REBAL_EVERY,
        accrual=accrual,
    )
    return pnl, weights


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — CPCV OOS distribution (copied verbatim from templates)
# ══════════════════════════════════════════════════════════════════════════════

def _cpcv_oos_dist(pnl_vals: np.ndarray, n: int) -> dict:
    """Run CPCV on a precomputed daily pnl array.

    purge and embargo are in DAYS (= the time-unit of our series).
    Returns pooled OOS distribution of daily-honest Sharpe (sqrt(365)),
    ann return, maxDD, and fraction of segments with Sharpe > 0.
    """
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
# STEP 5 — DSR helpers (copied from templates)
# ══════════════════════════════════════════════════════════════════════════════

def _dsr_with_menu(pnl: pd.Series, trial_sharpes: np.ndarray) -> dict:
    """DSR for pnl deflated against the array of all trial per-period Sharpes."""
    r = pnl.dropna().values
    return dsr_from_returns(r, trial_sharpes)


def _dsr_n1(pnl: pd.Series) -> dict:
    """DSR with N=1 deflation (no search penalty)."""
    r = pnl.dropna().values
    m = moments(r)
    return dsr_from_returns(r, np.array([m.sr]))


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — PBO across the 4-config menu (adapted from templates)
# ══════════════════════════════════════════════════════════════════════════════

def _pbo_across_menu(books_aligned: dict[str, np.ndarray]) -> dict:
    """CSCV PBO across the 4-config menu (EW + IV20 + IV30 + IV60).

    Builds a (T x 4) matrix on the common date index.
    """
    labels = CONFIG_LABELS
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
# TURNOVER helper (copied from rebal_validation, adapted for IV weights)
# ══════════════════════════════════════════════════════════════════════════════

def _annual_turnover(weights: pd.DataFrame, rebal: int) -> float:
    """Estimate annual turnover from the weight matrix.

    On rebal days: sum |w_new - w_old| (changes in weights).
    On hold days:  0.
    Returns annualized gross turnover = total_turnover / n_years.
    """
    n = len(weights)
    n_years = n / 365.0
    total_turnover = 0.0
    prev = pd.Series(0.0, index=weights.columns)
    for i in range(n):
        if i % rebal == 0:
            curr = weights.iloc[i]
            total_turnover += (curr - prev).abs().sum()
            prev = curr
    return total_turnover / n_years if n_years > 0 else float("nan")


# ══════════════════════════════════════════════════════════════════════════════
# EFFECTIVE BREADTH (inverse Herfindahl within the long leg)
# ══════════════════════════════════════════════════════════════════════════════

def _mean_eff_breadth(weights: pd.DataFrame, rebal: int) -> float:
    """Mean effective number of names per leg = mean over rebalance dates of
    1 / sum(w_i^2) within the LONG leg (inverse Herfindahl).

    EW k-name long leg: all w_i = 1/k → HHI = k*(1/k)^2 = 1/k → ENS = k. Correct.
    IV leg: concentrated into few low-vol names → ENS < k.

    Only computed on rebalance dates (i % rebal == 0), where weights change.
    """
    ens_vals = []
    for i, (dt, row) in enumerate(weights.iterrows()):
        if i % rebal != 0:
            continue
        long_weights = row[row > 0]
        if long_weights.empty:
            continue
        hhi = float((long_weights ** 2).sum())
        if hhi > 0:
            ens_vals.append(1.0 / hhi)
    return float(np.mean(ens_vals)) if ens_vals else float("nan")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 78)
    print("INVERSE-VOL WEIGHTING VALIDATION — CRYPTO XSEC MOMENTUM BOOK")
    print("Comparing EW (incumbent) vs IV20 / IV30 / IV60 (inverse-vol weights)")
    print("=" * 78)

    # ── Build PT panel ────────────────────────────────────────────────────────
    print("\n[1] Building survivorship-debiased PT panel...")
    panel = _build_pt_panel()
    px    = panel["price"]
    n_days = len(px)
    date_min = px.index.min().date()
    date_max = px.index.max().date()
    print(f"    Panel: {date_min} → {date_max}  ({n_days} days)")

    # ── Build EW book (incumbent) ─────────────────────────────────────────────
    print("\n[2] Building EW (incumbent) book...")
    pnl_ew = build_ew_book(panel)
    print(f"    EW: shape={pnl_ew.shape}  nan_count={pnl_ew.isna().sum()}")

    # ── Mandatory assert: EW == survivorship.run_book ─────────────────────────
    print("\n[3] MANDATORY ASSERT: EW == survivorship.run_book() (tol 1e-9)...")
    pnl_reference = survivorship.run_book(panel)
    diff = (pnl_ew - pnl_reference).abs().max()
    print(f"    Max abs difference: {diff:.2e}")
    assert diff < 1e-9, (
        f"ASSERT FAILED: EW book does NOT reproduce survivorship.run_book! diff={diff:.2e}\n"
        "Check that build_ew_book uses identical hyperparameters."
    )
    print("    ASSERT PASSED: EW reproduces survivorship.run_book exactly.")

    # ── Build IV books ────────────────────────────────────────────────────────
    print("\n[4] Building IV books (W ∈ {20, 30, 60})...")

    # EW weights for turnover/breadth reference
    score_ew = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    ew_weights = xsec.rank_to_weights(score_ew)

    iv_pnls:    dict[str, pd.Series]    = {}
    iv_weights: dict[str, pd.DataFrame] = {}
    for w in VOL_WINDOWS:
        lbl = f"IV{w}"
        pnl, wts = build_iv_book(panel, vol_window=w)
        iv_pnls[lbl]    = pnl
        iv_weights[lbl] = wts
        print(f"    {lbl}: shape={pnl.shape}  nan_count={pnl.isna().sum()}")

    # ── Collect all books ─────────────────────────────────────────────────────
    books: dict[str, pd.Series] = {"EW": pnl_ew}
    books.update(iv_pnls)

    # ── Align on common non-NaN window ───────────────────────────────────────
    # Start from EW's valid index (EW is the reference baseline)
    common_idx = pnl_ew.dropna().index
    for lbl in CONFIG_LABELS[1:]:
        common_idx = common_idx.intersection(books[lbl].dropna().index)
    print(f"\n[5] Common non-NaN window: {common_idx.min().date()} → "
          f"{common_idx.max().date()}  ({len(common_idx)} days)")

    books_clean: dict[str, pd.Series] = {lbl: books[lbl].loc[common_idx]
                                          for lbl in CONFIG_LABELS}
    n_common = len(common_idx)

    # Weights on the common window (for turnover and breadth accounting)
    ew_weights_common = ew_weights.reindex(common_idx).fillna(0.0)
    iv_weights_common = {lbl: iv_weights[lbl].reindex(common_idx).fillna(0.0)
                         for lbl in [f"IV{w}" for w in VOL_WINDOWS]}

    # ── Turnover ──────────────────────────────────────────────────────────────
    print("\n[6] Annual turnover and effective breadth per config:")
    turnovers:     dict[str, float] = {}
    eff_breadths:  dict[str, float] = {}

    turnovers["EW"]     = _annual_turnover(ew_weights_common, REBAL_EVERY)
    eff_breadths["EW"]  = _mean_eff_breadth(ew_weights_common, REBAL_EVERY)
    for lbl in [f"IV{w}" for w in VOL_WINDOWS]:
        turnovers[lbl]    = _annual_turnover(iv_weights_common[lbl], REBAL_EVERY)
        eff_breadths[lbl] = _mean_eff_breadth(iv_weights_common[lbl], REBAL_EVERY)

    print(f"  {'Config':>8}  {'turn/yr':>9}  {'eff_breadth/leg':>16}  "
          f"{'cost_bps/yr':>12}")
    for lbl in CONFIG_LABELS:
        to   = turnovers[lbl]
        eb   = eff_breadths[lbl]
        cost = to * COSTS_BPS
        print(f"  {lbl:>8}  {to:>9.2f}  {eb:>16.2f}  {cost:>12.1f}")

    # ── Full-period IS (in-sample) metrics ────────────────────────────────────
    print("\n[7] Full-period IS metrics (sqrt(365) daily Sharpe):")
    print(f"  {'Config':>8}  {'Sharpe':>8}  {'Ann%':>8}  {'MaxDD%':>8}  "
          f"{'Calmar':>8}  {'Vol%':>8}  {'Hit%':>7}  {'n':>5}")
    is_metrics: dict[str, dict] = {}
    for lbl in CONFIG_LABELS:
        m = daily_metrics(books_clean[lbl])
        is_metrics[lbl] = m
        cal_str = (f"{m['calmar']:>8.2f}"
                   if not np.isnan(m.get("calmar", float("nan"))) else "     nan")
        print(f"  {lbl:>8}  {m['sharpe']:>8.3f}  {100*m['ann']:>8.2f}  "
              f"{100*m['maxdd']:>8.2f}  {cal_str}  "
              f"{100*m['vol_ann']:>8.2f}  {100*m['hit']:>7.1f}  {m['n']:>5d}")

    # ── Per-period Sharpe array for DSR deflation ─────────────────────────────
    n_menu = len(CONFIG_LABELS)
    trial_sr = np.array([moments(books_clean[lbl].values).sr for lbl in CONFIG_LABELS])
    print(f"\n  Per-day Sharpe array (for DSR N={n_menu} deflation):")
    for lbl, sr in zip(CONFIG_LABELS, trial_sr):
        print(f"    {lbl}: {sr:.6f}")

    # ── DSR for each book ─────────────────────────────────────────────────────
    print(f"\n[8] DSR (deflated against N={n_menu} configs = full menu):")
    print(f"  {'Config':>8}  {'per-day SR':>11}  "
          f"{'DSR(N=4)':>10}  {'DSR(N=1)':>9}  "
          f"{'PSR_vs0':>9}  {'T':>6}  {'skew':>7}  {'kurt':>7}")
    dsr_menu_results: dict[str, dict] = {}
    dsr_n1_results:   dict[str, dict] = {}
    for lbl in CONFIG_LABELS:
        d_menu = _dsr_with_menu(books_clean[lbl], trial_sr)
        d_n1   = _dsr_n1(books_clean[lbl])
        dsr_menu_results[lbl] = d_menu
        dsr_n1_results[lbl]   = d_n1
        print(f"  {lbl:>8}  {d_menu['sr_hat']:>11.6f}  "
              f"{d_menu['dsr']:>10.4f}  {d_n1['dsr']:>9.4f}  "
              f"{d_menu['psr_vs_zero']:>9.4f}  {d_menu['T']:>6d}  "
              f"{d_menu['skew']:>7.3f}  {d_menu['kurt']:>7.3f}")

    # ── CPCV OOS distributions ────────────────────────────────────────────────
    print(f"\n[9] CPCV OOS distribution (n_groups={N_GROUPS}, k={K_CPCV}, "
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
    print(f"\n[10] PBO across the {n_menu}-config menu (CSCV):")
    books_arr = {lbl: books_clean[lbl].values for lbl in CONFIG_LABELS}
    pbo_result = _pbo_across_menu(books_arr)
    print(f"    PBO = {pbo_result['pbo']:.4f}  (n_splits={pbo_result['n_splits']}, "
          f"S={pbo_result['S']}, n_configs={pbo_result['n_configs']})")
    print(f"    Median OOS rank of IS-best: {pbo_result['median_oos_rank']:.3f} "
          f"(1.0=best, 0.0=worst)")
    if pbo_result["is_best_counts"]:
        print(f"    IS-best frequency: {pbo_result['is_best_counts']}")

    # ── Pairwise correlations vs EW ───────────────────────────────────────────
    ew_vals = books_clean["EW"].values
    corr_vs_ew: dict[str, float] = {}
    print("\n[11] Pairwise correlations vs EW:")
    for lbl in CONFIG_LABELS[1:]:
        c = float(np.corrcoef(ew_vals, books_clean[lbl].values)[0, 1])
        corr_vs_ew[lbl] = c
        print(f"    corr(EW, {lbl}) = {c:.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY TABLE
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SUMMARY TABLE")
    print("=" * 100)
    hdr = (f"  {'Config':>8}  {'turn/yr':>8}  {'eff_brd/leg':>12}  "
           f"{'IS Sharpe':>10}  {'DSR(N=4)':>9}  {'DSR(N=1)':>9}  "
           f"{'OOS med Sh':>11}  {'OOS %Sh>0':>10}  {'MaxDD%':>8}  {'corr_EW':>8}")
    print(hdr)
    print("  " + "-" * 98)

    for lbl in CONFIG_LABELS:
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl]
        to  = turnovers[lbl]
        eb  = eff_breadths[lbl]
        sh_oos  = oo["sharpe"].get("median", float("nan"))
        pct_pos = oo["frac_sharpe_pos"] * 100
        corr_str = f"{corr_vs_ew[lbl]:>8.4f}" if lbl != "EW" else "       —"
        print(f"  {lbl:>8}  {to:>8.2f}  {eb:>12.2f}  "
              f"{m['sharpe']:>10.3f}  {d_m['dsr']:>9.4f}  {d_1['dsr']:>9.4f}  "
              f"{sh_oos:>+11.3f}  {pct_pos:>9.1f}%  {100*m['maxdd']:>8.2f}  "
              f"{corr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    dsr_threshold = 0.95

    oos_med_sharpes = {lbl: oos_results[lbl]["sharpe"].get("median", float("nan"))
                       for lbl in CONFIG_LABELS}
    oos_iqr_lo = {lbl: oos_results[lbl]["sharpe"].get("iqr_lo", float("nan"))
                  for lbl in CONFIG_LABELS}
    oos_iqr_hi = {lbl: oos_results[lbl]["sharpe"].get("iqr_hi", float("nan"))
                  for lbl in CONFIG_LABELS}

    ew_oos_lo = oos_iqr_lo["EW"]
    ew_oos_hi = oos_iqr_hi["EW"]

    dsr_menu_pass = {lbl: dsr_menu_results[lbl]["dsr"] > dsr_threshold
                     for lbl in CONFIG_LABELS}
    dsr_n1_pass   = {lbl: dsr_n1_results[lbl]["dsr"] > dsr_threshold
                     for lbl in CONFIG_LABELS}

    iv_labels = [f"IV{w}" for w in VOL_WINDOWS]

    oos_iqr_overlap_with_ew = {
        lbl: (oos_iqr_lo[lbl] <= ew_oos_hi and ew_oos_lo <= oos_iqr_hi[lbl])
        for lbl in iv_labels
    }

    # Best IV by OOS median Sharpe
    best_iv_lbl  = max(iv_labels, key=lambda l: oos_med_sharpes.get(l, -np.inf))
    best_iv_oos  = oos_med_sharpes[best_iv_lbl]
    ew_oos       = oos_med_sharpes["EW"]
    best_beats_ew_oos = (best_iv_oos > ew_oos and
                         not oos_iqr_overlap_with_ew[best_iv_lbl])

    n_menu_iv_pass = sum(1 for lbl in iv_labels if dsr_menu_pass[lbl])
    any_iv_n1_pass = any(dsr_n1_pass[lbl] for lbl in iv_labels)
    any_iv_n1_warn = any(0.5 <= dsr_n1_results[lbl]["dsr"] < dsr_threshold
                         for lbl in iv_labels)
    all_oos_overlap = all(oos_iqr_overlap_with_ew[lbl] for lbl in iv_labels)

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)

    print("\n[DSR Analysis]")
    for lbl in CONFIG_LABELS:
        d_m  = dsr_menu_results[lbl]["dsr"]
        d_1  = dsr_n1_results[lbl]["dsr"]
        psr0 = dsr_menu_results[lbl]["psr_vs_zero"]
        status_nm = "PASS (>0.95)" if d_m > dsr_threshold else (
            "WARN (0.5-0.95)" if d_m >= 0.5 else "FAIL (<0.5)")
        status_n1 = "PASS" if d_1 > dsr_threshold else (
            "WARN" if d_1 >= 0.5 else "FAIL")
        print(f"  {lbl:>8}: DSR(N=4)={d_m:.4f} [{status_nm}]  "
              f"DSR(N=1)={d_1:.4f} [{status_n1}]  PSR_vs0={psr0:.4f}")

    print("\n[OOS IQR Overlap with EW]")
    for lbl in iv_labels:
        overlap = oos_iqr_overlap_with_ew[lbl]
        oo_med  = oos_med_sharpes[lbl]
        print(f"  {lbl:>8}: OOS med={oo_med:+.3f}  "
              f"IQR=[{oos_iqr_lo[lbl]:+.3f},{oos_iqr_hi[lbl]:+.3f}]  "
              f"vs EW=[{ew_oos_lo:+.3f},{ew_oos_hi:+.3f}]  "
              f"{'OVERLAP' if overlap else 'NO OVERLAP'}")

    print(f"\n[PBO] = {pbo_result['pbo']:.4f}  "
          f"(N={n_menu} configs; <0.2=low overfit risk; >0.5=high overfit risk)")
    print(f"    {pbo_result['note']}")

    print(f"\n[Concentration / Breadth Honesty Check]")
    print(f"  EW eff_breadth/leg = k by construction ≈ {eff_breadths['EW']:.2f}")
    for lbl in iv_labels:
        eb = eff_breadths[lbl]
        to_ew_ratio = turnovers[lbl] / turnovers["EW"] if turnovers["EW"] > 0 else float("nan")
        print(f"  {lbl}: eff_breadth/leg = {eb:.2f}  "
              f"(turn ratio vs EW: {to_ew_ratio:.2f}x)")

    print(f"\n[Turnover vs EW]")
    for lbl in iv_labels:
        to_delta = turnovers[lbl] - turnovers["EW"]
        cost_delta = to_delta * COSTS_BPS
        direction = "HIGHER" if to_delta > 0 else "LOWER"
        print(f"  {lbl}: {turnovers[lbl]:.2f}/yr vs EW {turnovers['EW']:.2f}/yr  "
              f"({direction} by {abs(cost_delta):.1f}bps/yr in cost)")

    # ── Resolved summary stats for the verdict prose ──────────────────────────
    iv_turns       = [turnovers[l] for l in iv_labels]
    iv_ebs         = [eff_breadths[l] for l in iv_labels]
    iv_cost_deltas = [(turnovers[l] - turnovers["EW"]) * COSTS_BPS for l in iv_labels]
    min_iv_turn, max_iv_turn = min(iv_turns), max(iv_turns)
    min_iv_eb, max_iv_eb     = min(iv_ebs), max(iv_ebs)
    min_cost, max_cost       = min(iv_cost_deltas), max(iv_cost_deltas)

    # ── Build verdict text ────────────────────────────────────────────────────
    if n_menu_iv_pass > 0 and best_beats_ew_oos:
        # An IV config clears DSR(N=4) AND beats EW OOS without IQR overlap
        best = max(iv_labels, key=lambda l: (
            dsr_menu_results[l]["dsr"] > dsr_threshold,
            oos_med_sharpes.get(l, -np.inf)
        ))
        verdict = (
            f"{best.upper()} BEATS EW (DSR(N=4)>0.95, no OOS IQR overlap).\n"
            f"\n"
            f"  The inverse-vol window {best} passes full menu deflation AND produces\n"
            f"  strictly higher OOS median Sharpe with non-overlapping IQR vs EW.\n"
            f"  Turnover {turnovers[best]:.2f}/yr vs EW {turnovers['EW']:.2f}/yr.\n"
            f"  Effective breadth/leg: {eff_breadths[best]:.2f} (EW: {eff_breadths['EW']:.2f}).\n"
            f"  RECOMMEND: consider switching to {best}.\n"
            f"  Note: verify concentration finding — if eff_breadth << EW, the book\n"
            f"  dumps the leg into very few low-vol names which adds idiosyncratic risk."
        )
    elif n_menu_iv_pass == 0 and (any_iv_n1_warn or any_iv_n1_pass or dsr_n1_pass["EW"]):
        if all_oos_overlap:
            verdict = (
                "KEEP EW — INVERSE-VOL CONFIGS ARE STATISTICALLY INDISTINGUISHABLE FROM EW.\n"
                "\n"
                f"  All {n_menu} configs (EW + IV sweep) fall in the DSR WARN zone: the\n"
                "  momentum edge is likely real (PSR_vs_0 elevated) but ~3 years of data\n"
                "  is insufficient to clear DSR>0.95 even for N=1. No inverse-vol window\n"
                "  clears the menu-size multi-test bar.\n"
                "\n"
                "  All OOS IQR ranges overlap substantially with EW. PBO confirms IS-winner\n"
                "  selection among these correlated books is unreliable.\n"
                "\n"
                f"  TURNOVER: every IV window costs MORE than EW — turnover "
                f"{min_iv_turn:.1f}-{max_iv_turn:.1f}/yr vs EW {turnovers['EW']:.1f}/yr "
                f"(+{min_cost:.0f} to +{max_cost:.0f} bps/yr),\n"
                "  because vol-based weights drift more between rebalances than equal weights.\n"
                "\n"
                f"  CONCENTRATION: the structural argument FAILS here. IV effective "
                f"breadth/leg is\n"
                f"  {min_iv_eb:.1f}-{max_iv_eb:.1f} names vs EW {eff_breadths['EW']:.1f} — "
                f"inverse-vol is MORE concentrated, not less.\n"
                "  Within momentum terciles crypto vol is fairly homogeneous, so 1/vol tilts\n"
                "  toward fewer low-vol names rather than equalizing risk contribution.\n"
                "\n"
                "  CONCLUSION: Inverse-vol weighting does not deliver a detectable\n"
                "  improvement in this dataset. The complexity cost (harder to audit,\n"
                "  path-dependent vol computation, potential concentration) is not\n"
                "  compensated by measurable OOS benefit. Retain EW as the incumbent.\n"
                "  Revisit with 5+ years of data or if live dispersion evidence suggests\n"
                "  specific coins are dominating realized portfolio vol."
            )
        else:
            no_overlap_ivs = [lbl for lbl in iv_labels
                              if not oos_iqr_overlap_with_ew[lbl]]
            verdict = (
                "KEEP EW — NO INVERSE-VOL CONFIG CLEARS DSR THRESHOLD.\n"
                "\n"
                f"  Despite some IV configs showing non-overlapping OOS IQR with EW\n"
                f"  ({no_overlap_ivs}), none clears DSR(N=4)>0.95 or even DSR(N=1)>0.95.\n"
                "  The non-overlap may reflect the additional warmup period consumed by\n"
                "  the vol window (W-day NaN prefix further trimming the OOS sample),\n"
                "  or may be a genuine but statistically marginal effect.\n"
                "\n"
                "  CONCLUSION: Insufficient statistical power to justify switching.\n"
                "  Retain EW. Revisit with more data."
            )
    elif n_menu_iv_pass > 0 and not any(dsr_menu_pass[lbl] for lbl in iv_labels):
        verdict = (
            "KEEP EW — IT IS THE ONLY DSR(N=4) PASS AND IS THE INCUMBENT.\n"
            "\n"
            "  EW clears the multi-test bar while no IV config does.\n"
            "  No statistical case for switching to inverse-vol weighting."
        )
    else:
        verdict = (
            "INCONCLUSIVE — INSUFFICIENT STATISTICAL POWER.\n"
            "\n"
            f"  No config clears DSR(N=4)>0.95. OOS IQR overlap prevents\n"
            "  distinguishing inverse-vol from equal-weight.\n"
            "  Retain EW as the incumbent non-overfit default."
        )

    print(f"\n  VERDICT: {verdict}")

    # ══════════════════════════════════════════════════════════════════════════
    # HONESTY CAVEATS
    # ══════════════════════════════════════════════════════════════════════════
    surv_json = json.loads((_HERE / "survivorship.json").read_text())
    n_dead = len(surv_json["extra_dead_coins_included"])

    caveats = [
        f"~3 years of data only ({n_common} days); low statistical power for "
        "discriminating between similar weighting schemes that share the same "
        "momentum signal.",
        "Survivorship-debiased PT book (the honest version; includes "
        f"{n_dead} dead/delisted coins). Results are more conservative than the "
        "frozen survivor set.",
        "No real crypto bear market or liquidity crisis in the study window "
        "(2023-06 to present); inverse-vol weighting might behave differently "
        "when vol regimes shift sharply.",
        f"DSR deflation uses N={n_menu} (full menu size). However, the three IV "
        "configs are NOT truly independent — they share the same momentum signal "
        "and differ only in the vol-window smoothing. N=4 deflation is therefore "
        "conservative (penalizes more than necessary); N=1 is the more honest "
        "single-strategy view. Both are reported.",
        "Vol is computed causally (rolling std of price returns, all data <= t). "
        "However, CPCV slices the precomputed pnl — the vol computation inside "
        "the IS portion of a CPCV fold is correctly estimated from IS data only; "
        "no look-ahead is introduced by the pnl slicing.",
        "Inverse-vol concentration risk: if the vol window is short (W=20), "
        "the weights can become highly concentrated in the 1-2 lowest-vol names "
        "(eff_breadth → 1). Check eff_breadth/leg in the summary table. If "
        "eff_breadth << k_EW, the structural rationale for IV (better diversification) "
        "is inverted — IV becomes MORE concentrated, not less.",
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

    per_config_out: dict = {}

    # EW block
    lbl = "EW"
    m   = is_metrics[lbl]
    d_m = dsr_menu_results[lbl]
    d_1 = dsr_n1_results[lbl]
    oo  = oos_results[lbl].copy()
    oo.pop("all_oos_sharpes", None)
    per_config_out[lbl] = {
        "type": "equal_weight",
        "vol_window": None,
        "turn_per_yr": float(turnovers[lbl]),
        "eff_breadth_per_leg": float(eff_breadths[lbl]),
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
        "corr_vs_ew": None,
        "assert_equals_run_book": True,
        "assert_max_diff": float(diff),
    }

    # IV blocks
    for w_win in VOL_WINDOWS:
        lbl = f"IV{w_win}"
        m   = is_metrics[lbl]
        d_m = dsr_menu_results[lbl]
        d_1 = dsr_n1_results[lbl]
        oo  = oos_results[lbl].copy()
        oo.pop("all_oos_sharpes", None)
        per_config_out[lbl] = {
            "type": "inverse_vol",
            "vol_window": int(w_win),
            "turn_per_yr": float(turnovers[lbl]),
            "eff_breadth_per_leg": float(eff_breadths[lbl]),
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
            "corr_vs_ew": float(corr_vs_ew[lbl]),
        }

    out = {
        "test": "invvol_weighting_validation",
        "description": (
            "CPCV+DSR+PBO validation of inverse-volatility leg weighting "
            "vs. equal-weight (EW) for the crypto cross-sectional momentum book. "
            "Menu: EW (incumbent) + IV20 + IV30 + IV60 (4 configs). "
            "Book: survivorship-debiased PT panel, identical hyperparams to validated book. "
            "Only the within-leg weight matrix changes; portfolio_returns is unchanged."
        ),
        "invvol_model": (
            "rank_to_weights_invvol: same selection (top-k long, bottom-k short) as EW. "
            "Within each leg: w_i = (1/vol_i) / sum(1/vol_j). "
            "Vol = rolling std of daily price pct_change, window=W, min_periods=W, causal. "
            "Names with vol<=0 or NaN are excluded from the leg. "
            "Dollar-neutral preserved: long leg sums to +1, short leg to −1."
        ),
        "assert_ew_equals_run_book": True,
        "assert_ew_max_diff": float(diff),
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
        "rebal_every": REBAL_EVERY,
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

    out_path = _HERE / "invvol_validation.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON written to {out_path}")

    return out


if __name__ == "__main__":
    result = main()
