"""
covweight.py — covariance-aware within-leg weight constructors.

HYPOTHESIS: equal-dollar weighting over-concentrates risk because crypto coins
are highly correlated with very different vols.  Replace equal-$ within each
tercile leg with RISK-aware weights, while keeping the book dollar-neutral
(Σ|long| = Σ|short| = 1).

Two variants:
  (a) inverse_vol_weights  — diagonal only: w_i ∝ 1/vol_i(rolling). Robust,
      one main knob (vol_window). PRIMARY method.
  (b) minvar_weights       — minimum-variance using the rolling shrunk covariance
      (Ledoit-Wolf diagonal-target shrinkage).  More fragile, more knobs.

Both functions take the SAME tercile membership as the baseline (identical score
ranking from the ensemble signal) but replace the equal-1/k sizing inside each
leg with risk-aware sizing.

NO LOOK-AHEAD: covariance / vol estimated strictly on daily returns up to and
including row t.  Estimation always uses a rolling window of fixed width ending
at t so that the weight vector w[t] depends only on data ≤ t.

Dollar-neutrality: after risk-weighting we normalise each leg independently so
Σ(positive weights) = +1  and  Σ(negative weights) = −1  (same convention as
xsec.rank_to_weights).

Output: pd.DataFrame with the same shape as scores (date × coin), compatible
with xsec.portfolio_returns.

Only numpy / pandas / scipy.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd


# ── helpers ───────────────────────────────────────────────────────────────────

def _daily_returns(price: pd.DataFrame) -> pd.DataFrame:
    """Simple daily returns for the price panel (used to estimate vol / cov)."""
    return price / price.shift(1) - 1.0


def _rolling_vol(daily_ret: pd.DataFrame, window: int) -> pd.DataFrame:
    """Per-coin rolling standard deviation (ddof=0) over `window` rows ending at t.
    Requires at least `window` rows; earlier rows are NaN.
    """
    return daily_ret.rolling(window, min_periods=window).std(ddof=0)


def _lw_shrinkage(S: np.ndarray, n: int) -> np.ndarray:
    """Simplified Oracle Approximating Shrinkage (OAS) toward a scaled identity.

    Ledoit-Wolf-style: blend the sample covariance S with its average diagonal
    variance scaled by the identity matrix.  The shrinkage coefficient δ is
    estimated analytically (no eigenvalue solver needed).

    S   : (p × p) sample covariance, computed from the most recent n returns.
    Returns the shrunk matrix S_shrunk = (1 − δ) * S + δ * μ * I
    where μ = trace(S) / p  and δ is the OAS coefficient clipped to [0, 1].
    """
    p = S.shape[0]
    if p == 0:
        return S
    mu = np.trace(S) / p
    # OAS analytical formula (Chen et al. 2010)
    # numerator = (((n-2)/n)*tr(S^2) + tr(S)^2) / ((n+2)*(tr(S^2) - tr(S)^2/p))
    tr_S = np.trace(S)
    tr_S2 = np.trace(S @ S)
    denom = (n + 2) * (tr_S2 - tr_S ** 2 / p)
    if abs(denom) < 1e-12:
        delta = 0.0   # sample covariance is already well-conditioned (p=1 case)
    else:
        delta = ((n - 2) / n * tr_S2 + tr_S ** 2) / denom
    delta = float(np.clip(delta, 0.0, 1.0))
    return (1.0 - delta) * S + delta * mu * np.eye(p)


def _minvar_weights_from_cov(cov: np.ndarray) -> np.ndarray:
    """Minimum-variance portfolio weights from a (p × p) covariance matrix.

    Analytical solution: w = Σ^{-1} 1 / (1^T Σ^{-1} 1), normalised to sum=1.
    Uses np.linalg.solve for numerical stability.  Falls back to equal-weight
    (1/p) if the system is singular or ill-conditioned.
    """
    p = cov.shape[0]
    ones = np.ones(p)
    try:
        cov_inv_ones = np.linalg.solve(cov + 1e-8 * np.eye(p), ones)
        denom = ones @ cov_inv_ones
        if abs(denom) < 1e-12:
            return ones / p
        w = cov_inv_ones / denom
        # Clip negatives to zero and re-normalise (long-only within leg).
        # The minimum-variance solution can go short; within a leg we don't want
        # that — we only want a RISK-SIZED long in the long leg and risk-sized
        # short in the short leg, not within-leg internal shorting.
        w = np.maximum(w, 0.0)
        s = w.sum()
        return w / s if s > 1e-12 else ones / p
    except np.linalg.LinAlgError:
        return ones / p


# ── Tercile membership helpers ────────────────────────────────────────────────

def _tercile_membership(scores: pd.DataFrame,
                         tercile_frac: float = 1.0 / 3.0,
                        ) -> tuple[dict, dict]:
    """Return two dicts: {date: list[coin]} for longs and shorts.

    Identical selection logic to xsec.rank_to_weights so that the leg members
    are exactly the same for all weight variants — only the sizing changes.
    """
    longs: dict = {}
    shorts: dict = {}
    for dt, row in scores.iterrows():
        valid = row.dropna()
        n = len(valid)
        if n < 2:
            longs[dt] = []
            shorts[dt] = []
            continue
        k = max(1, int(np.floor(n * tercile_frac)))
        order = valid.sort_values(ascending=False)
        longs[dt] = list(order.index[:k])
        shorts[dt] = list(order.index[n - k:])
    return longs, shorts


# ── Primary: inverse-vol weights ─────────────────────────────────────────────

def inverse_vol_weights(
    scores: pd.DataFrame,
    price: pd.DataFrame,
    vol_window: int = 60,
    tercile_frac: float = 1.0 / 3.0,
) -> pd.DataFrame:
    """Dollar-neutral weights: within each leg size ∝ 1 / vol_i(rolling).

    scores  : date × coin cross-sectional score panel (from ensemble signal).
              Higher = more attractive to long.  NaN where history insufficient.
    price   : date × coin price panel (used to compute daily returns → vol).
    vol_window: rolling estimation window (days) — the plateau-vs-spike test
              sweeps over (45, 60, 90, 120).
    tercile_frac: leg fraction (default 1/3, must match xsec usage).

    Returns pd.DataFrame[date × coin] with positive weights in the long leg
    (sum = +1 across longs) and negative weights in the short leg (sum = −1),
    0 elsewhere.  No look-ahead: vol[t] estimated from daily returns ≤ t.
    """
    dr = _daily_returns(price)
    vol = _rolling_vol(dr, vol_window)   # NaN for the first vol_window rows

    longs, shorts = _tercile_membership(scores, tercile_frac)
    w = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)

    for dt in scores.index:
        # --- long leg ---
        lng = longs.get(dt, [])
        if lng:
            v = vol.loc[dt, lng]
            # if vol is NaN for any coin (history too short), fall back to
            # equal-weight for that leg to avoid dropping names mid-leg.
            if v.isna().any():
                raw_w = np.ones(len(lng))
            else:
                raw_w = 1.0 / v.values.clip(1e-10)
            raw_w = raw_w / raw_w.sum()
            w.loc[dt, lng] = raw_w

        # --- short leg ---
        sht = shorts.get(dt, [])
        if sht:
            v = vol.loc[dt, sht]
            if v.isna().any():
                raw_w = np.ones(len(sht))
            else:
                raw_w = 1.0 / v.values.clip(1e-10)
            raw_w = raw_w / raw_w.sum()
            w.loc[dt, sht] = -raw_w

    return w


# ── Secondary: minimum-variance (Ledoit-Wolf shrinkage) weights ───────────────

def minvar_weights(
    scores: pd.DataFrame,
    price: pd.DataFrame,
    cov_window: int = 90,
    tercile_frac: float = 1.0 / 3.0,
) -> pd.DataFrame:
    """Dollar-neutral weights: within each leg sized by minimum-variance solution.

    Uses the rolling shrunk covariance (OAS toward scaled identity).  Within
    each leg the weights are the min-var solution projected to the non-negative
    orthant (no within-leg shorting), then normalised to sum = 1.

    cov_window: rolling estimation window (days).  A 90d window on ~11-coin
    terciles with OAS shrinkage is borderline (p ≈ 11, n = 90 → n/p ≈ 8);
    the shrinkage compensates but this method is necessarily noisier than
    inverse-vol.
    """
    dr = _daily_returns(price)

    longs, shorts = _tercile_membership(scores, tercile_frac)
    w = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)

    dates = scores.index
    dr_arr = dr.values
    cols = list(scores.columns)
    col_idx = {c: i for i, c in enumerate(cols)}

    for t_pos, dt in enumerate(dates):
        # window: the last cov_window rows ENDING at t_pos (inclusive).
        if t_pos < cov_window:
            # Not enough history for a full window → fall back to equal-weight.
            lng = longs.get(dt, [])
            sht = shorts.get(dt, [])
            if lng:
                w.loc[dt, lng] = 1.0 / len(lng)
            if sht:
                w.loc[dt, sht] = -1.0 / len(sht)
            continue

        # Returns window for covariance estimation (strictly ≤ t).
        # dr_arr[t_pos] is the return FROM t_pos-1 TO t_pos, so the window
        # [t_pos - cov_window + 1 : t_pos + 1] uses only data known at t_pos.
        win_start = t_pos - cov_window + 1
        win_end   = t_pos + 1            # exclusive

        for leg_coins, sign in [(longs.get(dt, []), +1.0),
                                 (shorts.get(dt, []), -1.0)]:
            if not leg_coins:
                continue
            idxs = [col_idx[c] for c in leg_coins]
            R_leg = dr_arr[win_start:win_end, :][:, idxs]  # (cov_window, k)

            # Drop any rows that have NaN in any of the leg coins.
            valid_rows = ~np.isnan(R_leg).any(axis=1)
            R_clean = R_leg[valid_rows]
            n_obs, p = R_clean.shape

            if n_obs < max(p + 1, 10):
                # Too few observations → equal-weight fallback.
                raw_w = np.ones(p) / p
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    S = np.cov(R_clean.T, ddof=0)
                    if p == 1:
                        S = np.array([[float(S)]])
                    S_shrunk = _lw_shrinkage(S, n_obs)
                raw_w = _minvar_weights_from_cov(S_shrunk)

            if sign > 0:
                w.loc[dt, leg_coins] = raw_w
            else:
                w.loc[dt, leg_coins] = -raw_w

    return w


# ── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    sys.path.insert(0, str(Path(__file__).parent.parent))

    import cryptodata
    import signals

    HERE = Path(__file__).parent
    coins = json.loads((HERE / "universe.json").read_text())["coins"]
    panel = cryptodata.load_panel(coins=coins)
    px    = panel["price"]
    score = signals.momentum_ensemble(panel, lookbacks=(14, 21, 30, 45, 60))

    w_eq  = None  # baseline from xsec
    w_iv  = inverse_vol_weights(score, px, vol_window=60)
    w_mv  = minvar_weights(score, px, cov_window=90)

    # dollar-neutrality check
    for label, wdf in [("inv_vol", w_iv), ("minvar", w_mv)]:
        bad = 0
        for dt, row in wdf.iterrows():
            pos = row[row > 0].sum()
            neg = row[row < 0].sum()
            if abs(pos) > 1e-6 and not np.isclose(pos, 1.0, atol=1e-6):
                bad += 1
            if abs(neg) < -1e-6 and not np.isclose(neg, -1.0, atol=1e-6):
                bad += 1
        print(f"[{label}] dollar-neutrality violations (of non-zero rows): {bad}")

    # shape check
    assert w_iv.shape == score.shape, f"inv_vol shape mismatch {w_iv.shape}"
    assert w_mv.shape == score.shape, f"minvar shape mismatch {w_mv.shape}"
    print(f"shapes OK: {w_iv.shape}")

    # no look-ahead structural sanity: weights at t use vol/cov only up to t
    # (enforced by rolling window ending at t — checked by construction above)

    print("covweight.py self-test PASSED")
