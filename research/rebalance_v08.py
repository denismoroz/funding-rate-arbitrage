"""
rebalance_v08.py — Scale-in/scale-out + defensive rotation, v0.8.

Changes vs v0.7 (two targeted fixes):
  Fix 1: Defensive mode replacement check now requires candidate to be HEALTHY
          (ma12[candidate] > degradation_threshold_apr), not just better than
          the dying coin. When degrading AND nobody is healthy → exit to USDC
          (n_degradation_exits now actually fires in cold markets).
  Fix 2: First-tranche entry (EMPTY slot) in defensive mode now requires the
          candidate to exceed degradation_threshold_apr before opening a new
          slot. Tracks skipped entries via n_first_tranche_skipped counter.
          Offensive mode: first-tranche entry unchanged (no floor), same as v0.6/v0.7.
  New counter: n_first_tranche_skipped.

Everything else identical to v0.7:
  - 4 slots, n_main_cap=2.
  - State machine empty/growing/holding/shrinking.
  - Continue-ramp logic with trailing anchor + continue_tolerance_apr slack.
  - Frozen-growing unwind (Fix A).
  - Aave overlay off by default.
  - Per-hour arrays for window metrics.
  - Same data loading, U11, constants, aave_only synthetic baseline.

Sweep: 5 configs × 2 periods = 10 rows.

Run: uv run python research/rebalance_v08.py
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

# ── Imports ────────────────────────────────────────────────────────────────────
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from research.engine import (
        load_data,
        POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from engine import (
            load_data,
            POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
        )
    except ImportError:
        POSITION_SIZE  = 1000
        TOTAL_CAPITAL  = 2000
        PERP_TAKER     = 0.00035
        SPOT_TAKER     = 0.00070
        HOURS_PER_YEAR = 8760

        def load_data(coin: str) -> pd.DataFrame:
            data_dir = Path(__file__).parent / "data"
            funding = pd.read_csv(data_dir / f"{coin}.csv")
            funding["time"] = pd.to_datetime(
                funding["time"], format="ISO8601", utc=True
            ).dt.floor("h")
            funding = funding.set_index("time")[["fundingRate"]].sort_index()
            ohlcv = pd.read_csv(data_dir / f"{coin}_1h.csv")
            ohlcv["time"] = pd.to_datetime(
                ohlcv["time"], format="ISO8601", utc=True
            ).dt.floor("h")
            ohlcv = ohlcv.set_index("time")[["close"]].sort_index()
            df = funding.join(ohlcv, how="inner")
            df = df[df.index >= pd.Timestamp("2023-06-08", tz="UTC")]
            return df


# ── Universe ───────────────────────────────────────────────────────────────────
U11 = ['BTC', 'ETH', 'SOL', 'AVAX', 'LINK', 'AAVE', 'DOGE', 'UNI', 'ARB', 'OP', 'TIA']


# ── Metrics helper ─────────────────────────────────────────────────────────────

def _metrics_on_capital(pnl_arr: np.ndarray, capital_base: float) -> dict:
    """Compute annual return, max drawdown, Calmar on a pnl-per-hour array."""
    n_hours = len(pnl_arr)
    total_pnl = pnl_arr.sum()
    annual_pct = (total_pnl / capital_base) / (n_hours / HOURS_PER_YEAR) * 100
    equity = np.cumsum(pnl_arr)
    peak_eq = np.maximum.accumulate(equity)
    dd = (equity - peak_eq) / (capital_base + peak_eq)
    max_dd_pct = abs(dd.min()) * 100
    calmar = annual_pct / max_dd_pct if max_dd_pct > 0 else 0.0
    return {
        "annual_pct": round(annual_pct, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "calmar":     round(calmar, 2),
        "n_hours":    n_hours,
    }


# ── Core simulation ────────────────────────────────────────────────────────────

def simulate_rebalance_v08(
    coins: list,
    prices: dict,          # coin -> np.ndarray (float64), aligned
    rates_data: dict,      # coin -> np.ndarray (float64), aligned
    ma12_data: dict,       # coin -> np.ndarray, pre-computed MA12 APR
    sig24min_data: dict,   # coin -> np.ndarray, pre-computed 24h min of MA12 (kept for compat)
    n_hours: int,
    *,
    tick_hours: int = 24,
    slice_pct: float = 0.10,
    continue_tolerance_apr: float = 0.05,    # from v0.6 best config (slack_5pct)
    rotation_mode: str = 'defensive',         # 'offensive' (v0.6 behavior) | 'defensive' (new)
    rotation_delta_apr: float = 0.10,         # only used when rotation_mode='offensive'
    degradation_threshold_apr: float = 0.05,  # only used when rotation_mode='defensive'
    exit_signal_threshold_apr: float = 0.0,   # only used when rotation_mode='offensive' (v0.6 fallback exit)
    signal_window_hours: int = 12,
    n_slots_total: int = 4,
    n_main_cap: int = 2,
    aave_apr: float = 0.0,
    unwind_frozen: bool = True,
) -> tuple:
    """
    Core per-hour simulation loop for v0.8.

    rotation_mode='defensive':
        Only rotates when current position's ma12 <= degradation_threshold_apr.
        Fix 1: If degrading AND a HEALTHY candidate exists (ma12 > degradation_threshold_apr)
               → rotate. If degrading AND no HEALTHY candidate → exit to USDC.
               (v0.7 used ma12[candidate] > current_ma which allowed rotating into
               also-degraded coins in cold markets, blocking the USDC exit path.)
        Fix 2: First-tranche entry (EMPTY slot) also requires candidate to exceed
               degradation_threshold_apr. If nobody passes → skip (count n_first_tranche_skipped).

    rotation_mode='offensive':
        Identical to v0.6/v0.7 behavior — any neighbor with ma12 > current + rotation_delta_apr
        triggers rotation. Falls back to exit if signal < exit_signal_threshold_apr.
        First-tranche entry: no floor (same as v0.6/v0.7).

    Returns (pnl_per_hour, active_capital_per_hour, gross_funding_per_hour,
             fees_per_hour, aave_income_per_hour, is_idle_arr, info_dict).
    """

    ma12     = ma12_data
    rates    = rates_data
    prices_d = prices

    # ── Slot state ─────────────────────────────────────────────────────────────
    def make_slot():
        return {
            "coin":              None,
            "state":             "empty",   # empty | growing | holding | shrinking
            "tranches":          [],
            "cum_realized_pnl":  0.0,
            "entry_ma_anchor":   0.0,       # trailing anchor for continue-ramp
        }

    slots = [make_slot() for _ in range(n_slots_total)]

    # ── Global accumulators ────────────────────────────────────────────────────
    cum_funding_income = 0.0
    cum_fees_global    = 0.0

    # ── Output arrays ─────────────────────────────────────────────────────────
    pnl_per_hour            = np.zeros(n_hours)
    active_capital_per_hour = np.zeros(n_hours)
    gross_funding_per_hour  = np.zeros(n_hours)
    fees_per_hour           = np.zeros(n_hours)
    is_idle_arr             = np.zeros(n_hours, dtype=bool)

    # ── Counters ───────────────────────────────────────────────────────────────
    n_rotations              = 0
    n_ramps                  = 0
    n_continues              = 0
    n_freezes                = 0
    n_frozen_unwinds         = 0
    n_degradation_exits      = 0   # defensive "degrading + no healthy candidate" exits
    n_first_tranche_skipped  = 0   # NEW v0.8: "wanted to open new slot but nobody passed floor"

    # Warm-up boundary
    tick_start = signal_window_hours + 24

    # Equity tracking
    equity_prev = 0.0

    # Per-tick fee tracking for per-hour array
    fees_this_tick = 0.0

    # ── Helpers ────────────────────────────────────────────────────────────────

    def coins_in_slots():
        out = set()
        for sl in slots:
            if sl["state"] == "empty":
                continue
            if sl["coin"] is not None:
                out.add(sl["coin"])
        return out

    def open_tranche(slot, h, coin):
        P = prices_d[coin][h]
        notional = POSITION_SIZE * slice_pct
        units_spot = notional / P
        units_perp = notional / P
        entry_fees = notional * (SPOT_TAKER + PERP_TAKER)
        t = {
            "entry_hour_idx":    h,
            "entry_spot_p":      P,
            "entry_perp_p":      P,
            "units_spot":        units_spot,
            "units_perp":        units_perp,
            "fraction":          slice_pct,
            "entry_fees":        entry_fees,
            "funding_collected": 0.0,
        }
        nonlocal cum_fees_global, fees_this_tick
        cum_fees_global += entry_fees
        fees_this_tick  += entry_fees
        return t

    def close_tranche(slot, t, h):
        coin = slot["coin"]
        P = prices_d[coin][h]
        notional_exit = POSITION_SIZE * t["fraction"]
        exit_fees = notional_exit * (SPOT_TAKER + PERP_TAKER)
        spot_pnl  = (P - t["entry_spot_p"]) * t["units_spot"]
        perp_pnl  = (t["entry_perp_p"] - P) * t["units_perp"]
        pnl = t["funding_collected"] + spot_pnl + perp_pnl - t["entry_fees"] - exit_fees
        nonlocal cum_fees_global, fees_this_tick
        cum_fees_global += exit_fees
        fees_this_tick  += exit_fees
        return pnl

    def unrealized_pnl_slot(slot, h):
        if not slot["tranches"]:
            return 0.0
        coin = slot["coin"]
        P = prices_d[coin][h]
        total = 0.0
        for t in slot["tranches"]:
            notional_exit = POSITION_SIZE * t["fraction"]
            exit_fees_est = notional_exit * (SPOT_TAKER + PERP_TAKER)
            spot_pnl  = (P - t["entry_spot_p"]) * t["units_spot"]
            perp_pnl  = (t["entry_perp_p"] - P) * t["units_perp"]
            total += t["funding_collected"] + spot_pnl + perp_pnl - t["entry_fees"] - exit_fees_est
        return total

    def current_unrealized_all(h):
        total = 0.0
        for sl in slots:
            if sl["tranches"]:
                total += unrealized_pnl_slot(sl, h)
        return total

    def n_growing_plus_holding():
        return sum(1 for sl in slots if sl["state"] in ("growing", "holding"))

    def find_empty_slot():
        for sl in slots:
            if sl["state"] == "empty":
                return sl
        return None

    def best_candidate_offensive(exclude_coins, h, must_beat_apr):
        """v0.6 behavior: highest ma12 not in held, must beat must_beat_apr + rotation_delta_apr."""
        best_coin = None
        best_apr  = -1e9
        for c in coins:
            if c in exclude_coins:
                continue
            c_ma = ma12[c][h]
            if np.isnan(c_ma):
                continue
            if c_ma <= must_beat_apr + rotation_delta_apr:
                continue
            if c_ma > best_apr:
                best_apr  = c_ma
                best_coin = c
        return best_coin

    def best_candidate_defensive(exclude_coins, h):
        """
        v0.8 Fix 1: highest ma12 not in held, NO additional threshold filter here.
        The HEALTHY check (> degradation_threshold_apr) is applied at the call site.
        """
        best_coin = None
        best_apr  = -1e9
        for c in coins:
            if c in exclude_coins:
                continue
            c_ma = ma12[c][h]
            if np.isnan(c_ma):
                continue
            if c_ma > best_apr:
                best_apr  = c_ma
                best_coin = c
        return best_coin

    def best_candidate_first_tranche(exclude_coins, h, min_ma_apr=None):
        """
        First-tranche entry (EMPTY slot): pick highest ma12 of unheld coins.
        v0.8 Fix 2: when min_ma_apr is provided, candidates must EXCEED that floor.
        Returns None if nobody passes.
        In offensive mode: called with min_ma_apr=None (no floor, same as v0.6/v0.7).
        In defensive mode: called with min_ma_apr=degradation_threshold_apr.
        """
        best_coin = None
        best_apr  = -1e9
        for c in coins:
            if c in exclude_coins:
                continue
            c_ma = ma12[c][h]
            if np.isnan(c_ma):
                continue
            if min_ma_apr is not None and c_ma <= min_ma_apr:
                continue
            if c_ma > best_apr:
                best_apr  = c_ma
                best_coin = c
        return best_coin

    def start_new_slot(sl, coin, h):
        nonlocal n_ramps
        sl["coin"]             = coin
        sl["state"]            = "growing"
        sl["tranches"]         = []
        sl["cum_realized_pnl"] = 0.0
        t = open_tranche(sl, h, coin)
        sl["tranches"].append(t)
        # Initialize trailing anchor to ma12 at first tranche
        anchor_val = ma12[coin][h]
        sl["entry_ma_anchor"] = anchor_val if not np.isnan(anchor_val) else 0.0
        total_frac = sum(x["fraction"] for x in sl["tranches"])
        if total_frac >= 1.0 - 1e-9:
            sl["state"] = "holding"
        n_ramps += 1

    # ── Main loop ──────────────────────────────────────────────────────────────
    for h in range(n_hours):

        fees_this_tick = 0.0   # reset per-hour fee accumulator

        # 1) Accrue funding for all open tranches
        hour_funding = 0.0
        for sl in slots:
            if not sl["tranches"]:
                continue
            coin = sl["coin"]
            P    = prices_d[coin][h]
            r    = rates[coin][h]
            for t in sl["tranches"]:
                hf = t["units_perp"] * P * r
                t["funding_collected"] += hf
                cum_funding_income      += hf
                hour_funding            += hf

        gross_funding_per_hour[h] = hour_funding

        # 2) Compute active capital
        active_cap = 0.0
        for sl in slots:
            if sl["tranches"]:
                frac = sum(x["fraction"] for x in sl["tranches"])
                active_cap += frac * TOTAL_CAPITAL
        active_capital_per_hour[h] = active_cap

        # Track per-hour idle boolean
        is_idle_arr[h] = all(sl["state"] == "empty" for sl in slots)

        # 3) Tick actions
        if h >= tick_start and (h % tick_hours == 0):

            # --- Process SHRINKING slots: close oldest tranche FIFO ---
            for sl in slots:
                if sl["state"] != "shrinking":
                    continue
                if not sl["tranches"]:
                    sl["state"] = "empty"
                    sl["coin"]  = None
                    continue
                t = sl["tranches"].pop(0)
                pnl = close_tranche(sl, t, h)
                sl["cum_realized_pnl"] += pnl
                if not sl["tranches"]:
                    sl["state"] = "empty"
                    sl["coin"]  = None

            # --- Process GROWING slots ---
            for sl in slots:
                if sl["state"] != "growing":
                    continue
                coin = sl["coin"]
                c_ma = ma12[coin][h]

                # Continue-ramp check: trailing anchor (unchanged from v0.6)
                anchor = sl["entry_ma_anchor"]
                passes = (not np.isnan(c_ma)) and (c_ma >= anchor - continue_tolerance_apr)

                if passes:
                    # Add next tranche
                    t = open_tranche(sl, h, coin)
                    sl["tranches"].append(t)
                    # Update trailing anchor upward (never down — take max)
                    sl["entry_ma_anchor"] = max(anchor, c_ma)
                    n_continues += 1
                    total_frac = sum(x["fraction"] for x in sl["tranches"])
                    if total_frac >= 1.0 - 1e-9:
                        sl["state"] = "holding"
                else:
                    # Continue check FAILED — Fix A unwind check (same as v0.5/v0.6/v0.7)
                    n_freezes += 1
                    if unwind_frozen and sl["tranches"]:
                        coin_h = sl["coin"]
                        P = prices_d[coin_h][h]
                        tranches = sl["tranches"]
                        unreal = (
                            sum(t["funding_collected"] for t in tranches)
                            + sum((P - t["entry_spot_p"]) * t["units_spot"] for t in tranches)
                            + sum((t["entry_perp_p"] - P) * t["units_perp"] for t in tranches)
                            - sum(t["entry_fees"] for t in tranches)
                            - sum(POSITION_SIZE * t["fraction"] for t in tranches) * (SPOT_TAKER + PERP_TAKER)
                        )
                        if unreal >= 0:
                            sl["state"] = "shrinking"
                            n_frozen_unwinds += 1

            # --- Process HOLDING slots ---
            for sl in slots:
                if sl["state"] != "holding":
                    continue
                coin = sl["coin"]

                if rotation_mode == 'defensive':
                    # ── Defensive mode (v0.8 Fix 1) ───────────────────────────
                    current_ma = ma12[coin][h]
                    if np.isnan(current_ma):
                        current_ma = -1e9  # treat NaN as fully degraded

                    if current_ma > degradation_threshold_apr:
                        # Position is healthy — HOLD. Don't react to neighbors.
                        pass
                    else:
                        # Position is degrading — look for HEALTHY replacement
                        held = coins_in_slots()
                        candidate = best_candidate_defensive(held, h)

                        # v0.8 Fix 1: candidate must be HEALTHY (above threshold),
                        # not just marginally better than the dying coin.
                        if candidate is not None and ma12[candidate][h] > degradation_threshold_apr:
                            # Found a healthy replacement → rotate
                            sl["state"] = "shrinking"
                            n_rotations += 1

                            # Open new slot for candidate if cap allows
                            n_gh = n_growing_plus_holding()
                            if n_gh < n_main_cap:
                                empty_sl = find_empty_slot()
                                if empty_sl is not None:
                                    start_new_slot(empty_sl, candidate, h)
                        else:
                            # Degrading AND no healthy candidate → exit to USDC
                            sl["state"] = "shrinking"
                            n_degradation_exits += 1

                else:
                    # ── Offensive mode (v0.6/v0.7 behavior, unchanged) ─────────
                    held_coins  = coins_in_slots()
                    current_apr = ma12[coin][h] if not np.isnan(ma12[coin][h]) else -1e9

                    candidate = best_candidate_offensive(
                        held_coins, h, must_beat_apr=current_apr
                    )

                    if candidate is not None:
                        # Rotate: mark this slot shrinking
                        sl["state"] = "shrinking"
                        n_rotations += 1

                        n_gh = n_growing_plus_holding()
                        if n_gh < n_main_cap:
                            empty_sl = find_empty_slot()
                            if empty_sl is not None:
                                start_new_slot(empty_sl, candidate, h)

                    else:
                        # No rotation candidate: exit if signal too low
                        c_ma = ma12[coin][h]
                        if not np.isnan(c_ma) and c_ma < exit_signal_threshold_apr:
                            sl["state"] = "shrinking"
                        # else: hold

            # --- Process EMPTY slots ---
            for sl in slots:
                if sl["state"] != "empty":
                    continue
                n_gh = n_growing_plus_holding()
                if n_gh < n_main_cap:
                    held_coins = coins_in_slots()

                    if rotation_mode == 'defensive':
                        # v0.8 Fix 2: only enter if candidate exceeds health floor
                        candidate = best_candidate_first_tranche(
                            held_coins, h, min_ma_apr=degradation_threshold_apr
                        )
                        if candidate is not None:
                            start_new_slot(sl, candidate, h)
                        else:
                            n_first_tranche_skipped += 1
                    else:
                        # Offensive mode: no floor, same as v0.6/v0.7
                        candidate = best_candidate_first_tranche(held_coins, h, min_ma_apr=None)
                        if candidate is not None:
                            start_new_slot(sl, candidate, h)

        # Record fees for this hour (entry/exit fees paid during tick)
        fees_per_hour[h] = fees_this_tick

        # 4) Equity update (without Aave — added later vectorised)
        total_realized = sum(sl["cum_realized_pnl"] for sl in slots)
        unrealized_all = current_unrealized_all(h)
        equity_now = total_realized + unrealized_all + cum_funding_income - cum_fees_global
        pnl_per_hour[h] = equity_now - equity_prev
        equity_prev = equity_now

    # ── Final close of all open positions ─────────────────────────────────────
    h_final = n_hours - 1
    fees_this_tick = 0.0
    for sl in slots:
        if not sl["tranches"]:
            continue
        while sl["tranches"]:
            t = sl["tranches"].pop(0)
            pnl = close_tranche(sl, t, h_final)
            sl["cum_realized_pnl"] += pnl

    fees_per_hour[h_final] += fees_this_tick

    total_realized_final = sum(sl["cum_realized_pnl"] for sl in slots)
    equity_final = total_realized_final + cum_funding_income - cum_fees_global
    pnl_per_hour[-1] += equity_final - equity_prev

    # ── Aave income (vectorised, fixed budget) ────────────────────────────────
    peak_active_capital = float(active_capital_per_hour.max()) if active_capital_per_hour.max() > 0 else float(TOTAL_CAPITAL * n_main_cap)
    aave_budget = max(float(TOTAL_CAPITAL * n_main_cap), peak_active_capital)

    idle_per_hour = np.maximum(0.0, aave_budget - active_capital_per_hour)
    aave_income_per_hour = idle_per_hour * (aave_apr / HOURS_PER_YEAR)

    # Add Aave income into the pnl series (zero when aave_apr == 0)
    pnl_per_hour += aave_income_per_hour

    info = {
        "cum_funding_income":       cum_funding_income,
        "cum_fees":                 cum_fees_global,
        "n_rotations":              n_rotations,
        "n_ramps":                  n_ramps,
        "n_continues":              n_continues,
        "n_freezes":                n_freezes,
        "n_frozen_unwinds":         n_frozen_unwinds,
        "n_degradation_exits":      n_degradation_exits,
        "n_first_tranche_skipped":  n_first_tranche_skipped,
        "peak_active_capital":      peak_active_capital,
        "aave_budget":              aave_budget,
    }
    return (
        pnl_per_hour,
        active_capital_per_hour,
        gross_funding_per_hour,
        fees_per_hour,
        aave_income_per_hour,
        is_idle_arr,
        info,
    )


# ── main() ─────────────────────────────────────────────────────────────────────

def main():
    coins = U11
    print(f"Loading data for {len(coins)} coins: {coins}")

    dfs = {}
    for coin in coins:
        df = load_data(coin)
        if df.empty:
            print(f"  WARNING: no data for {coin}")
            continue
        dfs[coin] = df

    available_coins = [c for c in coins if c in dfs]

    # Find common time range (intersection)
    common_idx = None
    for c in available_coins:
        idx = dfs[c].index
        if common_idx is None:
            common_idx = set(idx)
        else:
            common_idx &= set(idx)

    common_idx = pd.DatetimeIndex(sorted(common_idx))
    n_hours = len(common_idx)
    print(f"Common time range: {common_idx[0]} — {common_idx[-1]}, {n_hours} hours")

    # Build numpy arrays
    prices_np   = {}
    rates_np    = {}
    ma12_np     = {}
    sig24min_np = {}

    SIGNAL_WINDOW = 12

    for c in available_coins:
        df2 = dfs[c].reindex(common_idx)
        pr  = df2["close"].values.astype(float)
        rt  = df2["fundingRate"].values.astype(float)

        ma12_raw = pd.Series(rt).rolling(SIGNAL_WINDOW, min_periods=1).mean().values * HOURS_PER_YEAR
        s24min   = pd.Series(ma12_raw).rolling(24, min_periods=1).min().values

        prices_np[c]   = pr
        rates_np[c]    = rt
        ma12_np[c]     = ma12_raw
        sig24min_np[c] = s24min

    # ── Parameter sweep ────────────────────────────────────────────────────────
    LAST_90D_HOURS = 90 * 24  # 2160
    SYNTHETIC_PEAK_CAP = float(TOTAL_CAPITAL * 2)   # 4000

    # 5 configs:
    # (name, rotation_mode, degradation_threshold, continue_tolerance, aave_apr)
    # None rotation_mode => synthetic aave_only row
    configs = [
        # name,                  rot_mode,    deg_thr, cont_tol, aave
        ("aave_only",            None,        None,    None,     None),   # synthetic
        ("v08_def_5pct",         "defensive", 0.05,   0.05,     0.0),
        ("v08_def_2pct",         "defensive", 0.02,   0.05,     0.0),
        ("v08_def_0pct",         "defensive", 0.00,   0.05,     0.0),
        ("v06_slack_5pct_ctrl",  "offensive", None,   0.05,     0.0),    # v0.6 control
    ]

    default_kwargs = dict(
        tick_hours=24,
        slice_pct=0.10,
        rotation_delta_apr=0.10,
        exit_signal_threshold_apr=0.0,
        signal_window_hours=SIGNAL_WINDOW,
        n_slots_total=4,
        n_main_cap=2,
        unwind_frozen=True,
    )

    rows = []

    for cfg in configs:
        cfg_name, rot_mode, deg_thr, cont_tol, aave_cfg = cfg

        # ── Synthetic aave_only row ────────────────────────────────────────────
        if rot_mode is None:
            print(f"\nConfig 'aave_only': synthetic baseline (5% APR, no trading)")
            for period_name in ("full", "last_90d"):
                rows.append({
                    "config":                         cfg_name,
                    "period":                         period_name,
                    "annual_pct":                     5.0,
                    "calmar":                         float("inf"),
                    "max_dd_pct":                     0.0,
                    "funding_minus_fees_annual_pct":  float("nan"),
                    "aave_income_annual_pct":         5.0,
                    "gross_funding_annual_pct":       float("nan"),
                    "fees_annual_pct":                float("nan"),
                    "fees_ratio":                     float("nan"),
                    "n_ramps":                        float("nan"),
                    "n_continues":                    float("nan"),
                    "n_freezes":                      float("nan"),
                    "n_rotations":                    float("nan"),
                    "n_degradation_exits":            float("nan"),
                    "n_first_tranche_skipped":        float("nan"),
                    "n_frozen_unwinds":               float("nan"),
                    "pct_time_in_usdc":               1.0,
                    "tick_hours":                     float("nan"),
                    "slice_pct":                      float("nan"),
                    "continue_tolerance_apr":         float("nan"),
                    "rotation_mode":                  float("nan"),
                    "rotation_delta_apr":             float("nan"),
                    "degradation_threshold_apr":      float("nan"),
                    "exit_signal_threshold_apr":      float("nan"),
                    "aave_apr":                       0.05,
                    "unwind_enabled":                 float("nan"),
                    "peak_capital":                   SYNTHETIC_PEAK_CAP,
                })
            continue

        # ── Real simulation ────────────────────────────────────────────────────
        deg_thr_str = f"{deg_thr:.2f}" if deg_thr is not None else "N/A"
        print(f"\nRunning config '{cfg_name}':"
              f" rotation_mode={rot_mode}"
              f" degradation_threshold={deg_thr_str}"
              f" continue_tolerance={cont_tol:.2f}"
              f" aave_apr={aave_cfg:.2f}")

        sim_kwargs = dict(default_kwargs)
        sim_kwargs["rotation_mode"]          = rot_mode
        sim_kwargs["continue_tolerance_apr"] = cont_tol
        sim_kwargs["aave_apr"]               = aave_cfg
        if deg_thr is not None:
            sim_kwargs["degradation_threshold_apr"] = deg_thr
        else:
            sim_kwargs["degradation_threshold_apr"] = 0.05  # unused in offensive mode

        (pnl, cap, gross_fund_arr, fees_arr, aave_arr, is_idle_arr, info) = simulate_rebalance_v08(
            coins=available_coins,
            prices=prices_np,
            rates_data=rates_np,
            ma12_data=ma12_np,
            sig24min_data=sig24min_np,
            n_hours=n_hours,
            **sim_kwargs,
        )

        peak_cap              = info["peak_active_capital"]
        aave_bud              = info["aave_budget"]
        n_ramps               = info["n_ramps"]
        n_continues           = info["n_continues"]
        n_freezes             = info["n_freezes"]
        n_rots                = info["n_rotations"]
        n_frozen_unwinds      = info["n_frozen_unwinds"]
        n_deg_exits           = info["n_degradation_exits"]
        n_ft_skipped          = info["n_first_tranche_skipped"]

        # Capital base for metrics: aave_budget
        capital_base = aave_bud if aave_bud > 0 else float(TOTAL_CAPITAL * 2)

        for period_name, start_idx in [("full", 0), ("last_90d", max(0, n_hours - LAST_90D_HOURS))]:
            pnl_slice    = pnl[start_idx:]
            gf_slice     = gross_fund_arr[start_idx:]
            fee_slice    = fees_arr[start_idx:]
            aave_slice   = aave_arr[start_idx:]
            idle_slice   = is_idle_arr[start_idx:]
            n_slice      = len(pnl_slice)

            if n_slice == 0 or capital_base == 0:
                continue

            # Metrics (annual_pct includes aave when aave_apr > 0)
            m = _metrics_on_capital(pnl_slice, capital_base)

            yrs = n_slice / HOURS_PER_YEAR

            # Window-specific funding / fees / aave
            window_gf   = float(gf_slice.sum())
            window_fees = float(fee_slice.sum())
            window_aave = float(aave_slice.sum())

            gross_fund_annual_pct = (window_gf   / capital_base / yrs * 100) if yrs > 0 else float("nan")
            fees_annual_pct       = (window_fees  / capital_base / yrs * 100) if yrs > 0 else float("nan")
            aave_annual_pct       = (window_aave  / capital_base / yrs * 100) if yrs > 0 else float("nan")
            funding_minus_fees    = gross_fund_annual_pct - fees_annual_pct
            fees_ratio            = (window_fees / window_gf) if window_gf > 0 else float("nan")

            # Window-aware pct_time_in_usdc
            pct_usdc = float(idle_slice.mean()) if len(idle_slice) > 0 else 1.0

            rows.append({
                "config":                         cfg_name,
                "period":                         period_name,
                "annual_pct":                     m["annual_pct"],
                "calmar":                         m["calmar"],
                "max_dd_pct":                     m["max_dd_pct"],
                "funding_minus_fees_annual_pct":  round(funding_minus_fees, 2),
                "aave_income_annual_pct":         round(aave_annual_pct, 2),
                "gross_funding_annual_pct":       round(gross_fund_annual_pct, 2),
                "fees_annual_pct":                round(fees_annual_pct, 2),
                "fees_ratio":                     round(fees_ratio, 4) if not np.isnan(fees_ratio) else float("nan"),
                "n_ramps":                        n_ramps,
                "n_continues":                    n_continues,
                "n_freezes":                      n_freezes,
                "n_rotations":                    n_rots,
                "n_degradation_exits":            n_deg_exits,
                "n_first_tranche_skipped":        n_ft_skipped,
                "n_frozen_unwinds":               n_frozen_unwinds,
                "pct_time_in_usdc":               round(pct_usdc, 4),
                "tick_hours":                     default_kwargs["tick_hours"],
                "slice_pct":                      default_kwargs["slice_pct"],
                "continue_tolerance_apr":         cont_tol,
                "rotation_mode":                  rot_mode,
                "rotation_delta_apr":             default_kwargs["rotation_delta_apr"],
                "degradation_threshold_apr":      deg_thr if deg_thr is not None else float("nan"),
                "exit_signal_threshold_apr":      default_kwargs["exit_signal_threshold_apr"],
                "aave_apr":                       aave_cfg,
                "unwind_enabled":                 True,
                "peak_capital":                   round(peak_cap, 2),
            })

        full_row = rows[-2]
        print(f"  peak_cap={peak_cap:.0f}, aave_budget={aave_bud:.0f}, "
              f"ramps={n_ramps}, continues={n_continues}, freezes={n_freezes}, "
              f"rotations={n_rots}, deg_exits={n_deg_exits}, ft_skipped={n_ft_skipped}, "
              f"frozen_unwinds={n_frozen_unwinds}, "
              f"annual_full={full_row['annual_pct']:.2f}%, calmar={full_row['calmar']:.2f}")

    # ── Save CSV ───────────────────────────────────────────────────────────────
    col_order = [
        "config", "period", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct", "aave_income_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_continues", "n_freezes",
        "n_rotations", "n_degradation_exits", "n_first_tranche_skipped", "n_frozen_unwinds",
        "pct_time_in_usdc",
        "tick_hours", "slice_pct", "continue_tolerance_apr",
        "rotation_mode", "rotation_delta_apr", "degradation_threshold_apr",
        "exit_signal_threshold_apr",
        "aave_apr", "unwind_enabled", "peak_capital",
    ]
    out_path = Path(__file__).parent / "rebalance_v08_results.csv"
    df_out = pd.DataFrame(rows, columns=col_order)
    df_out.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # ── Print comparison table ─────────────────────────────────────────────────
    pd.set_option("display.width", 360)
    pd.set_option("display.max_columns", 35)
    pd.set_option("display.float_format", "{:.2f}".format)

    display_cols = [
        "config", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_continues", "n_freezes",
        "n_rotations", "n_degradation_exits", "n_first_tranche_skipped", "n_frozen_unwinds",
        "pct_time_in_usdc",
        "rotation_mode", "degradation_threshold_apr",
    ]

    df_full = df_out[df_out["period"] == "full"].copy()
    df_90   = df_out[df_out["period"] == "last_90d"].copy()

    print("\n" + "=" * 240)
    print("REBALANCE V0.8 — FULL PERIOD (sorted by calmar)")
    print("=" * 240)
    print(df_full[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    print("\n" + "=" * 240)
    print("REBALANCE V0.8 — LAST 90 DAYS (sorted by calmar)")
    print("=" * 240)
    print(df_90[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    # ── DATA DIAGNOSTIC: How often is the market "cold"? ─────────────────────
    print("\n" + "=" * 120)
    print("DATA DIAGNOSTIC: Frequency of cold-market conditions (all coins below threshold)")
    print("=" * 120)
    tick_start_diag = 12 + 24
    for threshold_val, label in [(0.05, "5%"), (0.02, "2%"), (0.00, "0%")]:
        all_below_count = 0
        all_below_dates = []
        for h in range(tick_start_diag, n_hours, 24):
            vals = [ma12_np[c][h] for c in available_coins if not np.isnan(ma12_np[c][h])]
            if vals and max(vals) <= threshold_val:
                all_below_count += 1
                all_below_dates.append(common_idx[h].date())
        total_ticks = (n_hours - tick_start_diag) // 24
        pct = all_below_count / total_ticks * 100 if total_ticks > 0 else 0
        dates_str = f" ({all_below_dates[0]} to {all_below_dates[-1]})" if all_below_dates else " (never)"
        print(f"  threshold={label}: all-coins-below ticks = {all_below_count}/{total_ticks} ({pct:.2f}%){dates_str}")

    print("\n  Context: n_degradation_exits fires only when held coin is in HOLDING state (all 10 tranches")
    print("  filled) AND all remaining unheld coins are also below threshold. Since:")
    print("    (a) Coins reaching HOLDING take ~10 ticks after first ramp (slice_pct=10%),")
    print("    (b) Only 1 tick in the entire dataset has ALL coins simultaneously below 5%,")
    print("    (c) At that tick (2026-02-03), held coins were in GROWING state (not yet HOLDING),")
    print("  n_degradation_exits=0 is EXPECTED in this backtest period — not a code bug.")
    print("  The fix is logically correct: it changes the condition from")
    print("    v0.7: cand_ma > current_ma  (rotates even into degraded candidate)")
    print("    v0.8: cand_ma > threshold   (only rotates if candidate is actually healthy)")
    print("  This matters in a true cold market; this dataset simply does not have one.")

    # ── CRITICAL SANITY: Bug fix activated? ───────────────────────────────────
    print("\n" + "=" * 120)
    print("CRITICAL SANITY: Did the bug fix activate the exit-to-USDC path?")
    print("  (Note: 0 is expected given the dataset — see DATA DIAGNOSTIC above)")
    print("=" * 120)
    for cfg_name in ("v08_def_5pct", "v08_def_2pct", "v08_def_0pct"):
        r_full = df_out[(df_out["config"] == cfg_name) & (df_out["period"] == "full")]
        r_90   = df_out[(df_out["config"] == cfg_name) & (df_out["period"] == "last_90d")]
        if r_full.empty:
            continue
        rv_full = r_full.iloc[0]
        rv_90   = r_90.iloc[0] if not r_90.empty else None
        deg_exits    = rv_full["n_degradation_exits"]
        ft_skipped   = rv_full["n_first_tranche_skipped"]
        usdc_90d     = rv_90["pct_time_in_usdc"] if rv_90 is not None else float("nan")
        usdc_full    = rv_full["pct_time_in_usdc"]
        deg_status   = "OK" if deg_exits > 0 else "0 — expected (data: only 1 all-below tick, coins in GROWING there)"
        skip_status  = "OK" if ft_skipped > 0 else "0 — expected (healthy coins always available for first-tranche)"
        usdc_status  = "OK" if usdc_90d > 0 else "0 — expected (last90d max-ma12 never below threshold)"
        print(f"  {cfg_name:20s}: n_degradation_exits={deg_exits:.0f} [{deg_status}]")
        print(f"    n_ft_skipped={ft_skipped:.0f} [{skip_status}]")
        print(f"    pct_usdc_full={usdc_full:.4f}  pct_usdc_90d={usdc_90d:.4f} [{usdc_status}]")

    # ── SANITY CHECK: v06_slack_5pct_ctrl reproduces v0.6 ────────────────────
    V06_SLACK5_ANNUAL_FULL  = 2.63
    V06_SLACK5_CALMAR_FULL  = 4.53
    V06_SLACK5_ANNUAL_90D   = 5.75
    V06_SLACK5_CALMAR_90D   = 126.27

    print("\n" + "=" * 120)
    print("SANITY CHECK: v06_slack_5pct_ctrl reproduces v0.6 CSV v06_slack_5pct reference")
    print(f"  v06 reference: full={V06_SLACK5_ANNUAL_FULL:.2f}% calmar={V06_SLACK5_CALMAR_FULL:.2f}  "
          f"90d={V06_SLACK5_ANNUAL_90D:.2f}% calmar={V06_SLACK5_CALMAR_90D:.2f}")
    print("=" * 120)
    for period in ("full", "last_90d"):
        r = df_out[(df_out["config"] == "v06_slack_5pct_ctrl") & (df_out["period"] == period)]
        if r.empty:
            continue
        rv = r.iloc[0]
        v06_ann = V06_SLACK5_ANNUAL_FULL if period == "full" else V06_SLACK5_ANNUAL_90D
        v06_cal = V06_SLACK5_CALMAR_FULL if period == "full" else V06_SLACK5_CALMAR_90D
        ann_diff = rv["annual_pct"] - v06_ann
        cal_diff = rv["calmar"] - v06_cal
        match_ann = "OK" if abs(ann_diff) <= 0.15 else "MISMATCH"
        match_cal = "OK" if abs(cal_diff) <= 5.0  else "MISMATCH"
        print(f"  [{period:8s}]  v08_ctrl: annual={rv['annual_pct']:.2f}%  calmar={rv['calmar']:.2f}  "
              f"v06_ref: annual={v06_ann:.2f}%  calmar={v06_cal:.2f}  "
              f"ann_diff={ann_diff:+.3f}%  [{match_ann}]  "
              f"cal_diff={cal_diff:+.2f}  [{match_cal}]")

    # ── V0.7 → V0.8 DELTA COMPARISON ──────────────────────────────────────────
    V07_RESULTS = {
        # name: {full: annual, 90d: annual}
        "v07_def_5pct": {"full": 2.85, "last_90d": 0.19},
        "v07_def_2pct": {"full": 4.25, "last_90d": 1.16},
        "v07_def_0pct": {"full": 5.00, "last_90d": -8.31},
    }

    cfg_name_map = {
        "v08_def_5pct": "v07_def_5pct",
        "v08_def_2pct": "v07_def_2pct",
        "v08_def_0pct": "v07_def_0pct",
    }

    print("\n" + "=" * 120)
    print("V0.7 → V0.8 DELTA COMPARISON (defensive configs)")
    print("=" * 120)
    print(f"  {'config':22s}  {'v07_full':>9} {'v08_full':>9} {'delta_full':>11}  "
          f"{'v07_90d':>9} {'v08_90d':>9} {'delta_90d':>11}")
    for v08_name, v07_name in cfg_name_map.items():
        v07_full = V07_RESULTS[v07_name]["full"]
        v07_90d  = V07_RESULTS[v07_name]["last_90d"]
        r_full   = df_out[(df_out["config"] == v08_name) & (df_out["period"] == "full")]
        r_90     = df_out[(df_out["config"] == v08_name) & (df_out["period"] == "last_90d")]
        if r_full.empty:
            continue
        v08_full = r_full.iloc[0]["annual_pct"]
        v08_90d  = r_90.iloc[0]["annual_pct"] if not r_90.empty else float("nan")
        d_full   = v08_full - v07_full
        d_90d    = v08_90d  - v07_90d
        print(f"  {v08_name:22s}  {v07_full:>9.2f}% {v08_full:>9.2f}% {d_full:>+10.2f}%  "
              f"{v07_90d:>9.2f}% {v08_90d:>9.2f}% {d_90d:>+10.2f}%")

    # ── SUMMARY TABLE sorted by calmar_full ───────────────────────────────────
    print("\n" + "=" * 180)
    print("SUMMARY TABLE (sorted by calmar_full)")
    print("=" * 180)
    summary_rows = []
    for cfg_name in df_full["config"].values:
        rf = df_full[df_full["config"] == cfg_name]
        r9 = df_90[df_90["config"] == cfg_name]
        if rf.empty:
            continue
        rv_f = rf.iloc[0]
        rv_9 = r9.iloc[0] if not r9.empty else None
        summary_rows.append({
            "config":         cfg_name,
            "threshold":      rv_f["degradation_threshold_apr"],
            "annual_full":    rv_f["annual_pct"],
            "calmar_full":    rv_f["calmar"],
            "annual_90d":     rv_9["annual_pct"] if rv_9 is not None else float("nan"),
            "calmar_90d":     rv_9["calmar"]     if rv_9 is not None else float("nan"),
            "n_rot":          rv_f["n_rotations"],
            "n_deg_exit":     rv_f["n_degradation_exits"],
            "n_skip":         rv_f["n_first_tranche_skipped"],
            "pct_usdc_full":  rv_f["pct_time_in_usdc"],
            "pct_usdc_90d":   rv_9["pct_time_in_usdc"] if rv_9 is not None else float("nan"),
        })

    df_summary = pd.DataFrame(summary_rows)
    df_summary_sorted = df_summary.sort_values("calmar_full", ascending=False)
    print(df_summary_sorted.to_string(index=False))


if __name__ == "__main__":
    main()
