"""
rebalance_v03.py — Scale-in/scale-out + rotation by funding strength, v0.3.

Single behavioral change vs v0.2:
  - exit_signal_threshold_apr default lowered from 0.12 → 0.00.
    Hypothesis: holding a 3% APR position still earns more than the 21bps
    fee cost of exit+re-entry over typical hold windows. Only exit when
    funding is truly dead (at or below zero).

Everything else identical to v0.2:
  - Frozen-growing unwind (Fix A, unwind_frozen=True always for real configs)
  - Asymmetric entry/exit (entry_ma_apr=0.15, entry_min_24h_apr=0.12)
  - Window-aware pct_time_in_usdc (Fix C)

Sweep: 6 configs × 2 periods (full + last_90d) = 12 rows.

Run: uv run python research/rebalance_v03.py
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

def _run_simulation(
    coins: list,
    prices: dict,          # coin -> np.ndarray (float64), aligned
    rates: dict,           # coin -> np.ndarray (float64), aligned
    ma12: dict,            # coin -> np.ndarray, pre-computed MA12 APR
    sig24min: dict,        # coin -> np.ndarray, pre-computed 24h min of MA12
    n_hours: int,
    *,
    tick_hours: int,
    slice_pct: float,
    entry_ma_apr: float,
    entry_min_24h_apr: float,
    rotation_delta_apr: float,
    exit_signal_threshold_apr: float,
    signal_window_hours: int,
    n_slots_total: int,
    n_main_cap: int,
    aave_apr: float,
    unwind_frozen: bool,
) -> tuple:
    """
    Core per-hour simulation loop.

    Returns (pnl_per_hour, active_capital_per_hour, gross_funding_per_hour,
             fees_per_hour, aave_income_per_hour, is_idle_arr, info_dict).
    """

    # ── Slot state ─────────────────────────────────────────────────────────────
    def make_slot():
        return {
            "coin":              None,
            "state":             "empty",   # empty | growing | holding | shrinking
            "tranches":          [],
            "cum_realized_pnl":  0.0,
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
    n_rotations      = 0
    n_ramps          = 0
    n_frozen_unwinds = 0

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
        P = prices[coin][h]
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
        P = prices[coin][h]
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
        P = prices[coin][h]
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

    def start_new_slot(sl, coin, h):
        nonlocal n_ramps
        sl["coin"]             = coin
        sl["state"]            = "growing"
        sl["tranches"]         = []
        sl["cum_realized_pnl"] = 0.0
        t = open_tranche(sl, h, coin)
        sl["tranches"].append(t)
        total_frac = sum(x["fraction"] for x in sl["tranches"])
        if total_frac >= 1.0 - 1e-9:
            sl["state"] = "holding"
        n_ramps += 1

    def best_candidate(exclude_coins, h, must_beat_apr=None):
        best_coin = None
        best_apr  = -1e9
        for c in coins:
            if c in exclude_coins:
                continue
            c_ma   = ma12[c][h]
            c_min  = sig24min[c][h]
            if np.isnan(c_ma) or np.isnan(c_min):
                continue
            if c_ma < entry_ma_apr:
                continue
            if c_min < entry_min_24h_apr:
                continue
            if must_beat_apr is not None and c_ma <= must_beat_apr + rotation_delta_apr:
                continue
            if c_ma > best_apr:
                best_apr  = c_ma
                best_coin = c
        return best_coin

    # ── Main loop ──────────────────────────────────────────────────────────────
    for h in range(n_hours):

        fees_this_tick = 0.0   # reset per-hour fee accumulator

        # 1) Accrue funding for all open tranches
        hour_funding = 0.0
        for sl in slots:
            if not sl["tranches"]:
                continue
            coin = sl["coin"]
            P    = prices[coin][h]
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

        # Fix C: track per-hour idle boolean
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
                c_ma  = ma12[coin][h]
                c_min = sig24min[coin][h]
                if (not np.isnan(c_ma) and not np.isnan(c_min)
                        and c_ma > entry_ma_apr
                        and c_min > entry_min_24h_apr):
                    # Filter passes: add next tranche
                    t = open_tranche(sl, h, coin)
                    sl["tranches"].append(t)
                    total_frac = sum(x["fraction"] for x in sl["tranches"])
                    if total_frac >= 1.0 - 1e-9:
                        sl["state"] = "holding"
                else:
                    # Fix A: filter does NOT pass — check breakeven on partial position
                    if unwind_frozen and sl["tranches"]:
                        coin_h = sl["coin"]
                        P = prices[coin_h][h]
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

                # 1) Try rotation first
                held_coins  = coins_in_slots()
                current_apr = ma12[coin][h] if not np.isnan(ma12[coin][h]) else -1e9

                candidate = best_candidate(
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
                    # 2) No rotation candidate: exit if signal too low
                    #    v0.3 change: default exit_signal_threshold_apr = 0.00
                    #    (only exit when funding is truly dead)
                    c_ma = ma12[coin][h]
                    if not np.isnan(c_ma) and c_ma < exit_signal_threshold_apr:
                        sl["state"] = "shrinking"
                    # 3) else: hold

            # --- Process EMPTY slots ---
            for sl in slots:
                if sl["state"] != "empty":
                    continue
                n_gh = n_growing_plus_holding()
                if n_gh < n_main_cap:
                    held_coins = coins_in_slots()
                    candidate  = best_candidate(held_coins, h)
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

    # Add Aave income into the pnl series
    pnl_per_hour += aave_income_per_hour

    info = {
        "cum_funding_income":   cum_funding_income,
        "cum_fees":             cum_fees_global,
        "n_rotations":          n_rotations,
        "n_ramps":              n_ramps,
        "n_frozen_unwinds":     n_frozen_unwinds,
        "peak_active_capital":  peak_active_capital,
        "aave_budget":          aave_budget,
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


def simulate_rebalance_v03(
    coins: list,
    prices: dict,
    rates_data: dict,
    ma12_data: dict,
    sig24min_data: dict,
    n_hours: int,
    *,
    tick_hours: int = 24,
    slice_pct: float = 0.10,
    entry_ma_apr: float = 0.15,
    entry_min_24h_apr: float = 0.12,
    rotation_delta_apr: float = 0.10,
    exit_signal_threshold_apr: float = 0.00,   # v0.3: default lowered from 0.12 → 0.00
    signal_window_hours: int = 12,
    n_slots_total: int = 4,
    n_main_cap: int = 2,
    aave_apr: float = 0.05,
    unwind_frozen: bool = True,
) -> tuple:
    """
    Returns (pnl_per_hour, active_capital_per_hour, gross_funding_per_hour,
             fees_per_hour, aave_income_per_hour, is_idle_arr, info_dict).
    """
    return _run_simulation(
        coins=coins,
        prices=prices,
        rates=rates_data,
        ma12=ma12_data,
        sig24min=sig24min_data,
        n_hours=n_hours,
        tick_hours=tick_hours,
        slice_pct=slice_pct,
        entry_ma_apr=entry_ma_apr,
        entry_min_24h_apr=entry_min_24h_apr,
        rotation_delta_apr=rotation_delta_apr,
        exit_signal_threshold_apr=exit_signal_threshold_apr,
        signal_window_hours=signal_window_hours,
        n_slots_total=n_slots_total,
        n_main_cap=n_main_cap,
        aave_apr=aave_apr,
        unwind_frozen=unwind_frozen,
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
    AAVE_APR = 0.05

    # v0.3 sweep: 6 configs
    # configs: (name, overrides_or_None, unwind_frozen)
    # overrides=None means synthetic aave_only row
    configs = [
        ("aave_only",               None,                                         True),
        ("v03_hold_til_zero",       dict(exit_signal_threshold_apr=0.00),         True),
        ("v03_hold_til_neg5",       dict(exit_signal_threshold_apr=-0.05),        True),
        ("v03_exit_at_5",           dict(exit_signal_threshold_apr=0.05),         True),
        ("v03_exit_at_12",          dict(exit_signal_threshold_apr=0.12),         True),
        ("v03_hold_til_zero_fast",  dict(exit_signal_threshold_apr=0.00,
                                         tick_hours=6),                           True),
    ]

    default_kwargs = dict(
        tick_hours=24,
        slice_pct=0.10,
        entry_ma_apr=0.15,
        entry_min_24h_apr=0.12,
        rotation_delta_apr=0.10,
        exit_signal_threshold_apr=0.00,   # v0.3 default
        signal_window_hours=SIGNAL_WINDOW,
        n_slots_total=4,
        n_main_cap=2,
        aave_apr=AAVE_APR,
    )

    rows = []
    SYNTHETIC_PEAK_CAP = float(TOTAL_CAPITAL * 2)   # 4000

    for cfg_name, overrides, unwind_frozen in configs:

        # ── Synthetic aave_only row ────────────────────────────────────────────
        if overrides is None:
            print(f"\nConfig 'aave_only': synthetic baseline (5% APR, no trading)")
            for period_name in ("full", "last_90d"):
                rows.append({
                    "config":                         cfg_name,
                    "period":                         period_name,
                    "annual_pct":                     5.0,
                    "calmar":                         float("inf"),
                    "max_dd_pct":                     0.0,
                    "funding_minus_fees_annual_pct":  0.0,
                    "aave_income_annual_pct":         5.0,
                    "gross_funding_annual_pct":       0.0,
                    "fees_annual_pct":                0.0,
                    "fees_ratio":                     float("nan"),
                    "n_ramps":                        0,
                    "n_rotations":                    0,
                    "n_frozen_unwinds":               0,
                    "pct_time_in_usdc":               1.0,
                    "tick_hours":                     float("nan"),
                    "slice_pct":                      float("nan"),
                    "entry_ma_apr":                   float("nan"),
                    "entry_min_24h_apr":              float("nan"),
                    "rotation_delta_apr":             float("nan"),
                    "exit_signal_threshold_apr":      float("nan"),
                    "aave_apr":                       AAVE_APR,
                    "unwind_enabled":                 float("nan"),
                    "peak_capital":                   SYNTHETIC_PEAK_CAP,
                })
            continue

        # ── Real simulation ────────────────────────────────────────────────────
        kwargs = {**default_kwargs, **overrides}
        print(f"\nRunning config '{cfg_name}' (unwind_frozen={unwind_frozen}): {overrides} ...")

        (pnl, cap, gross_fund_arr, fees_arr, aave_arr, is_idle_arr, info) = simulate_rebalance_v03(
            coins=available_coins,
            prices=prices_np,
            rates_data=rates_np,
            ma12_data=ma12_np,
            sig24min_data=sig24min_np,
            n_hours=n_hours,
            unwind_frozen=unwind_frozen,
            **kwargs,
        )

        peak_cap         = info["peak_active_capital"]
        aave_bud         = info["aave_budget"]
        n_ramps          = info["n_ramps"]
        n_rots           = info["n_rotations"]
        n_frozen_unwinds = info["n_frozen_unwinds"]

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

            # Metrics (annual_pct includes aave)
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

            # Fix C: window-aware pct_time_in_usdc
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
                "n_rotations":                    n_rots,
                "n_frozen_unwinds":               n_frozen_unwinds,
                "pct_time_in_usdc":               round(pct_usdc, 4),
                "tick_hours":                     kwargs["tick_hours"],
                "slice_pct":                      kwargs["slice_pct"],
                "entry_ma_apr":                   kwargs["entry_ma_apr"],
                "entry_min_24h_apr":              kwargs["entry_min_24h_apr"],
                "rotation_delta_apr":             kwargs["rotation_delta_apr"],
                "exit_signal_threshold_apr":      kwargs["exit_signal_threshold_apr"],
                "aave_apr":                       kwargs["aave_apr"],
                "unwind_enabled":                 unwind_frozen,
                "peak_capital":                   round(peak_cap, 2),
            })

        full_row = rows[-2]
        print(f"  peak_cap={peak_cap:.0f}, aave_budget={aave_bud:.0f}, "
              f"ramps={n_ramps}, rotations={n_rots}, frozen_unwinds={n_frozen_unwinds}, "
              f"annual_full={full_row['annual_pct']:.2f}%, calmar={full_row['calmar']:.2f}")

    # ── Save CSV ───────────────────────────────────────────────────────────────
    col_order = [
        "config", "period", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct", "aave_income_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_rotations", "n_frozen_unwinds", "pct_time_in_usdc",
        "tick_hours", "slice_pct", "entry_ma_apr", "entry_min_24h_apr",
        "rotation_delta_apr", "exit_signal_threshold_apr", "aave_apr", "unwind_enabled",
        "peak_capital",
    ]
    out_path = Path(__file__).parent / "rebalance_v03_results.csv"
    df_out = pd.DataFrame(rows, columns=col_order)
    df_out.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # ── Print comparison table ─────────────────────────────────────────────────
    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", "{:.2f}".format)

    display_cols = [
        "config", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct", "aave_income_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_rotations", "n_frozen_unwinds", "pct_time_in_usdc",
    ]

    df_full = df_out[df_out["period"] == "full"].copy()
    df_90   = df_out[df_out["period"] == "last_90d"].copy()

    print("\n" + "=" * 160)
    print("REBALANCE V0.3 — FULL PERIOD (sorted by calmar)")
    print("=" * 160)
    print(df_full[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    print("\n" + "=" * 160)
    print("REBALANCE V0.3 — LAST 90 DAYS (sorted by calmar)")
    print("=" * 160)
    print(df_90[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    # Sanity: v03_exit_at_12 should match v02_default within ~0.1%
    print("\n" + "=" * 160)
    print("SANITY: v03_exit_at_12 vs v02_default (should match within ~0.1%)")
    print("=" * 160)
    for period in ("full", "last_90d"):
        r = df_out[(df_out["config"] == "v03_exit_at_12") & (df_out["period"] == period)]
        if not r.empty:
            print(f"  v03_exit_at_12 [{period:8s}] annual={r['annual_pct'].values[0]:.2f}%  "
                  f"(v02_default was 8.76% full / 5.0% last_90d)")

    # Exit threshold progression
    print("\n" + "=" * 160)
    print("EXIT THRESHOLD PROGRESSION (exit_signal_threshold_apr: 0.12 → 0.05 → 0.00 → -0.05)")
    print("=" * 160)
    threshold_configs = [
        ("v03_exit_at_12", 0.12),
        ("v03_exit_at_5",  0.05),
        ("v03_hold_til_zero", 0.00),
        ("v03_hold_til_neg5", -0.05),
    ]
    for period in ("full", "last_90d"):
        print(f"\n  Period: {period}")
        print(f"  {'config':30s}  exit_thr  annual%   calmar   %usdc   n_rot")
        for cfg, thr in threshold_configs:
            r = df_out[(df_out["config"] == cfg) & (df_out["period"] == period)]
            if not r.empty:
                rv = r.iloc[0]
                print(f"  {cfg:30s}  {thr:+.2f}     {rv['annual_pct']:6.2f}%  "
                      f"{rv['calmar']:7.2f}   {rv['pct_time_in_usdc']:.4f}  {rv['n_rotations']}")

    print("\n" + "=" * 160)
    print("SANITY: annual_pct vs funding_minus_fees + aave_income")
    print("=" * 160)
    for _, r in df_out.iterrows():
        if r["config"] == "aave_only":
            continue
        computed = r["funding_minus_fees_annual_pct"] + r["aave_income_annual_pct"]
        diff = r["annual_pct"] - computed
        print(f"  {r['config']:30s} [{r['period']:8s}]  annual={r['annual_pct']:6.2f}%  "
              f"f-f+aave={computed:6.2f}%  diff(price_drift)={diff:+.2f}%")


if __name__ == "__main__":
    main()
