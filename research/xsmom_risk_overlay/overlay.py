"""
Risk-overlay logic for the weekly XSMOM long-short book (RESEARCH ONLY).

Overlays built ON TOP of the existing engine pieces:
  - Arm A  vol_target_scale  — rescale the gross of a carry-forward weight book
                               by target_vol / trailing_realised_vol (CAUSAL).
  - Arm B  path_aware_overlay(mode="stop")        — intra-window paired stop.
  - Arm C  path_aware_overlay(mode="take_profit") — intra-window paired take-profit.
  - Arms D/E  replacement_overlay(mode="stop"|"take_profit", vol_linked=False)
                               — SINGLE-LEG replacement (close only the triggered leg,
                                 open next-best same-side coin). Fixed-% threshold.
  - Arms F/G  replacement_overlay(mode="stop"|"take_profit", vol_linked=True)
                               — same, but threshold = k * σ_coin (per-coin causal vol).

Baseline and Arm A run on the vanilla xsec engine (carry-forward weights held
constant between weekly rebalances). Arms B/C/D/E/F/G need an INTRA-window path
simulation, because xsec.portfolio_returns holds weights constant between
rebalances and never looks inside the hold window — a stop/take-profit must track
each position's cumulative PnL DAY BY DAY within its hold window and cut legs
mid-window. This module implements that path-aware simulator from scratch (numpy/
pandas only), reusing xsec.rank_to_weights for the weekly target book.

NO LOOK-AHEAD (mirrors xsec.py / signals.py contract — verified in selftest.py):
  - scores[t] use info <= t; weight[t] earns fwd_ret[t] (fwd_ret aligned by caller
    via cryptodata.load_panel, which is r_{t+1} indexed at t).
  - Arm A: the vol estimate that scales the book entering period t uses portfolio
    returns realised STRICTLY BEFORE t (shifted by 1), never the period-t return.
  - Arms B/C: a leg's running PnL on day d inside the window uses fwd_ret up to and
    including day d (the move that already happened); the cut takes effect on the
    SAME day's pnl is NOT counted retroactively — once cumulative PnL crosses the
    threshold AT END of day d, the leg is zeroed for day d+1 onward (the triggering
    day's return is kept — you cannot unwind a move you only observe at its close).
  - Arms D/E/F/G: same timing convention as B/C for the trigger.  σ_coin at day d
    uses per-coin daily returns up to and including day d-1 (trailing 20-day window,
    shifted by 1 so day-d return is NOT used).  The replacement coin is picked from
    the ranking known at day d (scores[d] use data <= d), and starts earning from d+1.

All return series are in fractional (per-day) units, same convention as
xsec.portfolio_returns, ready to feed compute_metrics via the harness.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import xsec


# ── Arm A: book-level causal vol target ────────────────────────────────────────

def realised_vol(pnl: pd.Series, window: int, ewma: bool = False) -> pd.Series:
    """Trailing realised daily vol of a pnl series, CAUSAL (uses returns <= t).

    rolling std (ddof=0) or EWMA std over `window`. The value at t summarises
    returns up to and including t; the CALLER shifts by 1 before scaling so the
    scaler entering period t never uses the period-t return (no look-ahead).
    """
    if ewma:
        return pnl.ewm(span=window, min_periods=window).std(bias=True)
    return pnl.rolling(window, min_periods=window).std(ddof=0)


def vol_target_scale(
    base_pnl: pd.Series,
    target_vol_annual: float,
    vol_window: int,
    *,
    ewma: bool = False,
    max_leverage: float = 5.0,
    periods_per_year: int = 252,
) -> pd.Series:
    """Arm A: rescale a book's pnl by target_vol / trailing_realised_vol (CAUSAL).

    scaler[t] = clip( target_vol_daily / vol_est[t-1] , 0, max_leverage )
    out[t]    = scaler[t] * base_pnl[t]

    vol_est is the trailing realised daily vol of base_pnl; we SHIFT it by one
    period so the leverage applied during period t is decided from information
    available BEFORE t (no look-ahead — the day-t return is not used to size day t).
    Periods with an undefined trailing vol (warmup) pass through UNSCALED
    (scaler=1) so the warmup region matches the baseline rather than being zeroed.

    Only rescales the gross of the EXISTING carry-forward weights — no engine
    rewrite, no change to which coins are long/short. target_vol_daily =
    target_vol_annual / sqrt(periods_per_year).
    """
    target_daily = target_vol_annual / np.sqrt(periods_per_year)
    vol_est = realised_vol(base_pnl, vol_window, ewma=ewma).shift(1)
    scaler = (target_daily / vol_est).clip(lower=0.0, upper=max_leverage)
    scaler = scaler.where(vol_est.notna() & (vol_est > 0), 1.0)  # warmup → unscaled
    return (scaler * base_pnl).rename("xsec_voltgt")


# ── Arms B/C: path-aware paired stop / take-profit simulator ───────────────────

def _rebalance_indices(n: int, rebal_every: int) -> list[int]:
    return list(range(0, n, rebal_every))


def path_aware_overlay(
    weights: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    *,
    threshold: float,
    mode: str,                       # "stop" | "take_profit"
    pair_rule: str,                  # "worst_opposite" | "symmetric_rank"
    reentry: str,                    # "next_rebalance" | "none"
    costs_bps: float = 4.4,
    rebal_every: int = 7,
) -> pd.Series:
    """Path-aware long-short pnl with intra-window paired stop / take-profit.

    Holds the weekly target `weights` (from xsec.rank_to_weights) between rebalances
    like xsec.portfolio_returns, BUT walks day by day inside each hold window and
    tracks every open leg's cumulative PnL. When a leg's cumulative move crosses the
    threshold it is CUT, together with one PAIRED opposite-side leg, preserving
    dollar-neutrality. Cut capital sits in cash (0 return) until re-entry.

    Parameters
    ----------
    threshold : signed fraction.
        mode="stop":        a leg triggers when its cumulative PnL <= threshold
                            (threshold is NEGATIVE, e.g. -0.08 for -8%).
        mode="take_profit": a leg triggers when its cumulative PnL >= threshold
                            (threshold is POSITIVE, e.g. +0.08 for +8%).
    pair_rule :
        "worst_opposite"  — cut the opposite-side leg with the worst running PnL
                            (the laggard on the other side).
        "symmetric_rank"  — cut the opposite-side leg at the mirror rank (e.g. the
                            best long pairs with the worst short, by entry rank).
    reentry :
        "next_rebalance"  — cut legs come back at the very next weekly rebalance,
                            where the fresh target book re-establishes the position
                            (the natural, prompt re-entry).
        "none"            — a cut coin is BLACKLISTED for one extra cycle: it is NOT
                            re-established at the immediately following rebalance
                            (its weight is forced to 0 there) and only re-arms the
                            rebalance after. Models "don't jump straight back into a
                            name that just stopped you out." A genuinely distinct,
                            slower re-entry than next_rebalance.

    NO LOOK-AHEAD: on day d we (1) earn the day's gross with the weights ACTIVE at
    the START of day d, then (2) update each active leg's cumulative PnL with the
    day-d return, then (3) evaluate triggers on that cumulative PnL — a triggered
    leg is zeroed for day d+1 onward. The triggering day's own return is kept (you
    observe the move only at its close; you cannot retroactively avoid it).

    Costs: turnover * costs_bps/1e4 charged (a) at each rebalance (Σ|w_new - w_held|)
    and (b) when a stop/take-profit cut changes the held book mid-window
    (Σ|w_after_cut - w_before_cut|). Re-entry cost is captured by the next
    rebalance's turnover. Mirrors xsec.portfolio_returns' turnover accounting.
    """
    if mode not in ("stop", "take_profit"):
        raise ValueError(f"mode must be 'stop'|'take_profit', got {mode!r}")
    if pair_rule not in ("worst_opposite", "symmetric_rank"):
        raise ValueError(f"bad pair_rule {pair_rule!r}")
    if reentry not in ("next_rebalance", "none"):
        raise ValueError(f"bad reentry {reentry!r}")

    w = weights.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    cols = list(w.columns)
    idx = w.index
    n = len(idx)
    cost_rate = costs_bps / 1e4
    rebal_set = set(_rebalance_indices(n, rebal_every))

    held = np.zeros(len(cols))      # currently active weights (post-cut)
    prev = np.zeros(len(cols))      # weights as of the last turnover event
    target = np.zeros(len(cols))    # weekly target book for this window
    cum = np.zeros(len(cols))       # cumulative PnL of each currently-open leg
    blacklist = np.zeros(len(cols), dtype=bool)  # coins to suppress at NEXT rebalance

    out = np.zeros(n)

    for i in range(n):
        if i in rebal_set:
            # weekly rebalance: re-establish the full target book, reset path state.
            target = w.iloc[i].to_numpy(copy=True)
            if reentry == "none":
                # coins cut in the JUST-FINISHED window are suppressed at this
                # rebalance (forced flat for one extra cycle), then blacklist clears.
                # DOLLAR-NEUTRALITY: zeroing blacklisted coins may leave the book
                # imbalanced (e.g. 2 shorts, 1 long).  Re-normalise each side
                # independently so long-side sums to +1 and short-side sums to -1.
                target = np.where(blacklist, 0.0, target)
                long_idx = np.where(target > 0.0)[0]
                shrt_idx = np.where(target < 0.0)[0]
                long_n, shrt_n = long_idx.size, shrt_idx.size
                if long_n > 0 and shrt_n > 0:
                    # Trim the larger side to match the smaller side (drop worst legs
                    # by original rank, i.e. the ones closest to the neutral tercile).
                    # 'target' long legs are equal-weight from rank_to_weights, so
                    # any subset keeps equal-weight; just keep the first min(n) legs
                    # on each side (stable order from the weekly rank).
                    k = min(long_n, shrt_n)
                    target[long_idx[k:]] = 0.0   # zero excess longs
                    target[shrt_idx[k:]] = 0.0   # zero excess shorts
                    # re-normalise to ±1/k so each side sums to ±1
                    long_idx = long_idx[:k]
                    shrt_idx = shrt_idx[:k]
                    target[long_idx] = 1.0 / k
                    target[shrt_idx] = -1.0 / k
                elif long_n == 0 or shrt_n == 0:
                    # One side completely blacklisted → no valid book; go flat.
                    target = np.zeros(len(cols))
            blacklist = np.zeros(len(cols), dtype=bool)
            held = target.copy()
            cum = np.zeros(len(cols))
            turnover = np.abs(held - prev).sum()
            prev = held.copy()
            cost = turnover * cost_rate
        else:
            cost = 0.0

        ri = r.iloc[i].to_numpy()
        # (1) earn the day with the weights active at the START of day i
        gross = float(np.dot(held, ri))

        # (2) update cumulative PnL of every still-open leg with the day-i move.
        #     Per-unit-of-weight running return = sign(weight) * price-move so a
        #     short leg's "PnL" is positive when the coin falls. We track the
        #     position's cumulative pnl as the per-dollar return of that leg.
        open_mask = (held != 0.0)
        # per-leg directional return contribution this day, normalised per unit
        # leg size (|held|), so cum is comparable to the threshold in %.
        with np.errstate(invalid="ignore", divide="ignore"):
            leg_ret = np.where(open_mask, np.sign(held) * ri, 0.0)
        cum = cum + leg_ret

        # (3) evaluate triggers on the post-day cumulative PnL.
        if mode == "stop":
            triggered = open_mask & (cum <= threshold)
        else:  # take_profit
            triggered = open_mask & (cum >= threshold)

        if triggered.any():
            # pair each triggered leg with an opposite-side open leg, then cut both.
            before = held.copy()
            held = _apply_paired_cuts(
                held, target, cum, triggered, pair_rule, cols
            )
            # record which coins got cut (for reentry="none" blacklist next rebalance)
            newly_cut = (before != 0.0) & (held == 0.0)
            blacklist = blacklist | newly_cut
            # mid-window turnover cost for the cut (book changed without rebalance)
            turn_cut = np.abs(held - prev).sum()
            cost += turn_cut * cost_rate
            prev = held.copy()
            # cut legs no longer accumulate; their cum is frozen (irrelevant once 0)

        out[i] = gross - cost

    return pd.Series(out, index=idx, name=f"xsec_{mode}")


def _apply_paired_cuts(
    held: np.ndarray,
    target: np.ndarray,
    cum: np.ndarray,
    triggered: np.ndarray,
    pair_rule: str,
    cols: list[str],
) -> np.ndarray:
    """Zero each triggered leg AND a paired opposite-side OPEN leg (dollar-neutral).

    The pairing keeps the book dollar-neutral: each cut removes one long-unit and
    one short-unit of equal size (terciles are equal-weight, so |w| is identical
    across legs on a side). Returns the updated `held` (a fresh array).
    """
    held = held.copy()
    long_open = lambda: np.where((held > 0.0))[0]
    short_open = lambda: np.where((held < 0.0))[0]

    trig_idx = np.where(triggered & (held != 0.0))[0]
    for j in trig_idx:
        if held[j] == 0.0:
            continue  # already cut as someone's pair
        side = np.sign(held[j])
        opp = short_open() if side > 0 else long_open()
        if opp.size == 0:
            # no opposite leg left to pair with → just zero this leg (book may
            # become slightly net; rare endgame of a window). Keeps it simple.
            held[j] = 0.0
            continue
        if pair_rule == "worst_opposite":
            # opposite-side leg with the WORST running PnL (the laggard)
            pair = opp[np.argmin(cum[opp])]
        else:  # symmetric_rank: pair with opposite leg of mirror entry rank
            pair = _symmetric_rank_pair(j, target, held, side, cols)
            if pair is None:
                pair = opp[np.argmin(cum[opp])]  # fallback
        held[j] = 0.0
        held[pair] = 0.0
    return held


def _symmetric_rank_pair(j, target, held, side, cols):
    """Mirror-rank partner: the long entered at rank r pairs with the short entered
    at the mirror rank (best long ↔ worst short). Ranks are by the ENTRY target
    book magnitudes — but terciles are equal-weight so magnitude can't rank within a
    side. We therefore approximate mirror-rank by position ORDER within each side
    (stable, deterministic): the k-th still-open leg on side `side` pairs with the
    k-th still-open leg on the opposite side. Returns an index or None."""
    same_side = np.where(np.sign(target) == side)[0]
    opp_side_open = np.where((np.sign(target) == -side) & (held != 0.0))[0]
    if opp_side_open.size == 0:
        return None
    # position of j among the same-side ENTRY legs (mirror that ordinal)
    pos = int(np.where(same_side == j)[0][0]) if j in same_side else 0
    # mirror ordinal from the opposite end
    mirror = opp_side_open[-1 - (pos % opp_side_open.size)]
    return int(mirror)


# ── Arms D/E/F/G: single-leg replacement overlay ───────────────────────────────

def coin_realised_vol(
    fwd_ret: pd.DataFrame,
    window: int = 20,
) -> pd.DataFrame:
    """Per-coin trailing realised daily vol, CAUSAL (uses returns ≤ t).

    Returns a DataFrame [date × coin] where the value at (t, c) is the
    rolling-std (ddof=0) of coin c's daily returns over the `window` days ending
    at t (inclusive).  The CALLER shifts this by 1 before using it as a trigger
    threshold so the day-d trigger uses vol estimated from returns ≤ d-1.

    Rows where the window hasn't fully filled yet are NaN (min_periods = window).
    """
    r = fwd_ret.fillna(0.0)
    return r.rolling(window, min_periods=window).std(ddof=0)


def replacement_overlay(
    weights: pd.DataFrame,
    scores: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    *,
    threshold: float,
    mode: str,          # "stop" | "take_profit"
    vol_linked: bool,   # False → fixed-% threshold; True → k*σ_coin threshold
    vol_window: int = 20,
    costs_bps: float = 4.4,
    rebal_every: int = 7,
) -> pd.Series:
    """Path-aware SINGLE-LEG replacement stop / take-profit (Arms D/E/F/G).

    Mechanism
    ---------
    Like path_aware_overlay (Arms B/C) but instead of cutting a PAIR of legs,
    only the TRIGGERED leg is closed and a REPLACEMENT leg is opened on the SAME
    SIDE using the next-best-ranked coin not currently held.  Dollar-neutrality is
    preserved automatically: long leg is replaced by another long, short leg by
    another short.  No opposite-side pairing rule is needed.

    Parameters
    ----------
    weights : pd.DataFrame
        Weekly XSMOM target book from xsec.rank_to_weights (daily, same rebalance
        cadence as the engine).  Shape: [date × coin].
    scores : pd.DataFrame
        Full daily momentum scores for ALL coins [date × coin].  Same index as
        weights.  Used to pick the best-ranked available replacement coin.
        scores[t] uses data ≤ t (no look-ahead — caller's responsibility).
    fwd_ret : pd.DataFrame
        Forward returns [date × coin], aligned as in xsec.portfolio_returns.
    threshold : float
        For vol_linked=False: signed fraction (e.g. -0.08 for -8% stop, +0.08
        for +8% take-profit).
        For vol_linked=True: the k multiplier (e.g. 1.5 → trigger when
        cum_pnl ≤ -1.5·σ_coin for stop, or cum_pnl ≥ +1.5·σ_coin for take-profit).
    mode : "stop" | "take_profit"
        "stop": trigger when leg's cumulative PnL ≤ threshold (or ≤ -k·σ).
        "take_profit": trigger when leg's cumulative PnL ≥ threshold (or ≥ +k·σ).
    vol_linked : bool
        If True, threshold is treated as k and the per-coin vol (shifted by 1, so
        the day-d trigger uses σ estimated from returns ≤ d-1) scales it.
    vol_window : int
        Rolling window for per-coin vol (used only when vol_linked=True).
    costs_bps : float
        Cost per unit of turnover, same as path_aware_overlay.
    rebal_every : int
        Weekly rebalance cadence (default 7).

    Timing (NO look-ahead)
    ----------------------
    - On day d: (1) earn gross with weights active at START of day d; (2) update
      each open leg's cumulative PnL with day-d return; (3) evaluate trigger on
      updated cumPnl — triggered leg is replaced for day d+1 onward; the triggering
      day's own return is kept (cannot retroactively avoid it).
    - σ_coin at day d uses coin returns ≤ d-1 (coin_realised_vol shifted by 1).
    - Replacement coin is the highest-score (for longs) or lowest-score (for
      shorts) coin among those NOT currently held, using scores[d] (info ≤ d).
    - The new leg starts accumulating cumPnL from 0 at day d+1.
    - If no replacement coin is available (all same-side coins already held), the
      old leg is simply closed and the book shrinks temporarily until next rebalance.
    - At each weekly rebalance, the full target book is re-established from scratch
      (cumPnl resets).  There is no blacklist (each window starts fresh).

    Costs
    -----
    Turnover cost charged (a) at each rebalance and (b) when a replacement changes
    the held book mid-window (close old leg + open new leg = 2 × leg_weight turnover).
    """
    if mode not in ("stop", "take_profit"):
        raise ValueError(f"mode must be 'stop'|'take_profit', got {mode!r}")

    w = weights.reindex_like(fwd_ret).fillna(0.0)
    s = scores.reindex_like(fwd_ret)          # NaN where score unavailable
    r = fwd_ret.fillna(0.0)
    cols = list(w.columns)
    n_cols = len(cols)
    idx = w.index
    n = len(idx)
    cost_rate = costs_bps / 1e4
    rebal_set = set(_rebalance_indices(n, rebal_every))

    # Pre-compute per-coin trailing vol (shifted by 1 → causal for triggers)
    # Shape: [n × n_cols], numpy array for fast indexing
    if vol_linked:
        coin_vol_df = coin_realised_vol(r, window=vol_window).shift(1)
        coin_vol_arr = coin_vol_df.values    # [n × n_cols]
    else:
        coin_vol_arr = None                  # not used

    held = np.zeros(n_cols)    # currently active weights (post-replacement)
    prev = np.zeros(n_cols)    # weights as of last turnover event (for cost)
    cum = np.zeros(n_cols)     # cumulative PnL of each open leg since window open
    leg_weight = 0.0           # equal-weight per leg on each side (e.g. 1/k)

    out = np.zeros(n)

    for i in range(n):
        if i in rebal_set:
            # Re-establish full target book; reset all path state.
            target = w.iloc[i].to_numpy(copy=True)
            held = target.copy()
            cum = np.zeros(n_cols)
            turnover = np.abs(held - prev).sum()
            prev = held.copy()
            cost = turnover * cost_rate
            # Determine leg weight (equal-weight within a side; same for all legs).
            long_n = int((held > 0).sum())
            leg_weight = (1.0 / long_n) if long_n > 0 else 0.0
        else:
            cost = 0.0

        ri = r.iloc[i].to_numpy()
        # (1) Earn the day with weights active at START of day i.
        gross = float(np.dot(held, ri))

        # (2) Update cumulative PnL of every still-open leg.
        open_mask = (held != 0.0)
        leg_ret = np.where(open_mask, np.sign(held) * ri, 0.0)
        cum = cum + leg_ret

        # (3) Compute per-coin trigger threshold.
        if vol_linked and coin_vol_arr is not None:
            # σ_coin[d] uses returns ≤ d-1 (already shifted).
            sigma_i = coin_vol_arr[i]   # shape [n_cols]; NaN where warmup
            # threshold is k; signed trigger level per coin per side
            # stop:        trigger if cum ≤ -k*σ  (threshold is negative k, but
            #              by convention for vol_linked we take abs: k = threshold
            #              for stops the trigger is cum ≤ -k*sigma; for tp cum ≥ k*sigma)
            # We use |threshold| as k consistently.
            k = abs(threshold)
            if mode == "stop":
                # trigger where σ is defined; where NaN → no trigger (conservative)
                with np.errstate(invalid="ignore"):
                    vol_thr = -k * sigma_i
                    triggered = open_mask & np.where(
                        np.isfinite(sigma_i), cum <= vol_thr, False
                    )
            else:
                with np.errstate(invalid="ignore"):
                    vol_thr = k * sigma_i
                    triggered = open_mask & np.where(
                        np.isfinite(sigma_i), cum >= vol_thr, False
                    )
        else:
            if mode == "stop":
                triggered = open_mask & (cum <= threshold)
            else:
                triggered = open_mask & (cum >= threshold)

        if triggered.any():
            si_row = s.iloc[i].to_numpy()   # scores at day i (info ≤ i) for picking replacement
            trig_idx = np.where(triggered)[0]
            for j in trig_idx:
                if held[j] == 0.0:
                    continue  # already replaced as a cascade (shouldn't normally happen)
                side = np.sign(held[j])
                # Find replacement: same-side coin, NOT currently held, best score.
                # For longs (side=+1): highest score not already held.
                # For shorts (side=-1): lowest score not already held.
                not_held = (held == 0.0)
                if side > 0:
                    # Rank remaining not-held coins by score descending
                    candidates = np.where(not_held & np.isfinite(si_row))[0]
                    if candidates.size == 0:
                        # No replacement available; just close the leg.
                        held[j] = 0.0
                        cum[j] = 0.0
                    else:
                        best = candidates[np.argmax(si_row[candidates])]
                        # Close old, open replacement at same leg_weight.
                        held[j] = 0.0
                        held[best] = leg_weight
                        cum[j] = 0.0
                        cum[best] = 0.0   # new leg starts fresh from d+1
                else:
                    # Short side: lowest score (most negative) → short candidate
                    candidates = np.where(not_held & np.isfinite(si_row))[0]
                    if candidates.size == 0:
                        held[j] = 0.0
                        cum[j] = 0.0
                    else:
                        worst = candidates[np.argmin(si_row[candidates])]
                        held[j] = 0.0
                        held[worst] = -leg_weight
                        cum[j] = 0.0
                        cum[worst] = 0.0
            # Mid-window turnover cost for the replacement(s).
            turn_rep = np.abs(held - prev).sum()
            cost += turn_rep * cost_rate
            prev = held.copy()

        out[i] = gross - cost

    return pd.Series(out, index=idx, name=f"xsec_repl_{mode}")
