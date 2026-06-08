"""
generators/parametric.py — Parametric synthetic dfs generator (T3).

Model overview
--------------
Given per-coin calibration JSONs (from T2 calibrate_stats) + a cross-funding
correlation matrix, this generator produces a dict[coin → DataFrame] that the
engine_adapter.run_on_dfs() can consume directly.  Each DataFrame has columns
['close', 'fundingRate'] and an hourly DatetimeIndex.

1. REGIME (market-wide Markov 2-state: hot / cold)
   ─────────────────────────────────────────────────
   A single market-wide binary regime path is shared across all coins (funding
   regimes are macro-driven, so joint hot/cold is the right model for correlated
   crashes).  Transition probability per hour = mean of regime_transition_freq
   across all coins.  Start state = cold (conservative).

2. FUNDING per coin — log-level AR(1) with bounded regime shifts
   ────────────────────────────────────────────────────────────────
   The real funding rate distribution is RIGHT-SKEWED — fewer negative hours
   than a Gaussian AR(1) would predict for the same mean and std.  To resolve
   this, the model operates in LOG space:

     g_t = log(f_t + offset)

   where offset > 0 is calibrated per-coin.  g_t follows an AR(1):

     g_t = mu_g_regime(t) + phi_g * (g_{t-1} − mu_g_regime(t)) + eps_t
     eps_t ~ N(0, sigma_innov_g)

   Back-transform:  f_t = exp(g_t) − offset

   The per-coin parameters (offset, mu_g, sigma_g) are calibrated so that the
   OVERALL (unconditional) distribution of f satisfies:
     E[f]    = fund_mean_h              (funding mean reproduced)
     Std[f]  = fund_std_h               (funding std reproduced)
     P(f<0)  = negative_hours_share     (neg-share reproduced EXACTLY)
   See _calibrate_log_ar1() for the 3-equation / 3-unknown solution.

   Regime mean in g-space (hot/cold):
     delta_g = log(mu_regime + offset) - sigma_g^2/2 - mu_g
     (exact expected value relationship: E[exp(g)] = mu_regime + offset)
   BUT for stability (avoiding exploding between-regime variance in g-space),
   the delta is CLIPPED to [−REGIME_DELTA_CLIP * sigma_g, REGIME_DELTA_CLIP * sigma_g].
   This matters most for SOL whose cold regime mean is slightly negative, causing a
   very large g-space cold mean; the clip keeps the model well-conditioned.

   sigma_innov_g is computed from the WITHIN-REGIME variance:
     within_var_g = max(sigma_g^2 - between_clipped_var_g, MIN_WITHIN_VAR_FRAC^2 * sigma_g^2)
     sigma_innov_g = sqrt(within_var_g) * sqrt(1 − phi^2)
   where between_clipped_var_g uses the CLIPPED regime deltas.
   This ensures the within-regime AR(1) stationary std is consistent with the
   overall sigma_g target.

   NEGATIVE RATES arise naturally whenever g_t < log(offset), i.e., exp(g_t) < offset.
   No clipping is applied to the back-transformed f_t.

   Cross-coin correlation: the Gaussian innovations eps_t are correlated between
   coins via Cholesky decomposition of the cross-funding correlation matrix
   (_cross_funding_corr.json).

3. PRICE per coin — regime-vol GBM + Poisson jump-diffusion
   ──────────────────────────────────────────────────────────
   log_return_t = mu_h + sigma_t * z_t + jump_t
   where:
     mu_h       = log_return_mean_h
     sigma_t    = cold_sigma (cold regime) or hot_sigma (hot regime)
     z_t        ~ N(0, 1)
     jump_t     = J_t * N(0, jump_sigma),  J_t ~ Bernoulli(jump_freq)

   Regime vol (hot_sigma / cold_sigma) for BTC, ETH, SOL:
     Computed by splitting each coin's historical log-returns into hot/cold
     periods using the same 720h rolling-mean regime rule as T2, then taking
     the ratio std(hot_returns) / std(cold_returns).

   Regime vol for HYPE/PURR (per PLAN.md user decision):
     HYPE/PURR have only ~208 days of price history (2025-11 onward), which is
     cold-only.  Hot vol is BORROWED from majors:
       hot_sigma(HYPE/PURR) = cold_sigma(HYPE/PURR) × mean(hot/cold ratio for BTC, ETH, SOL)
     cold_sigma(HYPE/PURR) = log_return_std_h.

   jump_sigma = JUMP_SIGMA_K * cold_sigma  (k=3 → excess kurtosis > 1 on aggregate paths)
   Price = 100 * cumprod(exp(log_return_t))  starting from t=0.

Round-trip gate
---------------
compute_round_trip_stats() / print_round_trip_table() in this module.
Tests in tests/test_parametric.py verify:
  - negative_hours_share : |diff| <= 0.05  (KEY)
  - funding_mean_annual_pct : rel <= 30%  OR  |diff| <= 3pp
  - funding_ar1_phi : |diff| <= 0.12
  - funding_std_h : rel <= 25%
  - log_return_std_h : rel <= 20%
  - excess_kurtosis (synth) > 1
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import optimize, stats as scipy_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOURS_PER_YEAR = 8760
REGIME_WINDOW_H = 720          # must match calibrate_stats.REGIME_WINDOW_H
JUMP_SIGMA_K = 7.0             # jump magnitude = k * log_return_std_h (k=7 → excess_kurt > 1 even for low jump_freq)
REGIME_DELTA_CLIP = 0.40       # max |delta_g| as fraction of sigma_g
MIN_WITHIN_VAR_FRAC = 0.15    # floor: within-regime sigma >= 15% of sigma_g

# Majors used to compute empirical hot/cold vol ratio
_MAJOR_COINS = frozenset({"BTC", "ETH", "SOL"})
# Coins whose price history is too short → borrow hot/cold vol ratio from majors
_SHORT_HISTORY_COINS = frozenset({"HYPE", "PURR"})


# ---------------------------------------------------------------------------
# Log-level AR(1) calibration
# ---------------------------------------------------------------------------

def _phi_g_from_phi_f(phi_f: float, sigma_g: float) -> float:
    """Compute the g-space AR(1) coefficient that yields a target f-space AR(1) coeff.

    For f = exp(g) - offset where g ~ AR(1)(phi_g), the autocorrelation of f at
    lag 1 is (analytically, using lognormal properties):

        phi_f = (exp(sigma_g^2 * phi_g) - 1) / (exp(sigma_g^2) - 1)

    Solving for phi_g:

        phi_g = log(phi_f * (exp(sigma_g^2) - 1) + 1) / sigma_g^2

    This ensures that the AR(1) coefficient *measured on the f-space series*
    matches the calibrated funding_ar1_phi from the real data.

    Returns:
        phi_g clipped to [0, 0.9999].
    """
    rhs = phi_f * (float(np.exp(sigma_g ** 2)) - 1.0) + 1.0
    if rhs <= 0:
        return 0.0
    return float(np.clip(np.log(rhs) / (sigma_g ** 2), 0.0, 0.9999))


def _calibrate_log_ar1(
    fund_mean_h: float,
    fund_std_h: float,
    neg_share: float,
    phi_f: float,
) -> tuple[float, float, float, float, float]:
    """Calibrate log-level AR(1) parameters to reproduce mean, std, and neg share.

    The model:  g_t = log(f_t + offset),  g_t ~ AR(1)(phi_g).
    Back-transform: f_t = exp(g_t) - offset.

    When g ~ N(mu_g, sigma_g) (the stationary distribution in log-space):
      E[f]    = exp(mu_g + sigma_g^2/2) - offset  = fund_mean_h
      Var[f]  = (exp(sigma_g^2) - 1) * exp(2*mu_g + sigma_g^2)  = fund_std_h^2
      P(f<0)  = Phi((log(offset) - mu_g) / sigma_g)              = neg_share

    These three equations uniquely determine (offset, mu_g, sigma_g).
    The equation is solved numerically via Brent's method on log(offset).

    The g-space AR(1) coefficient phi_g is computed from phi_f via the exact
    lognormal formula: phi_g = log(phi_f*(exp(sg^2)-1)+1)/sg^2.
    This ensures that the f-space lag-1 autocorrelation equals the calibrated phi_f.

    Arguments:
        phi_f:  target AR(1) coefficient in f-space (= funding_ar1_phi from JSON)

    Returns:
        (offset, mu_g, sigma_g, phi_g, sigma_innov_g_base)
        where sigma_innov_g_base = sigma_g_within * sqrt(1 − phi_g^2),
        sigma_g_within is determined after applying regime clipping.
    """
    if neg_share <= 0:
        neg_share = 1e-6
    if neg_share >= 1:
        neg_share = 1 - 1e-6

    z_neg = float(scipy_stats.norm.ppf(neg_share))

    def _eq(log_offset: float) -> float:
        offset = np.exp(log_offset)
        total = fund_mean_h + offset
        if total <= 1e-30:
            return -1e10
        cv = fund_std_h / total
        sigma_g = float(np.sqrt(np.log(1.0 + cv * cv)))
        mu_g = float(np.log(total) - 0.5 * sigma_g * sigma_g)
        return log_offset - (mu_g + sigma_g * z_neg)

    # Search range: offset from ~e^{-30} to ~e^{-5}
    try:
        log_off = optimize.brentq(_eq, -30.0, -5.0, xtol=1e-12, maxiter=300)
    except ValueError:
        log_off = float(np.log(max(neg_share * fund_std_h, 1e-15)))

    offset = float(np.exp(log_off))
    total = fund_mean_h + offset
    cv = fund_std_h / total
    sigma_g = float(np.sqrt(np.log(1.0 + cv * cv)))
    mu_g = float(np.log(total) - 0.5 * sigma_g * sigma_g)

    # Compute phi_g from phi_f using the lognormal formula
    phi_g = _phi_g_from_phi_f(phi_f, sigma_g)

    # sigma_innov_g for the OVERALL process (before regime correction)
    sigma_innov_g_base = sigma_g * float(np.sqrt(max(1.0 - phi_g * phi_g, 1e-10)))

    return offset, mu_g, sigma_g, phi_g, sigma_innov_g_base


def _compute_regime_deltas(
    mu_g: float,
    sigma_g: float,
    offset: float,
    mu_hot: float,
    mu_cold: float,
) -> tuple[float, float, float]:
    """Compute and clip regime deltas in g-space.

    For each regime, the exact g-space target mean delta is:
        delta_exact = log(mu_regime + offset) - sigma_g^2/2 - mu_g

    This sets E[exp(g_hot) - offset] = mu_hot (in stationarity within regime).

    For stability, delta is CLIPPED to [−clip_max, +clip_max]:
        clip_max = REGIME_DELTA_CLIP * sigma_g

    This avoids the regime means in g-space being so far apart that the
    between-regime variance alone exceeds sigma_g^2 (which would require imaginary
    within-regime sigma).  This primarily matters for SOL, whose cold funding
    mean is slightly negative.

    The within-regime sigma is derived as:
        within_var_g = max(sigma_g^2 − between_clipped_var_g, (MIN_WITHIN_VAR_FRAC * sigma_g)^2)
        within_sigma_g = sqrt(within_var_g)

    Returns:
        (delta_hot_clipped, delta_cold_clipped, within_sigma_g)
        Caller computes sigma_innov_g = within_sigma_g * sqrt(1 - phi_g^2).
    """
    clip_max = REGIME_DELTA_CLIP * sigma_g

    def _safe_delta(mu_regime: float) -> float:
        val = mu_regime + offset
        if val <= 0:
            val = offset * 1e-3
        return float(np.log(val) - 0.5 * sigma_g * sigma_g - mu_g)

    delta_hot = float(np.clip(_safe_delta(mu_hot), -clip_max, clip_max))
    delta_cold = float(np.clip(_safe_delta(mu_cold), -clip_max, clip_max))

    between_var_g = 0.5 * delta_hot ** 2 + 0.5 * delta_cold ** 2
    min_within = (MIN_WITHIN_VAR_FRAC * sigma_g) ** 2
    within_var_g = max(sigma_g ** 2 - between_var_g, min_within)

    return delta_hot, delta_cold, float(np.sqrt(within_var_g))


# ---------------------------------------------------------------------------
# Price regime vol helpers
# ---------------------------------------------------------------------------

def _compute_hot_cold_vol_ratio(calib_dir: Path, coin: str) -> float | None:
    """Compute hot/cold price-vol ratio for a major coin from real history.

    Splits the coin's historical log-returns into hot/cold using the same 720h
    rolling-mean funding-regime rule as calibrate_stats._regime_stats:
      hot  if rolling_720h_mean(funding) > median(rolling_mean)
      cold otherwise.

    Returns std(hot_returns) / std(cold_returns), or None if data unavailable
    or insufficient (< 200 aligned observations).

    Data directory is inferred as calib_dir.parents[2] / 'data', i.e.
    research/data/ when calib_dir = research/two_phase_margin/monte_carlo/calibration/.
    """
    data_dir = calib_dir.parents[2] / "data"

    fund_path = data_dir / f"{coin}.csv"
    if not fund_path.exists():
        return None
    fund_df = pd.read_csv(fund_path, usecols=["time", "fundingRate"])
    fund_df["time"] = pd.to_datetime(fund_df["time"], format="mixed", utc=True)
    fund_df = (
        fund_df.sort_values("time")
        .drop_duplicates("time", keep="last")
        .set_index("time")
    )
    funding = fund_df["fundingRate"].resample("1h").mean().dropna()

    price_path = data_dir / f"{coin}_1h.csv"
    if not price_path.exists():
        return None
    price_df = pd.read_csv(price_path, usecols=lambda c: c in {"time", "close"})
    price_df["time"] = pd.to_datetime(price_df["time"], utc=True)
    price_df = (
        price_df.sort_values("time")
        .drop_duplicates("time", keep="last")
        .set_index("time")
    )
    price = price_df["close"].resample("1h").last().dropna()
    log_ret = np.log(price / price.shift(1)).dropna()

    rolling = funding.rolling(REGIME_WINDOW_H, min_periods=24).mean()
    threshold = float(rolling.median())
    hot_mask_s = (rolling > threshold).astype(float)

    aligned = pd.DataFrame({"log_return": log_ret, "hot": hot_mask_s}).dropna()
    if len(aligned) < 200:
        return None

    hot_bool = aligned["hot"] > 0.5
    hot_rets = aligned.loc[hot_bool, "log_return"]
    cold_rets = aligned.loc[~hot_bool, "log_return"]

    if len(hot_rets) < 20 or len(cold_rets) < 20:
        return None

    cold_std = float(cold_rets.std(ddof=1))
    if cold_std <= 0:
        return None
    return float(hot_rets.std(ddof=1)) / cold_std


def _build_vol_ratios(calib_dir: Path, coins: list[str]) -> dict[str, float]:
    """Return hot/cold price-vol ratio for every coin.

    For BTC/ETH/SOL: computed from real data (regime-split log-returns).
    For HYPE/PURR:   mean ratio of available majors (PLAN.md user decision).
    Default fallback: 1.2 if data unavailable.
    """
    major_ratios: list[float] = []
    for c in coins:
        if c in _MAJOR_COINS:
            r = _compute_hot_cold_vol_ratio(calib_dir, c)
            if r is not None:
                major_ratios.append(r)

    mean_major_ratio = float(np.mean(major_ratios)) if major_ratios else 1.2

    result: dict[str, float] = {}
    for c in coins:
        if c in _SHORT_HISTORY_COINS:
            result[c] = mean_major_ratio
        elif c in _MAJOR_COINS:
            r = _compute_hot_cold_vol_ratio(calib_dir, c)
            result[c] = r if r is not None else mean_major_ratio
        else:
            result[c] = mean_major_ratio

    return result


# ---------------------------------------------------------------------------
# Cholesky of cross-correlation
# ---------------------------------------------------------------------------

def _cholesky_from_cross(cross: dict, coins: list[str]) -> np.ndarray:
    """Return Cholesky factor L such that L @ L.T ≈ cross-correlation submatrix.

    Coins may be a subset of cross['coins'] and in different order.
    Adds a small ridge (1e-6 * I) for numerical stability.
    """
    cross_coins = cross["coins"]
    matrix = np.array(cross["matrix"], dtype=float)
    idx = [cross_coins.index(c) for c in coins]
    sub = matrix[np.ix_(idx, idx)]
    sub += np.eye(len(coins)) * 1e-6
    return np.linalg.cholesky(sub)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_calib_dir(calib_dir: Path) -> tuple[dict[str, dict], dict]:
    """Load all per-coin calibration JSONs and _cross_funding_corr.json."""
    per_coin: dict[str, dict] = {}
    for p in sorted(calib_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue
        coin = p.stem
        with open(p) as f:
            per_coin[coin] = json.load(f)

    cross_path = calib_dir / "_cross_funding_corr.json"
    with open(cross_path) as f:
        cross = json.load(f)

    return per_coin, cross


# ---------------------------------------------------------------------------
# Regime simulation
# ---------------------------------------------------------------------------

def _simulate_regime(
    rng: np.random.Generator,
    horizon_h: int,
    p_transition: float,
    start_hot: bool = False,
) -> np.ndarray:
    """Simulate a 2-state Markov chain regime path (0=cold, 1=hot).

    p_transition: per-hour probability of switching state.
    Returns int8 array of shape (horizon_h,).
    """
    path = np.empty(horizon_h, dtype=np.int8)
    state = int(start_hot)
    switch_draws = rng.random(horizon_h)
    for t in range(horizon_h):
        if switch_draws[t] < p_transition:
            state = 1 - state
        path[t] = state
    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    calib_dir: str | Path,
    horizon_h: int,
    seed: int,
    coins: list[str],
    start: str = "2024-01-01",
) -> dict[str, pd.DataFrame]:
    """Generate a synthetic dfs dict from calibration stored in calib_dir.

    Arguments:
        calib_dir:  Path to directory containing {coin}.json and
                    _cross_funding_corr.json (output of calibrate_stats T2).
        horizon_h:  Number of hourly steps to simulate.
        seed:       Integer random seed (deterministic replay).
        coins:      List of coin symbols to generate.
        start:      ISO date string for first DatetimeIndex timestamp.

    Returns:
        dict mapping coin → pd.DataFrame with columns ['close', 'fundingRate']
        and a timezone-naive hourly DatetimeIndex of length horizon_h.
    """
    calib_dir = Path(calib_dir)
    per_coin, cross = _load_calib_dir(calib_dir)

    missing = [c for c in coins if c not in per_coin]
    if missing:
        raise ValueError(f"No calibration JSON for coins: {missing}")

    rng = np.random.default_rng(seed)
    n = len(coins)

    # ── Pre-compute log-level AR(1) parameters ───────────────────────────────
    log_params: list[dict] = []
    for coin in coins:
        c = per_coin[coin]
        phi_f = c["funding_ar1_phi"]  # f-space AR(1) coefficient (calibrated)
        offset, mu_g, sigma_g, phi_g, _ = _calibrate_log_ar1(
            fund_mean_h=c["funding_mean_h"],
            fund_std_h=c["funding_std_h"],
            neg_share=c["negative_hours_share"],
            phi_f=phi_f,
        )

        mu_hot = c["regime_hot_funding_annual_pct"] / HOURS_PER_YEAR / 100
        mu_cold = c["regime_cold_funding_annual_pct"] / HOURS_PER_YEAR / 100

        delta_hot, delta_cold, within_sigma_g = _compute_regime_deltas(
            mu_g, sigma_g, offset, mu_hot, mu_cold
        )

        # sigma_innov_g uses within-regime sigma and g-space phi
        sigma_innov_g = within_sigma_g * float(np.sqrt(max(1.0 - phi_g * phi_g, 1e-10)))

        log_params.append({
            "offset": offset,
            "mu_g": mu_g,
            "mu_g_hot": mu_g + delta_hot,
            "mu_g_cold": mu_g + delta_cold,
            "sigma_innov_g": sigma_innov_g,
            "phi_g": phi_g,
        })

    # ── Pre-compute hot/cold vol ratios for price ────────────────────────────
    vol_ratios = _build_vol_ratios(calib_dir, coins)

    # ── Cholesky of cross-funding correlation ────────────────────────────────
    cross_coins_set = set(cross["coins"])
    if all(c in cross_coins_set for c in coins):
        L = _cholesky_from_cross(cross, coins)
    else:
        L = np.eye(n)

    # ── Market-wide regime path ──────────────────────────────────────────────
    trans_freqs = [
        per_coin[c]["regime_transition_freq"]
        for c in coins
        if per_coin[c].get("regime_transition_freq") is not None
    ]
    p_transition = float(np.mean(trans_freqs)) if trans_freqs else 0.0008
    regime_path = _simulate_regime(rng, horizon_h, p_transition, start_hot=False)

    # ── Funding simulation (log-level AR(1)) ─────────────────────────────────
    funding_mat = np.zeros((horizon_h, n), dtype=float)

    phi_vec = np.array([p["phi_g"] for p in log_params], dtype=float)
    sigma_innov_vec = np.array([p["sigma_innov_g"] for p in log_params], dtype=float)
    mu_g_hot_vec = np.array([p["mu_g_hot"] for p in log_params], dtype=float)
    mu_g_cold_vec = np.array([p["mu_g_cold"] for p in log_params], dtype=float)
    offset_vec = np.array([p["offset"] for p in log_params], dtype=float)

    # Start at cold regime mean
    g_prev = mu_g_cold_vec.copy()

    for t in range(horizon_h):
        mu_g_r = mu_g_hot_vec if regime_path[t] == 1 else mu_g_cold_vec
        z_raw = rng.standard_normal(n)
        z_corr = L @ z_raw
        eps = sigma_innov_vec * z_corr
        g_t = mu_g_r + phi_vec * (g_prev - mu_g_r) + eps
        funding_mat[t] = np.exp(g_t) - offset_vec
        g_prev = g_t

    # ── Price simulation (regime-vol GBM + jumps) ────────────────────────────
    price_mat = np.zeros((horizon_h, n), dtype=float)

    mu_lr = np.array([per_coin[c]["log_return_mean_h"] for c in coins], dtype=float)
    cold_sigma = np.array([per_coin[c]["log_return_std_h"] for c in coins], dtype=float)
    hot_sigma = np.array([cold_sigma[i] * vol_ratios[coins[i]] for i in range(n)], dtype=float)
    jump_freq_vec = np.array([per_coin[c]["jump_freq"] for c in coins], dtype=float)
    jump_sigma_vec = JUMP_SIGMA_K * cold_sigma

    log_price = np.zeros(n, dtype=float)  # price starts at 100

    for t in range(horizon_h):
        sigma_t = hot_sigma if regime_path[t] == 1 else cold_sigma
        z_price = rng.standard_normal(n)
        log_ret = mu_lr + sigma_t * z_price
        jump_mask = rng.random(n) < jump_freq_vec
        log_ret += jump_mask * (rng.standard_normal(n) * jump_sigma_vec)
        log_price += log_ret
        price_mat[t] = np.exp(log_price) * 100.0

    # ── Assemble DataFrames ──────────────────────────────────────────────────
    idx = pd.date_range(start=start, periods=horizon_h, freq="h")
    return {
        coin: pd.DataFrame(
            {"close": price_mat[:, i], "fundingRate": funding_mat[:, i]},
            index=idx,
        )
        for i, coin in enumerate(coins)
    }


# ---------------------------------------------------------------------------
# Round-trip gate
# ---------------------------------------------------------------------------

def compute_round_trip_stats(
    calib_dir: str | Path,
    coins: list[str],
    horizon_h: int = 8760,
    n_paths: int = 1000,
    seed: int = 42,
) -> dict[str, dict]:
    """Generate n_paths synthetic paths and compare aggregate stats to calibration.

    Re-extracts the same statistics as calibrate_stats using its helper functions
    (imported via sys.path, not duplicated).  Returns a dict mapping each coin to
    a comparison dict with real_*, synth_* (median over paths), and diff_* keys.

    Arguments:
        calib_dir:  Path to calibration directory.
        coins:      Coins to validate.
        horizon_h:  Hours per path (default 8760 = 1 year).
        n_paths:    Number of independent paths (each uses seed + i).
        seed:       Base random seed.
    """
    import sys  # noqa: PLC0415

    calib_dir = Path(calib_dir)
    _pkg_dir = calib_dir.parents[1]  # research/two_phase_margin/
    if str(_pkg_dir) not in sys.path:
        sys.path.insert(0, str(_pkg_dir))

    from monte_carlo.calibrate_stats import _ar1_phi  # noqa: PLC0415

    # Load real calibration
    per_coin: dict[str, dict] = {}
    for coin in coins:
        with open(calib_dir / f"{coin}.json") as f:
            per_coin[coin] = json.load(f)

    # Accumulate per-path stats
    agg: dict[str, dict[str, list]] = {
        c: {"neg_share": [], "fund_mean_h": [], "fund_std_h": [],
            "ar1_phi": [], "lr_std": [], "excess_kurt": []}
        for c in coins
    }

    for i in range(n_paths):
        dfs = generate(calib_dir, horizon_h, seed=seed + i, coins=coins)
        for coin in coins:
            df = dfs[coin]
            fr = df["fundingRate"]
            cl = df["close"]
            agg[coin]["neg_share"].append(float((fr < 0).mean()))
            agg[coin]["fund_mean_h"].append(float(fr.mean()))
            agg[coin]["fund_std_h"].append(float(fr.std(ddof=1)))
            agg[coin]["ar1_phi"].append(_ar1_phi(fr))
            lr = np.log(cl / cl.shift(1)).dropna()
            agg[coin]["lr_std"].append(float(lr.std(ddof=1)))
            agg[coin]["excess_kurt"].append(
                float(lr.kurtosis()) if len(lr) >= 4 else float("nan")
            )

    # Aggregate: median across paths
    out: dict[str, dict] = {}
    for coin in coins:
        real = per_coin[coin]
        a = agg[coin]

        s_neg = float(np.median(a["neg_share"]))
        s_mean = float(np.median(a["fund_mean_h"]))
        s_std = float(np.median(a["fund_std_h"]))
        s_phi = float(np.median(a["ar1_phi"]))
        s_lr_std = float(np.median(a["lr_std"]))
        s_kurt = float(np.nanmedian(a["excess_kurt"]))

        s_mean_ann = s_mean * HOURS_PER_YEAR * 100

        out[coin] = {
            "real_negative_hours_share": real["negative_hours_share"],
            "synth_negative_hours_share": s_neg,
            "diff_negative_hours_share": s_neg - real["negative_hours_share"],
            "real_funding_mean_annual_pct": real["funding_mean_annual_pct"],
            "synth_funding_mean_annual_pct": s_mean_ann,
            "diff_funding_mean_annual_pct": s_mean_ann - real["funding_mean_annual_pct"],
            "real_funding_std_h": real["funding_std_h"],
            "synth_funding_std_h": s_std,
            "diff_funding_std_h_rel": (s_std - real["funding_std_h"]) / real["funding_std_h"],
            "real_funding_ar1_phi": real["funding_ar1_phi"],
            "synth_funding_ar1_phi": s_phi,
            "diff_funding_ar1_phi": s_phi - real["funding_ar1_phi"],
            "real_log_return_std_h": real["log_return_std_h"],
            "synth_log_return_std_h": s_lr_std,
            "diff_log_return_std_h_rel": (
                (s_lr_std - real["log_return_std_h"]) / real["log_return_std_h"]
            ),
            "synth_excess_kurtosis": s_kurt,
            "real_excess_kurtosis": real["excess_kurtosis"],
        }

    return out


def print_round_trip_table(
    calib_dir: str | Path,
    coins: list[str] | None = None,
    horizon_h: int = 8760,
    n_paths: int = 1000,
    seed: int = 42,
) -> None:
    """Print the round-trip gate table to stdout."""
    calib_dir = Path(calib_dir)
    if coins is None:
        coins = [
            p.stem for p in sorted(calib_dir.glob("*.json"))
            if not p.name.startswith("_")
        ]

    print(f"Round-trip gate: {n_paths} paths × {horizon_h}h per path, seed={seed}")
    print(f"Coins: {coins}")
    print()

    rt = compute_round_trip_stats(calib_dir, coins, horizon_h, n_paths, seed)

    header = (
        f"{'Coin':<5} | {'Stat':<26} | {'Real':>10} | {'Synth':>10} | "
        f"{'Diff':>10} | {'OK?':>4}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    for coin in coins:
        r = rt[coin]
        rows = [
            ("neg_hours_share",
             r["real_negative_hours_share"],
             r["synth_negative_hours_share"],
             r["diff_negative_hours_share"],
             abs(r["diff_negative_hours_share"]) <= 0.05,
             "abs<=0.05"),
            ("fund_mean_ann_%",
             r["real_funding_mean_annual_pct"],
             r["synth_funding_mean_annual_pct"],
             r["diff_funding_mean_annual_pct"],
             (abs(r["diff_funding_mean_annual_pct"]) <= 3.0 or
              abs(r["diff_funding_mean_annual_pct"] / max(
                  abs(r["real_funding_mean_annual_pct"]), 1e-9)) <= 0.30),
             "rel30%/abs3pp"),
            ("fund_std_h_rel",
             r["real_funding_std_h"],
             r["synth_funding_std_h"],
             r["diff_funding_std_h_rel"],
             abs(r["diff_funding_std_h_rel"]) <= 0.25,
             "rel<=25%"),
            ("ar1_phi",
             r["real_funding_ar1_phi"],
             r["synth_funding_ar1_phi"],
             r["diff_funding_ar1_phi"],
             abs(r["diff_funding_ar1_phi"]) <= 0.12,
             "abs<=0.12"),
            ("lr_std_h_rel",
             r["real_log_return_std_h"],
             r["synth_log_return_std_h"],
             r["diff_log_return_std_h_rel"],
             abs(r["diff_log_return_std_h_rel"]) <= 0.20,
             "rel<=20%"),
            ("excess_kurtosis",
             r["real_excess_kurtosis"],
             r["synth_excess_kurtosis"],
             r["synth_excess_kurtosis"] - r["real_excess_kurtosis"],
             r["synth_excess_kurtosis"] > 1.0,
             ">1"),
        ]
        for stat, real_v, synth_v, diff_v, ok, tol in rows:
            ok_str = "OK" if ok else "FAIL"
            print(
                f"{coin:<5} | {stat:<26} | {real_v:10.6f} | "
                f"{synth_v:10.6f} | {diff_v:+10.6f} | {ok_str:>4}  [{tol}]"
            )
        print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    _DEFAULT_CALIB = Path(__file__).resolve().parents[1] / "calibration"
    _COINS = ["BTC", "ETH", "SOL", "HYPE", "PURR"]

    parser = argparse.ArgumentParser(description="Parametric generator round-trip gate")
    parser.add_argument("--calib-dir", default=str(_DEFAULT_CALIB))
    parser.add_argument("--coins", nargs="+", default=_COINS)
    parser.add_argument("--n-paths", type=int, default=1000)
    parser.add_argument("--horizon-h", type=int, default=8760)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print_round_trip_table(
        calib_dir=args.calib_dir,
        coins=args.coins,
        horizon_h=args.horizon_h,
        n_paths=args.n_paths,
        seed=args.seed,
    )
