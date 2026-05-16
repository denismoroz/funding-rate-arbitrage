"""
rebalance_v04.py — Scale-in/scale-out + rotation by funding strength, v0.4.

Changes vs v0.3:
  - Replace hardcoded entry_ma_apr=0.15 / entry_min_24h_apr=0.12 with values
    DERIVED from economic inputs: aave_apr, target_alpha_apr, expected_hold_days.
  - Add optional cross-sectional percentile filter (xsect_percentile).

Everything else identical to v0.3.

Sweep: 7 configs × 2 periods = 14 rows.

Run: uv run python research/rebalance_v04.py
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


# ── Entry threshold derivation ─────────────────────────────────────────────────

def derive_entry_thresholds(
    aave_apr: float,
    target_alpha_apr: float,
    expected_hold_days: int,
) -> tuple:
    """
    Derive entry_ma_apr and entry_min_24h_apr from economic inputs.

    fee_cycle_cost  = 2 * (SPOT_TAKER + PERP_TAKER)   # entry + exit per leg
    cycles_per_year = 365 / expected_hold_days
    fee_drag_annual = fee_cycle_cost * cycles_per_year

    entry_ma_apr_derived      = 2 * (aave_apr + target_alpha_apr + fee_drag_annual)
    entry_min_24h_apr_derived = 0.8 * entry_ma_apr_derived

    The factor of 2 arises because we deploy 2× capital (spot + perp collateral)
    but only the perp leg earns funding.

    Returns (entry_ma_apr, entry_min_24h_apr).
    """
    fee_cycle_cost  = 2.0 * (SPOT_TAKER + PERP_TAKER)
    cycles_per_year = 365.0 / expected_hold_days
    fee_drag_annual = fee_cycle_cost * cycles_per_year

    entry_ma_apr      = 2.0 * (aave_apr + target_alpha_apr + fee_drag_annual)
    entry_min_24h_apr = 0.8 * entry_ma_apr
    return entry_ma_apr, entry_min_24h_apr


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
    # Cross-sectional percentile filter (optional)
    xsect_percentile: float | None = None,
    xsect_lookback_days: int = 30,
    # All coin MA12 stacked array for cross-sectional filter
    all_coins_ma12: np.ndarray | None = None,  # shape (n_hours, n_coins)
) -> tuple:
    """
    Core per-hour simulation loop.

    Returns (pnl_per_hour, active_capital_per_hour, gross_funding_per_hour,
             fees_per_hour, aave_income_per_hour, is_idle_arr, info_dict).
    """

    lookback_hours = xsect_lookback_days * 24 if xsect_percentile is not None else 0

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

    def xsect_passes(coin_idx: int, h: int) -> bool:
        """Return True if cross-sectional filter passes (or is disabled)."""
        if xsect_percentile is None or all_coins_ma12 is None:
            return True
        start_h = max(0, h - lookback_hours)
        window = all_coins_ma12[start_h:h, :]
        flat = window.flatten()
        valid = flat[~np.isnan(flat)]
        if len(valid) == 0:
            return True
        threshold = np.nanpercentile(valid, 100.0 * xsect_percentile)
        coin_val = all_coins_ma12[h, coin_idx]
        if np.isnan(coin_val):
            return False
        return coin_val >= threshold

    def start_new_slot(sl, coin, coin_idx, h):
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

    # Build coin -> index map for cross-sectional filter
    coin_to_idx = {c: i for i, c in enumerate(coins)}

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
            # Cross-sectional percentile filter
            if not xsect_passes(coin_to_idx[c], h):
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
                            start_new_slot(empty_sl, candidate, coin_to_idx[candidate], h)

                else:
                    # 2) No rotation candidate: exit if signal too low
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
                        start_new_slot(sl, candidate, coin_to_idx[candidate], h)

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


def simulate_rebalance_v04(
    coins: list,
    prices: dict,
    rates_data: dict,
    ma12_data: dict,
    sig24min_data: dict,
    n_hours: int,
    *,
    tick_hours: int = 24,
    slice_pct: float = 0.10,
    # Economic inputs — replaces hardcoded entry thresholds
    aave_apr: float = 0.05,
    target_alpha_apr: float = 0.00,
    expected_hold_days: int = 30,
    # Other params (identical to v0.3)
    rotation_delta_apr: float = 0.10,
    exit_signal_threshold_apr: float = 0.00,
    signal_window_hours: int = 12,
    n_slots_total: int = 4,
    n_main_cap: int = 2,
    unwind_frozen: bool = True,
    # Cross-sectional filter
    xsect_percentile: float | None = None,
    xsect_lookback_days: int = 30,
    all_coins_ma12: np.ndarray | None = None,
) -> tuple:
    """
    Returns (pnl_per_hour, active_capital_per_hour, gross_funding_per_hour,
             fees_per_hour, aave_income_per_hour, is_idle_arr, info_dict,
             entry_ma_apr_derived, entry_min_24h_apr_derived).
    """
    entry_ma_apr, entry_min_24h_apr = derive_entry_thresholds(
        aave_apr=aave_apr,
        target_alpha_apr=target_alpha_apr,
        expected_hold_days=expected_hold_days,
    )

    result = _run_simulation(
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
        xsect_percentile=xsect_percentile,
        xsect_lookback_days=xsect_lookback_days,
        all_coins_ma12=all_coins_ma12,
    )
    return result + (entry_ma_apr, entry_min_24h_apr)


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

    # Build stacked MA12 array for cross-sectional filter: shape (n_hours, n_coins)
    all_coins_ma12_stacked = np.stack(
        [ma12_np[c] for c in available_coins], axis=1
    )

    # ── Threshold sanity check ─────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("THRESHOLD DERIVATION SANITY CHECK")
    print("=" * 80)
    for aave, alpha, hold in [(0.05, 0.00, 30), (0.05, 0.01, 30),
                               (0.05, 0.02, 30), (0.05, 0.03, 30),
                               (0.05, 0.00, 14)]:
        entry_ma, entry_min = derive_entry_thresholds(aave, alpha, hold)
        fee_cycle = 2.0 * (SPOT_TAKER + PERP_TAKER)
        cpyr = 365.0 / hold
        drag = fee_cycle * cpyr
        print(f"  aave={aave:.2f} alpha={alpha:.2f} hold={hold:2d}d  "
              f"drag={drag:.4f}  entry_ma_apr={entry_ma:.4f}  entry_min_24h={entry_min:.4f}")

    # ── Parameter sweep ────────────────────────────────────────────────────────
    LAST_90D_HOURS = 90 * 24  # 2160
    AAVE_APR = 0.05

    # 7 configs (excluding aave_only synthetic)
    # Each: (name, aave_apr, target_alpha_apr, expected_hold_days, xsect_percentile)
    configs = [
        ("aave_only",         None, None, None, None),   # synthetic
        ("v04_target_0pct",   0.05, 0.00, 30,   None),
        ("v04_target_1pct",   0.05, 0.01, 30,   None),
        ("v04_target_2pct",   0.05, 0.02, 30,   None),
        ("v04_target_3pct",   0.05, 0.03, 30,   None),
        ("v04_short_hold",    0.05, 0.00, 14,   None),
        ("v04_pct70_filter",  0.05, 0.00, 30,   0.70),
    ]

    default_kwargs = dict(
        tick_hours=24,
        slice_pct=0.10,
        rotation_delta_apr=0.10,
        exit_signal_threshold_apr=0.00,
        signal_window_hours=SIGNAL_WINDOW,
        n_slots_total=4,
        n_main_cap=2,
        unwind_frozen=True,
        xsect_lookback_days=30,
    )

    rows = []
    SYNTHETIC_PEAK_CAP = float(TOTAL_CAPITAL * 2)   # 4000

    for cfg_name, aave_apr_cfg, target_alpha, hold_days, xsect_pct in configs:

        # ── Synthetic aave_only row ────────────────────────────────────────────
        if aave_apr_cfg is None:
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
                    "aave_apr":                       AAVE_APR,
                    "target_alpha_apr":               float("nan"),
                    "expected_hold_days":             float("nan"),
                    "entry_ma_apr_derived":           float("nan"),
                    "entry_min_24h_apr_derived":      float("nan"),
                    "xsect_percentile":               float("nan"),
                    "rotation_delta_apr":             float("nan"),
                    "exit_signal_threshold_apr":      float("nan"),
                    "unwind_enabled":                 float("nan"),
                    "peak_capital":                   SYNTHETIC_PEAK_CAP,
                })
            continue

        # ── Real simulation ────────────────────────────────────────────────────
        entry_ma_derived, entry_min_derived = derive_entry_thresholds(
            aave_apr=aave_apr_cfg,
            target_alpha_apr=target_alpha,
            expected_hold_days=hold_days,
        )

        print(f"\nRunning config '{cfg_name}':"
              f" aave={aave_apr_cfg:.2f} alpha={target_alpha:.2f} hold={hold_days}d"
              f" xsect={xsect_pct}"
              f" => entry_ma={entry_ma_derived:.4f} entry_min={entry_min_derived:.4f}")

        (pnl, cap, gross_fund_arr, fees_arr, aave_arr, is_idle_arr, info,
         entry_ma_r, entry_min_r) = simulate_rebalance_v04(
            coins=available_coins,
            prices=prices_np,
            rates_data=rates_np,
            ma12_data=ma12_np,
            sig24min_data=sig24min_np,
            n_hours=n_hours,
            aave_apr=aave_apr_cfg,
            target_alpha_apr=target_alpha,
            expected_hold_days=hold_days,
            xsect_percentile=xsect_pct,
            all_coins_ma12=all_coins_ma12_stacked,
            **default_kwargs,
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
                "tick_hours":                     default_kwargs["tick_hours"],
                "slice_pct":                      default_kwargs["slice_pct"],
                "aave_apr":                       aave_apr_cfg,
                "target_alpha_apr":               target_alpha,
                "expected_hold_days":             hold_days,
                "entry_ma_apr_derived":           round(entry_ma_r, 6),
                "entry_min_24h_apr_derived":      round(entry_min_r, 6),
                "xsect_percentile":               xsect_pct if xsect_pct is not None else float("nan"),
                "rotation_delta_apr":             default_kwargs["rotation_delta_apr"],
                "exit_signal_threshold_apr":      default_kwargs["exit_signal_threshold_apr"],
                "unwind_enabled":                 True,
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
        "tick_hours", "slice_pct",
        "aave_apr", "target_alpha_apr", "expected_hold_days",
        "entry_ma_apr_derived", "entry_min_24h_apr_derived",
        "xsect_percentile",
        "rotation_delta_apr", "exit_signal_threshold_apr",
        "unwind_enabled", "peak_capital",
    ]
    out_path = Path(__file__).parent / "rebalance_v04_results.csv"
    df_out = pd.DataFrame(rows, columns=col_order)
    df_out.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # ── Print comparison table ─────────────────────────────────────────────────
    pd.set_option("display.width", 320)
    pd.set_option("display.max_columns", 35)
    pd.set_option("display.float_format", "{:.2f}".format)

    display_cols = [
        "config", "annual_pct", "calmar", "max_dd_pct",
        "funding_minus_fees_annual_pct", "aave_income_annual_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_rotations", "n_frozen_unwinds", "pct_time_in_usdc",
    ]

    df_full = df_out[df_out["period"] == "full"].copy()
    df_90   = df_out[df_out["period"] == "last_90d"].copy()

    print("\n" + "=" * 180)
    print("REBALANCE V0.4 — FULL PERIOD (sorted by calmar)")
    print("=" * 180)
    print(df_full[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    print("\n" + "=" * 180)
    print("REBALANCE V0.4 — LAST 90 DAYS (sorted by calmar)")
    print("=" * 180)
    print(df_90[display_cols].sort_values("calmar", ascending=False).to_string(index=False))

    # ── Sanity: v04_target_0pct vs v03_hold_til_zero / v03_exit_at_12 ─────────
    print("\n" + "=" * 120)
    print("SANITY: v04_target_0pct vs v0.3 (entry_ma=0.15 / entry_min=0.12) — should match within 0.1%")
    print("=" * 120)
    V03_ANNUAL_FULL   = 8.76
    V03_MAXDD_FULL    = 1.28
    V03_ANNUAL_90D    = 5.0
    V03_MAXDD_90D     = 0.0

    for period in ("full", "last_90d"):
        r = df_out[(df_out["config"] == "v04_target_0pct") & (df_out["period"] == period)]
        if r.empty:
            continue
        rv = r.iloc[0]
        v03_annual = V03_ANNUAL_FULL if period == "full" else V03_ANNUAL_90D
        v03_maxdd  = V03_MAXDD_FULL  if period == "full" else V03_MAXDD_90D
        diff_annual = rv["annual_pct"] - v03_annual
        diff_dd     = rv["max_dd_pct"] - v03_maxdd
        match = "OK" if abs(diff_annual) <= 0.1 else "MISMATCH"
        print(f"  [{period:8s}]  v04_target_0pct annual={rv['annual_pct']:.2f}%  "
              f"v03={v03_annual:.2f}%  diff={diff_annual:+.2f}%  [{match}]"
              f"   max_dd={rv['max_dd_pct']:.2f}%  v03={v03_maxdd:.2f}%  diff_dd={diff_dd:+.2f}%")

    # ── Derived threshold sanity ───────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("DERIVED THRESHOLD SANITY (expected: 0pct~0.1511, 2pct~0.1911, short_hold~0.2095)")
    print("=" * 120)
    for cfg in ("v04_target_0pct", "v04_target_2pct", "v04_short_hold"):
        r = df_out[(df_out["config"] == cfg) & (df_out["period"] == "full")]
        if not r.empty:
            rv = r.iloc[0]
            print(f"  {cfg:20s}  entry_ma_derived={rv['entry_ma_apr_derived']:.4f}"
                  f"  entry_min_derived={rv['entry_min_24h_apr_derived']:.4f}")

    # ── Trade quality vs quantity progression ──────────────────────────────────
    print("\n" + "=" * 120)
    print("TRADE QUALITY vs QUANTITY: raising target_alpha (0% → 1% → 2% → 3%)")
    print("=" * 120)
    print(f"  {'config':20s}  entry_ma   annual%   calmar  n_ramps  %usdc")
    for cfg in ("v04_target_0pct", "v04_target_1pct", "v04_target_2pct", "v04_target_3pct"):
        r = df_out[(df_out["config"] == cfg) & (df_out["period"] == "full")]
        if not r.empty:
            rv = r.iloc[0]
            print(f"  {cfg:20s}  {rv['entry_ma_apr_derived']:.4f}   "
                  f"{rv['annual_pct']:6.2f}%  {rv['calmar']:7.2f}  "
                  f"{rv['n_ramps']:5d}  {rv['pct_time_in_usdc']:.4f}")

    # ── Cross-sectional filter effect ─────────────────────────────────────────
    print("\n" + "=" * 120)
    print("CROSS-SECTIONAL PERCENTILE FILTER: v04_pct70_filter vs v04_target_0pct")
    print("=" * 120)
    for cfg in ("v04_target_0pct", "v04_pct70_filter"):
        for period in ("full", "last_90d"):
            r = df_out[(df_out["config"] == cfg) & (df_out["period"] == period)]
            if not r.empty:
                rv = r.iloc[0]
                print(f"  {cfg:22s} [{period:8s}]  annual={rv['annual_pct']:.2f}%  "
                      f"calmar={rv['calmar']:.2f}  n_ramps={rv['n_ramps']:4d}  %usdc={rv['pct_time_in_usdc']:.4f}")


if __name__ == "__main__":
    main()
