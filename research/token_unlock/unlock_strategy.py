"""
Token-unlock short-book strategy.

THESIS: Cliff token unlocks create predictable sell pressure that the market
front-runs. Shorting coin (market-hedged) in the window [-W, -1] before a
large cliff unlock captures the abnormal return.

MARKET-HEDGE: abnormal_ret[t] = coin_ret[t] - equal_weight_universe_ret[t]
  (market-adjusted excess return — beta ~1 for most alts, so EW market
   proxy is cleaner than estimating rolling beta per coin)

SIZING: proportional to unlock size (unlock fraction of max supply) —
  weight_raw ∝ unlock_size  → normalize across active shorts daily so
  the total book is 1-unit short (with market hedge long).
  Equal-weight mode: every active event weight=1 (normalized to 1/N).

NO LOOK-AHEAD: the unlock date d is known in advance (public schedule).
  At bar t ∈ [d-W, d-1] we are short. Return earned is fwd_ret[t] (the
  return from t to t+1, forward-aligned). Weight[t] · fwd_ret[t] is the
  correct PnL construction.

COSTS: 4.4 bps per leg one-way (perp taker 3.5 + slippage 0.9).
  Entry at t=d-W, exit at t=d-1 (close before unlock).
  Market hedge is the EW basket of remaining universe coins — also pays
  per-leg costs. Round-trip = 2 legs × 4.4 bps = 8.8 bps per trade.
  We model this as a turnover-proportional daily drag:
    cost_per_period = entry_cost + exit_cost spread over W bars.
  Simplified: charge entry cost on first bar of new positions, exit cost
  on last bar (position change ≥ threshold).

SIGNAL CONSTRUCTION (seam-safe):
  signal_weight[t] = Σ (size_i / Σ_j size_j) for all events i whose
    window covers t (i.e., event.date - W <= t <= event.date - 1).
  This is a PURE weight, known at t from the public schedule.

Parameters
----------
W : int      entry window (days before unlock). e.g. 10 = [-10, -1]
thr : float  minimum unlock size (fraction of max supply). e.g. 0.01 = 1%
sizing : str "prop" (proportional to size) | "equal" (equal weight)
top_n : int  use only top-N largest events per coin per window (0 = all)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent

# Cost constants (4.4 bps/leg = perp taker 3.5 + slippage ~0.9)
COST_PER_LEG_BPS = 4.4
COST_PER_LEG = COST_PER_LEG_BPS / 10_000

# Default parameters
DEFAULT_W = 10
DEFAULT_THR = 0.01   # 1% of max supply
DEFAULT_SIZING = "prop"   # "prop" | "equal"


def _load_price_panel(coins: list[str]) -> dict:
    """Load aligned daily price + fwd_ret panel for given coins.

    Uses research/cross_sectional/crypto/cryptodata.load_panel.
    """
    import sys
    crypto_dir = str(_HERE.parent / "cross_sectional" / "crypto")
    if crypto_dir not in sys.path:
        sys.path.insert(0, crypto_dir)
    import cryptodata
    # Only coins that have local data
    data_dir = _HERE.parent / "cross_sectional" / "crypto" / "data"
    available = [c for c in coins
                 if (data_dir / f"{c}_1h.csv").exists()]
    if not available:
        raise ValueError("No coins with local price data")
    return cryptodata.load_panel(coins=available)


def build_book(
    events: pd.DataFrame,
    *,
    W: int = DEFAULT_W,
    thr: float = DEFAULT_THR,
    sizing: str = DEFAULT_SIZING,
    top_n: int = 0,
    min_price_history_days: int = 30,
    squeeze_lookback: int = 0,
    squeeze_thr: float = 0.30,
) -> pd.Series:
    """Build daily PnL series for the token-unlock short book.

    Parameters
    ----------
    events  : DataFrame from unlock_data.load_events()
    W       : window length in days ([-W, -1] before unlock date)
    thr     : minimum unlock size to include event
    sizing  : "prop" = weight ∝ unlock size | "equal" = equal weight
    top_n   : if > 0, keep only the top_n events by size per coin (reduces
               clustered events from protocols with many small cliffs)
    min_price_history_days : skip events where coin has < this many price bars

    Returns
    -------
    pd.Series of daily PnL (market-neutral: short coin, long EW market),
    net of entry/exit costs, indexed by UTC date.
    """
    # ── Filter events ─────────────────────────────────────────────────────────
    ev = events.copy()
    ev = ev[ev["size"] >= thr].copy()

    # Only coins with local price data
    coins_in_ev = ev["coin"].unique().tolist()
    panel = _load_price_panel(coins_in_ev)
    available_coins = set(panel["coins"])
    ev = ev[ev["coin"].isin(available_coins)].copy()
    if ev.empty:
        raise ValueError("No events after filtering — check thr/coin availability")

    # Optional: per-coin top_n by size
    if top_n > 0:
        ev = ev.groupby("coin", group_keys=False).apply(
            lambda g: g.nlargest(top_n, "size")
        ).reset_index(drop=True)

    # ── Panel setup ───────────────────────────────────────────────────────────
    price = panel["price"]        # DataFrame[date × coin]
    fwd_ret = panel["fwd_ret"]    # DataFrame[date × coin], forward (seam-safe)
    coins = panel["coins"]

    # Daily index
    idx = price.index             # DatetimeIndex (UTC, daily)

    # Only events whose date falls within our panel range
    ev = ev[
        (ev["date"] >= idx.min()) &
        (ev["date"] <= idx.max())
    ].copy()

    # Normalize event date to panel resolution (floor to day UTC)
    ev["date"] = pd.to_datetime(ev["date"], utc=True).dt.normalize()

    # ── Market proxy: EW return of all available universe coins ──────────────
    market_ret = fwd_ret[list(available_coins)].mean(axis=1)   # Series[date]

    # Coin excess return (market-adjusted abnormal return)
    excess_ret: dict[str, pd.Series] = {}
    for coin in available_coins:
        if coin in fwd_ret.columns:
            excess_ret[coin] = fwd_ret[coin] - market_ret

    # ── Build daily weight matrix ─────────────────────────────────────────────
    # weight[t, coin] = sum of active event weights for coin on day t
    # An event (coin c, unlock_date d, size s) is "active" if t ∈ [d-W, d-1]
    weight_mat = pd.DataFrame(0.0, index=idx, columns=list(available_coins))

    # Track position changes for cost calculation
    prev_weight: dict[str, float] = {c: 0.0 for c in available_coins}
    cost_arr = pd.Series(0.0, index=idx)

    # Squeeze filter precompute: trailing market-adjusted run-up known at entry.
    # Mechanism (hypothesis): a coin pumping into its unlock has crowded shorts →
    # squeeze risk (the source of the −68% DD events, e.g. JUP). Skip events whose
    # coin outperformed the market by > squeeze_thr over squeeze_lookback days
    # ending at entry. Uses ONLY past prices → no look-ahead.
    #
    # FINDING (2026-06-24, tested L∈{10,20}, thr∈{20,30,50}%): REJECTED — does NOT
    # reduce maxDD. L=10 filters ~nothing; L=20 removes WINNERS (pnl 118%→89%), not
    # the squeezes. Pre-entry momentum is not a squeeze predictor here: the blow-ups
    # came from coins that did NOT run up beforehand. Left OFF by default (=0). Not
    # tuned further on purpose — that would be overfitting. See README.
    if squeeze_lookback > 0:
        trail = price[list(available_coins)] / price[list(available_coins)].shift(squeeze_lookback) - 1.0
        mkt_trail = trail.mean(axis=1)
        abn_trail = trail.sub(mkt_trail, axis=0)   # market-adjusted run-up per coin/day

    # For each event, add weight on active days
    n_skip_squeeze = 0
    for _, row in ev.iterrows():
        coin = row["coin"]
        unlock_date = row["date"]
        size = row["size"]

        # Active window: [unlock_date - W days, unlock_date - 1 day]
        entry_date = unlock_date - pd.Timedelta(days=W)
        exit_date  = unlock_date - pd.Timedelta(days=1)

        # Squeeze filter: skip if coin ran up into the window vs market
        if squeeze_lookback > 0 and coin in abn_trail.columns:
            e_pos = idx.searchsorted(entry_date)
            if 0 <= e_pos < len(idx):
                ru = abn_trail[coin].iloc[e_pos]
                if np.isfinite(ru) and ru > squeeze_thr:
                    n_skip_squeeze += 1
                    continue

        # Find panel dates within [entry_date, exit_date]
        mask = (idx >= entry_date) & (idx <= exit_date)
        active_days = idx[mask]
        if len(active_days) == 0:
            continue

        # Raw weight for this event (prop or equal)
        raw_w = size if sizing == "prop" else 1.0
        for d in active_days:
            weight_mat.loc[d, coin] += raw_w

    # ── Normalize daily weights so book is 1-unit short total ─────────────────
    # Row-wise: normalize by total weight across coins (so Σ_coin weight = 1)
    row_sums = weight_mat.sum(axis=1)
    has_pos = row_sums > 0
    norm_weights = weight_mat.copy()
    norm_weights.loc[has_pos] = weight_mat.loc[has_pos].div(row_sums[has_pos], axis=0)

    # ── Compute daily PnL ─────────────────────────────────────────────────────
    # PnL[t] = Σ_coin (−weight[t,coin] · excess_ret[t,coin])
    #        = −(weights · excess_ret)  (short the unlocking coin, long market)
    #
    # excess_ret[t] = coin_fwd_ret[t] − market_fwd_ret[t]
    # Since we are short the COIN vs long the MARKET, the PnL is:
    #   pnl = -weight · coin_fwd_ret + weight · market_fwd_ret
    #       = -weight · (coin_fwd_ret - market_fwd_ret) = -weight · excess_ret
    # If the coin drops more than market (negative excess), short profits → pos PnL ✓

    pnl = pd.Series(0.0, index=idx)
    for coin in available_coins:
        if coin not in excess_ret:
            continue
        w_col = norm_weights[coin]
        er_col = excess_ret[coin]
        # Use fillna(0) on contribution: no position → no PnL even if er is NaN.
        # If there IS a position but er is NaN (delisted/missing data), treat as 0.
        contribution = (-w_col * er_col).fillna(0.0)
        pnl += contribution

    # ── Transaction costs ─────────────────────────────────────────────────────
    # Charge cost when weight changes (turnover):
    #   cost[t] = |Δweight[t]| × COST_PER_LEG  (entry or exit — one-way)
    # Both the short leg AND the market-hedge leg pay costs.
    # Market hedge turnover mirrors the short book (same weight change magnitude).
    prev_w = pd.DataFrame(0.0, index=idx, columns=list(available_coins))
    prev_w = norm_weights.shift(1).fillna(0.0)
    turnover = (norm_weights - prev_w).abs().sum(axis=1)  # sum of |Δw| across coins
    # Short leg cost + hedge leg cost (2× per unit turnover)
    cost_series = turnover * COST_PER_LEG * 2.0

    pnl_net = pnl - cost_series

    # Drop leading zeros (before any events active)
    first_nonzero = (norm_weights.sum(axis=1) > 0).idxmax()
    pnl_net = pnl_net.loc[first_nonzero:]

    return pnl_net


def build_book_equal_weight(events: pd.DataFrame, *, W: int = 10,
                            thr: float = 0.01) -> pd.Series:
    """Convenience: equal-weight book (diagnostic baseline)."""
    return build_book(events, W=W, thr=thr, sizing="equal")


def event_study_car(
    events: pd.DataFrame,
    *,
    pre: int = 10,
    post: int = 5,
    thr: float = 0.0,
    n_bootstrap: int = 2000,
    seed: int = 42,
) -> pd.DataFrame:
    """Event-study: cumulative abnormal return (CAR) around cliff unlocks.

    Computes market-adjusted CAR at each day relative to unlock date
    (day 0 = unlock day), averaged across events above threshold.

    Returns DataFrame with columns:
        day, mean_car, median_car, ci_lo_95, ci_hi_95, n_events
    where CIs are bootstrap (event-block bootstrap).
    """
    coins_in_ev = events["coin"].unique().tolist()
    panel = _load_price_panel(coins_in_ev)
    available_coins = set(panel["coins"])
    price = panel["price"]
    fwd_ret = panel["fwd_ret"]
    idx = price.index

    market_ret = fwd_ret[list(available_coins)].mean(axis=1)

    ev = events[events["coin"].isin(available_coins)].copy()
    if thr > 0:
        ev = ev[ev["size"] >= thr].copy()
    ev["date"] = pd.to_datetime(ev["date"], utc=True).dt.normalize()
    # Only events in panel range with enough history and future
    ev = ev[
        (ev["date"] >= idx.min() + pd.Timedelta(days=pre + 5)) &
        (ev["date"] <= idx.max() - pd.Timedelta(days=post + 5))
    ].copy()

    if ev.empty:
        return pd.DataFrame()

    # Compute per-event CAR from -pre to +post
    rel_days = list(range(-pre, post + 1))
    car_matrix = []   # list of arrays (one per event)

    for _, row in ev.iterrows():
        coin = row["coin"]
        unlock_date = row["date"]
        if coin not in fwd_ret.columns:
            continue
        coin_er = fwd_ret[coin] - market_ret  # abnormal return series

        # Get returns for each relative day
        event_cars = []
        try:
            d_idx = idx.get_loc(unlock_date)
        except KeyError:
            # Find closest date
            d_idx = idx.searchsorted(unlock_date)
            if d_idx >= len(idx):
                continue

        ars = []
        valid = True
        for rel in rel_days:
            abs_idx = d_idx + rel
            if abs_idx < 0 or abs_idx >= len(idx):
                valid = False
                break
            ar = coin_er.iloc[abs_idx]
            ars.append(ar if np.isfinite(ar) else 0.0)

        if valid:
            cum = np.cumsum(ars)
            car_matrix.append(cum)

    if not car_matrix:
        return pd.DataFrame()

    car_arr = np.array(car_matrix)   # (n_events, n_days)
    n_events = len(car_arr)

    # Bootstrap CI (event-block bootstrap)
    rng = np.random.default_rng(seed)
    boot_means = []
    for _ in range(n_bootstrap):
        boot_idx = rng.integers(0, n_events, size=n_events)
        boot_means.append(car_arr[boot_idx].mean(axis=0))
    boot_means = np.array(boot_means)  # (n_bootstrap, n_days)

    result = pd.DataFrame({
        "day": rel_days,
        "mean_car": car_arr.mean(axis=0),
        "median_car": np.median(car_arr, axis=0),
        "ci_lo_95": np.percentile(boot_means, 2.5, axis=0),
        "ci_hi_95": np.percentile(boot_means, 97.5, axis=0),
        "n_events": n_events,
    })
    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_HERE))
    from unlock_data import load_events

    print("=== Building token-unlock short book ===")
    events = load_events(verbose=False)
    print(f"Events loaded: {len(events)}, coins: {events['coin'].nunique()}")

    # Test default config
    pnl = build_book(events, W=DEFAULT_W, thr=DEFAULT_THR, sizing=DEFAULT_SIZING)
    ann = pnl.mean() * 252
    std = pnl.std() * np.sqrt(252)
    sr = ann / std if std > 0 else 0.0
    print(f"\nDefault (W={DEFAULT_W}, thr={DEFAULT_THR:.0%}, {DEFAULT_SIZING}):")
    print(f"  Ann return: {ann:.2%}  Vol: {std:.2%}  Sharpe: {sr:.2f}")
    print(f"  N days: {len(pnl)}  Date: {pnl.index[0].date()} → {pnl.index[-1].date()}")

    # Event study
    print("\n=== Event study CAR ===")
    for thr_val in [0.005, 0.01, 0.02]:
        n = len(events[events["size"] >= thr_val])
        study = event_study_car(events, pre=10, post=5, thr=thr_val)
        if not study.empty:
            car_pre = study[study["day"] == -1]["mean_car"].values[0]
            car_day0 = study[study["day"] == 0]["mean_car"].values[0]
            print(f"  thr={thr_val:.0%}: n_events={n}, "
                  f"CAR[-10,-1]={car_pre:.3%}, CAR[0]={car_day0:.3%}")
