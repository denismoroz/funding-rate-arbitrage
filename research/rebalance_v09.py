"""
rebalance_v09.py — Scale-in/scale-out v0.9: no position limit, 1h tick, continue-mode sweep.

Three structural changes vs v0.8:
  Change 1: Remove position limit — n_slots_total=11, n_main_cap=11 (all 11 U11 coins can be
             active simultaneously). Peak capital up to 11 × $2000 = $22,000.
  Change 2: 1h tick (was 24h). Ramp/exit decisions happen every hour.
  Change 3: continue_mode parameter sweep over 4 variants:
             'trailing' — anchor updates upward after each continue (v0.6/v0.7/v0.8 behavior)
             'fixed'    — anchor set at first tranche, never changes
             'decay'    — anchor decays by anchor_decay_per_tick each tick, still ratchets up
             'none'     — no anchor check; always continue if funding > 0

Sweep: 6 configs × 2 periods = 12 rows.

Run: uv run python research/rebalance_v09.py
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

def simulate_rebalance_v09(
    coins: list,
    prices: dict,          # coin -> np.ndarray (float64), aligned
    rates_data: dict,      # coin -> np.ndarray (float64), aligned
    ma12_data: dict,       # coin -> np.ndarray, pre-computed MA12 APR
    sig24min_data: dict,   # coin -> np.ndarray, pre-computed 24h min of MA12 (kept for compat)
    n_hours: int,
    *,
    tick_hours: int = 1,                   # was 24; 1h tick for faster reactivity
    slice_pct: float = 0.10,
    continue_mode: str = 'trailing',       # 'trailing' | 'fixed' | 'decay' | 'none'
    continue_tolerance_apr: float = 0.05,
    anchor_decay_per_tick: float = 0.001,  # only used when continue_mode='decay'
    rotation_mode: str = 'offensive',      # 'offensive' | 'defensive'
    rotation_delta_apr: float = 0.10,
    degradation_threshold_apr: float = 0.05,
    exit_signal_threshold_apr: float = 0.0,
    signal_window_hours: int = 12,
    n_slots_total: int = 11,               # was 4; one per U11 coin
    n_main_cap: int = 11,                  # was 2; no cap
    aave_apr: float = 0.0,
    unwind_frozen: bool = True,
) -> tuple:
    """
    Core per-hour simulation loop for v0.9.

    Changes vs v0.8:
      - tick_hours=1 (default): react every hour
      - n_slots_total=11, n_main_cap=11: all 11 coins can be active simultaneously
      - continue_mode controls the anchor logic for the continue-ramp check:
          'trailing': anchor = max(anchor, current) after each continue (v0.6/v0.7/v0.8)
          'fixed':    anchor set at first tranche, never updated
          'decay':    anchor decays by anchor_decay_per_tick each tick; ratchets up on continue
          'none':     no anchor check; continue as long as funding > 0

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
            "entry_ma_anchor":   0.0,       # anchor for continue-ramp
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
    n_degradation_exits      = 0
    n_first_tranche_skipped  = 0

    # Warm-up boundary (same as v0.8)
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
        """Highest ma12 not in held, must beat must_beat_apr + rotation_delta_apr."""
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
        """Highest ma12 not in held (HEALTHY check applied at call site)."""
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
        In offensive mode: called with min_ma_apr=None (no floor).
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
        # Initialize anchor to ma12 at first tranche
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

                # Apply anchor decay BEFORE checking continue condition (decay mode only)
                if continue_mode == 'decay':
                    sl["entry_ma_anchor"] = max(0.0, sl["entry_ma_anchor"] - anchor_decay_per_tick)

                anchor = sl["entry_ma_anchor"]

                # Determine if this slot passes the continue check
                if continue_mode == 'none':
                    passes = (not np.isnan(c_ma)) and (c_ma > 0.0)
                else:
                    passes = (not np.isnan(c_ma)) and (c_ma >= anchor - continue_tolerance_apr)

                if passes:
                    # Add next tranche
                    t = open_tranche(sl, h, coin)
                    sl["tranches"].append(t)
                    # Update anchor based on continue_mode
                    if continue_mode in ('trailing', 'decay'):
                        sl["entry_ma_anchor"] = max(anchor, c_ma)
                    # 'fixed': don't touch anchor
                    # 'none': anchor irrelevant
                    n_continues += 1
                    total_frac = sum(x["fraction"] for x in sl["tranches"])
                    if total_frac >= 1.0 - 1e-9:
                        sl["state"] = "holding"
                else:
                    # Continue check FAILED — unwind check (same as v0.5/v0.6/v0.7/v0.8)
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
                    # Defensive mode
                    current_ma = ma12[coin][h]
                    if np.isnan(current_ma):
                        current_ma = -1e9

                    if current_ma > degradation_threshold_apr:
                        # Healthy — hold
                        pass
                    else:
                        # Degrading — look for healthy replacement
                        held = coins_in_slots()
                        candidate = best_candidate_defensive(held, h)

                        if candidate is not None and ma12[candidate][h] > degradation_threshold_apr:
                            sl["state"] = "shrinking"
                            n_rotations += 1
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
                    # Offensive mode (default for v0.9)
                    held_coins  = coins_in_slots()
                    current_apr = ma12[coin][h] if not np.isnan(ma12[coin][h]) else -1e9

                    candidate = best_candidate_offensive(
                        held_coins, h, must_beat_apr=current_apr
                    )

                    if candidate is not None:
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

            # --- Process EMPTY slots ---
            for sl in slots:
                if sl["state"] != "empty":
                    continue
                n_gh = n_growing_plus_holding()
                if n_gh < n_main_cap:
                    held_coins = coins_in_slots()

                    if rotation_mode == 'defensive':
                        candidate = best_candidate_first_tranche(
                            held_coins, h, min_ma_apr=degradation_threshold_apr
                        )
                        if candidate is not None:
                            start_new_slot(sl, candidate, h)
                        else:
                            n_first_tranche_skipped += 1
                    else:
                        # Offensive mode: no floor (same as v0.6/v0.7)
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
    LAST_90D_HOURS   = 90 * 24          # 2160
    SYNTHETIC_PEAK_CAP = float(TOTAL_CAPITAL * 2)   # 4000 — aave_only synthetic

    # 6 configs:
    # (name, tick_hours, n_slots, n_main, continue_mode, cont_tol, decay, rotation_mode, sim_override)
    #
    # sim_override: dict of extra kwargs to override defaults (used for v06_baseline_ctrl)
    configs = [
        # name,                tick, slots, cap,  cont_mode,   tol,  decay,  rot_mode,     overrides
        ("aave_only",          None, None,  None, None,        None, None,   None,          {}),
        ("v09_trailing",       1,    11,    11,   "trailing",  0.05, 0.001,  "offensive",   {}),
        ("v09_fixed",          1,    11,    11,   "fixed",     0.05, 0.001,  "offensive",   {}),
        ("v09_decay",          1,    11,    11,   "decay",     0.05, 0.001,  "offensive",   {}),
        ("v09_none",           1,    11,    11,   "none",      0.05, 0.001,  "offensive",   {}),
        # v06 control: reproduces v0.6 slack_5pct (tick=24, n_main_cap=2, n_slots=4, trailing, offensive)
        ("v06_baseline_ctrl",  24,   4,     2,    "trailing",  0.05, 0.001,  "offensive",   {}),
    ]

    rows = []

    for cfg in configs:
        (cfg_name, tick_h, n_slots, n_cap, cont_mode,
         cont_tol, decay, rot_mode, overrides) = cfg

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
                    "n_main_cap":                     float("nan"),
                    "n_slots_total":                  float("nan"),
                    "continue_mode":                  float("nan"),
                    "continue_tolerance_apr":         float("nan"),
                    "anchor_decay_per_tick":          float("nan"),
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
        print(f"\nRunning config '{cfg_name}':"
              f" tick={tick_h}h  n_slots={n_slots}  n_cap={n_cap}"
              f"  continue_mode={cont_mode}  tolerance={cont_tol:.2f}"
              f"  decay={decay:.4f}  rotation_mode={rot_mode}")

        sim_kwargs = dict(
            tick_hours=tick_h,
            slice_pct=0.10,
            continue_mode=cont_mode,
            continue_tolerance_apr=cont_tol,
            anchor_decay_per_tick=decay,
            rotation_mode=rot_mode,
            rotation_delta_apr=0.10,
            degradation_threshold_apr=0.05,
            exit_signal_threshold_apr=0.0,
            signal_window_hours=SIGNAL_WINDOW,
            n_slots_total=n_slots,
            n_main_cap=n_cap,
            aave_apr=0.0,
            unwind_frozen=True,
        )
        sim_kwargs.update(overrides)

        (pnl, cap, gross_fund_arr, fees_arr, aave_arr, is_idle_arr, info) = simulate_rebalance_v09(
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
        n_ramps_val           = info["n_ramps"]
        n_continues_val       = info["n_continues"]
        n_freezes_val         = info["n_freezes"]
        n_rots                = info["n_rotations"]
        n_frozen_unwinds_val  = info["n_frozen_unwinds"]
        n_deg_exits           = info["n_degradation_exits"]
        n_ft_skipped          = info["n_first_tranche_skipped"]

        # Capital base for metrics: aave_budget
        capital_base = aave_bud if aave_bud > 0 else float(TOTAL_CAPITAL * n_cap)

        for period_name, start_idx in [("full", 0), ("last_90d", max(0, n_hours - LAST_90D_HOURS))]:
            pnl_slice    = pnl[start_idx:]
            gf_slice     = gross_fund_arr[start_idx:]
            fee_slice    = fees_arr[start_idx:]
            aave_slice   = aave_arr[start_idx:]
            idle_slice   = is_idle_arr[start_idx:]
            n_slice      = len(pnl_slice)

            if n_slice == 0 or capital_base == 0:
                continue

            m = _metrics_on_capital(pnl_slice, capital_base)

            yrs = n_slice / HOURS_PER_YEAR

            window_gf   = float(gf_slice.sum())
            window_fees = float(fee_slice.sum())
            window_aave = float(aave_slice.sum())

            gross_fund_annual_pct = (window_gf   / capital_base / yrs * 100) if yrs > 0 else float("nan")
            fees_annual_pct       = (window_fees  / capital_base / yrs * 100) if yrs > 0 else float("nan")
            aave_annual_pct       = (window_aave  / capital_base / yrs * 100) if yrs > 0 else float("nan")
            funding_minus_fees    = gross_fund_annual_pct - fees_annual_pct
            fees_ratio            = (window_fees / window_gf) if window_gf > 0 else float("nan")

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
                "n_ramps":                        n_ramps_val,
                "n_continues":                    n_continues_val,
                "n_freezes":                      n_freezes_val,
                "n_rotations":                    n_rots,
                "n_degradation_exits":            n_deg_exits,
                "n_first_tranche_skipped":        n_ft_skipped,
                "n_frozen_unwinds":               n_frozen_unwinds_val,
                "pct_time_in_usdc":               round(pct_usdc, 4),
                "tick_hours":                     sim_kwargs["tick_hours"],
                "slice_pct":                      sim_kwargs["slice_pct"],
                "n_main_cap":                     sim_kwargs["n_main_cap"],
                "n_slots_total":                  sim_kwargs["n_slots_total"],
                "continue_mode":                  sim_kwargs["continue_mode"],
                "continue_tolerance_apr":         sim_kwargs["continue_tolerance_apr"],
                "anchor_decay_per_tick":          sim_kwargs["anchor_decay_per_tick"],
                "rotation_mode":                  sim_kwargs["rotation_mode"],
                "rotation_delta_apr":             sim_kwargs["rotation_delta_apr"],
                "degradation_threshold_apr":      sim_kwargs["degradation_threshold_apr"],
                "exit_signal_threshold_apr":      sim_kwargs["exit_signal_threshold_apr"],
                "aave_apr":                       sim_kwargs["aave_apr"],
                "unwind_enabled":                 sim_kwargs["unwind_frozen"],
                "peak_capital":                   round(peak_cap, 2),
            })

        full_row = rows[-2]
        print(f"  peak_cap={peak_cap:.0f}, aave_budget={aave_bud:.0f}, "
              f"ramps={n_ramps_val}, continues={n_continues_val}, freezes={n_freezes_val}, "
              f"rotations={n_rots}, deg_exits={n_deg_exits}, "
              f"frozen_unwinds={n_frozen_unwinds_val}, "
              f"annual_full={full_row['annual_pct']:.2f}%, calmar={full_row['calmar']:.2f}")

    # ── Save CSV ───────────────────────────────────────────────────────────────
    col_order = [
        "config", "period", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct", "aave_income_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_continues", "n_freezes",
        "n_rotations", "n_degradation_exits", "n_first_tranche_skipped", "n_frozen_unwinds",
        "pct_time_in_usdc",
        "tick_hours", "slice_pct", "n_main_cap", "n_slots_total",
        "continue_mode", "continue_tolerance_apr", "anchor_decay_per_tick",
        "rotation_mode", "rotation_delta_apr", "degradation_threshold_apr",
        "exit_signal_threshold_apr", "aave_apr", "unwind_enabled", "peak_capital",
    ]
    out_path = Path(__file__).parent / "rebalance_v09_results.csv"
    df_out = pd.DataFrame(rows, columns=col_order)
    df_out.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # ── Print comparison table ─────────────────────────────────────────────────
    pd.set_option("display.width", 400)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.float_format", "{:.2f}".format)

    display_cols = [
        "config", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_continues", "n_freezes",
        "n_rotations", "n_frozen_unwinds",
        "pct_time_in_usdc",
        "continue_mode", "n_main_cap", "tick_hours", "peak_capital",
    ]

    df_full = df_out[df_out["period"] == "full"].copy()
    df_90   = df_out[df_out["period"] == "last_90d"].copy()

    print("\n" + "=" * 260)
    print("REBALANCE V0.9 — FULL PERIOD (sorted by calmar)")
    print("=" * 260)
    print(df_full[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    print("\n" + "=" * 260)
    print("REBALANCE V0.9 — LAST 90 DAYS (sorted by calmar)")
    print("=" * 260)
    print(df_90[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    # ── SANITY CHECK 1: v06_baseline_ctrl reproduces v0.6 slack_5pct ──────────
    V06_SLACK5_ANNUAL_FULL  = 2.63
    V06_SLACK5_CALMAR_FULL  = 4.53
    V06_SLACK5_ANNUAL_90D   = 5.75
    V06_SLACK5_CALMAR_90D   = 126.27

    print("\n" + "=" * 120)
    print("SANITY CHECK 1: v06_baseline_ctrl vs v06_slack_5pct reference")
    print(f"  v06 reference: full={V06_SLACK5_ANNUAL_FULL:.2f}% calmar={V06_SLACK5_CALMAR_FULL:.2f}  "
          f"90d={V06_SLACK5_ANNUAL_90D:.2f}% calmar={V06_SLACK5_CALMAR_90D:.2f}")
    print("=" * 120)
    for period in ("full", "last_90d"):
        r = df_out[(df_out["config"] == "v06_baseline_ctrl") & (df_out["period"] == period)]
        if r.empty:
            continue
        rv = r.iloc[0]
        v06_ann = V06_SLACK5_ANNUAL_FULL if period == "full" else V06_SLACK5_ANNUAL_90D
        v06_cal = V06_SLACK5_CALMAR_FULL if period == "full" else V06_SLACK5_CALMAR_90D
        ann_diff = rv["annual_pct"] - v06_ann
        cal_diff = rv["calmar"] - v06_cal
        match_ann = "OK" if abs(ann_diff) <= 0.15 else "MISMATCH"
        match_cal = "OK" if abs(cal_diff) <= 5.0  else "MISMATCH"
        print(f"  [{period:8s}]  ctrl: annual={rv['annual_pct']:.2f}%  calmar={rv['calmar']:.2f}  "
              f"v06_ref: annual={v06_ann:.2f}%  calmar={v06_cal:.2f}  "
              f"ann_diff={ann_diff:+.3f}%  [{match_ann}]  "
              f"cal_diff={cal_diff:+.2f}  [{match_cal}]")

    # ── SANITY CHECK 2: Structural changes impact ──────────────────────────────
    print("\n" + "=" * 120)
    print("SANITY CHECK 2: Structural changes (no limit + 1h tick): v09_trailing vs v06_baseline_ctrl")
    print("=" * 120)
    for period in ("full", "last_90d"):
        r_ctrl  = df_out[(df_out["config"] == "v06_baseline_ctrl") & (df_out["period"] == period)]
        r_v09t  = df_out[(df_out["config"] == "v09_trailing")      & (df_out["period"] == period)]
        if r_ctrl.empty or r_v09t.empty:
            continue
        ctrl = r_ctrl.iloc[0]
        v09t = r_v09t.iloc[0]
        print(f"  [{period:8s}]")
        print(f"    v06_ctrl:    annual={ctrl['annual_pct']:.2f}%  calmar={ctrl['calmar']:.2f}  "
              f"peak_cap={ctrl['peak_capital']:.0f}  ramps={ctrl['n_ramps']:.0f}  "
              f"continues={ctrl['n_continues']:.0f}  fees_ratio={ctrl['fees_ratio']:.4f}")
        print(f"    v09_trailing: annual={v09t['annual_pct']:.2f}%  calmar={v09t['calmar']:.2f}  "
              f"peak_cap={v09t['peak_capital']:.0f}  ramps={v09t['n_ramps']:.0f}  "
              f"continues={v09t['n_continues']:.0f}  fees_ratio={v09t['fees_ratio']:.4f}")
        delta_ann = v09t["annual_pct"] - ctrl["annual_pct"]
        print(f"    delta: annual={delta_ann:+.2f}%  peak_cap_ratio={v09t['peak_capital']/ctrl['peak_capital']:.1f}x  "
              f"continues_ratio={v09t['n_continues']/ctrl['n_continues']:.1f}x")

    # ── SUMMARY TABLE (continue mode comparison) ───────────────────────────────
    print("\n" + "=" * 180)
    print("CONTINUE-MODE COMPARISON TABLE")
    print("=" * 180)
    v09_configs = ["v09_trailing", "v09_fixed", "v09_decay", "v09_none"]
    header = (f"  {'mode':15s}  {'annual_full':>11} {'calmar_full':>11} "
              f"{'annual_90d':>10} {'calmar_90d':>10} "
              f"{'n_ramps':>8} {'n_continues':>11} {'n_freezes':>10} {'fees%':>7}")
    print(header)
    print("  " + "-" * 100)
    for cfg_name in v09_configs:
        rf = df_full[df_full["config"] == cfg_name]
        r9 = df_90[df_90["config"] == cfg_name]
        if rf.empty:
            continue
        rv_f = rf.iloc[0]
        rv_9 = r9.iloc[0] if not r9.empty else None
        ann_90d   = rv_9["annual_pct"] if rv_9 is not None else float("nan")
        cal_90d   = rv_9["calmar"]     if rv_9 is not None else float("nan")
        fees_pct  = rv_f["fees_annual_pct"]
        mode_lbl  = cfg_name.replace("v09_", "")
        print(f"  {mode_lbl:15s}  {rv_f['annual_pct']:>11.2f} {rv_f['calmar']:>11.2f} "
              f"{ann_90d:>10.2f} {cal_90d:>10.2f} "
              f"{rv_f['n_ramps']:>8.0f} {rv_f['n_continues']:>11.0f} {rv_f['n_freezes']:>10.0f} {fees_pct:>7.2f}%")

    # ── BEST V0.9 RECOMMENDATION ───────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("BEST V0.9 CONFIG SELECTION (highest calmar_full among v09_* configs)")
    print("=" * 120)
    best_calmar = -1e9
    best_cfg    = None
    for cfg_name in v09_configs:
        rf = df_full[df_full["config"] == cfg_name]
        if rf.empty:
            continue
        cal = rf.iloc[0]["calmar"]
        if cal > best_calmar:
            best_calmar = cal
            best_cfg    = cfg_name
    if best_cfg:
        rf = df_full[df_full["config"] == best_cfg]
        r9 = df_90[df_90["config"] == best_cfg]
        rv_f = rf.iloc[0]
        rv_9 = r9.iloc[0] if not r9.empty else None
        print(f"  Winner: {best_cfg}")
        print(f"    full:    annual={rv_f['annual_pct']:.2f}%  calmar={rv_f['calmar']:.2f}  max_dd={rv_f['max_dd_pct']:.2f}%")
        if rv_9 is not None:
            print(f"    last90d: annual={rv_9['annual_pct']:.2f}%  calmar={rv_9['calmar']:.2f}  max_dd={rv_9['max_dd_pct']:.2f}%")
        print(f"    peak_capital={rv_f['peak_capital']:.0f}  n_ramps={rv_f['n_ramps']:.0f}  n_continues={rv_f['n_continues']:.0f}")

    # ── VS PRIOR BEST (v0.5/v0.6) ─────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("V0.9 BEST vs PRIOR BEST (v06_slack_5pct)")
    print(f"  v06 best: full=2.63%/calmar=4.53  90d=5.75%/calmar=126.27")
    print("=" * 120)
    for cfg_name in v09_configs:
        rf = df_full[df_full["config"] == cfg_name]
        r9 = df_90[df_90["config"] == cfg_name]
        if rf.empty:
            continue
        rv_f = rf.iloc[0]
        rv_9 = r9.iloc[0] if not r9.empty else None
        ann_90d = rv_9["annual_pct"] if rv_9 is not None else float("nan")
        cal_90d = rv_9["calmar"]     if rv_9 is not None else float("nan")
        print(f"  {cfg_name:20s}  full: ann={rv_f['annual_pct']:+.2f}%  calmar={rv_f['calmar']:.2f}  "
              f"90d: ann={ann_90d:+.2f}%  calmar={cal_90d:.2f}")

    print(f"\nDone. Output: {out_path}")


if __name__ == "__main__":
    main()
