"""
rebalance_v0.py — Scale-in/scale-out + rotation by funding strength.

Strategy: 4 slots total, at most 2 in (growing+holding). Each slot ramps in
via tranches (slice_pct of $2000 per tick) and rotates out when a better
coin is available (by MA12 APR).

Run: uv run python research/rebalance_v0.py
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
    # Fallback: constants from engine.py + local load_data
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
    """Mirror of concurrency_cap.py:metrics_on_capital."""
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
    exit_usdc_threshold_apr: float,
    signal_window_hours: int,
    n_slots_total: int,
    n_main_cap: int,
) -> tuple:
    """
    Core per-hour simulation loop.

    Returns (pnl_per_hour, active_capital_per_hour, info_dict).
    """

    # ── Slot state ─────────────────────────────────────────────────────────────
    # Each slot: dict with coin, state, tranches list, cum_realized_pnl
    # Each tranche: dict as per spec

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
    cum_realized_pnl   = 0.0   # sum over all slots' cum_realized_pnl

    # ── Output arrays ─────────────────────────────────────────────────────────
    pnl_per_hour           = np.zeros(n_hours)
    active_capital_per_hour = np.zeros(n_hours)

    # ── Counters ───────────────────────────────────────────────────────────────
    n_rotations  = 0
    n_ramps      = 0
    hours_all_empty = 0

    # Warm-up boundary: start ticking at h >= signal_window_hours + 24
    tick_start = signal_window_hours + 24

    # Equity tracking
    equity_prev = 0.0   # total equity baseline = 0 (PnL from zero)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def coins_in_slots(exclude_state=None):
        """Return set of coins currently held in non-empty slots."""
        out = set()
        for sl in slots:
            if sl["state"] == "empty":
                continue
            if exclude_state and sl["state"] == exclude_state:
                continue
            if sl["coin"] is not None:
                out.add(sl["coin"])
        return out

    def open_tranche(slot, h, coin):
        """Open one new tranche for a slot. Pay entry fees. Return tranche dict."""
        P = prices[coin][h]
        notional = POSITION_SIZE * slice_pct
        units_spot = notional / P
        units_perp = notional / P
        entry_fees = notional * (SPOT_TAKER + PERP_TAKER)
        t = {
            "entry_hour_idx":  h,
            "entry_spot_p":    P,
            "entry_perp_p":    P,
            "units_spot":      units_spot,
            "units_perp":      units_perp,
            "fraction":        slice_pct,
            "entry_fees":      entry_fees,
            "funding_collected": 0.0,
        }
        nonlocal cum_fees_global
        cum_fees_global += entry_fees
        return t

    def close_tranche(slot, t, h):
        """Realize PnL for one tranche. Return realized pnl (net of fees)."""
        coin = slot["coin"]
        P = prices[coin][h]
        notional_exit = POSITION_SIZE * t["fraction"]
        exit_fees = notional_exit * (SPOT_TAKER + PERP_TAKER)
        spot_pnl  = (P - t["entry_spot_p"]) * t["units_spot"]
        perp_pnl  = (t["entry_perp_p"] - P) * t["units_perp"]
        pnl = t["funding_collected"] + spot_pnl + perp_pnl - t["entry_fees"] - exit_fees
        nonlocal cum_fees_global
        cum_fees_global += exit_fees
        return pnl

    def unrealized_pnl_slot(slot, h):
        """Compute current unrealized PnL for a slot (for breakeven check)."""
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
        """Total unrealized PnL across all slots with open tranches."""
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
        """Initialize an empty slot with a new coin and first tranche."""
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
        """Find best coin not in exclude_coins meeting entry filter."""
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

        # 1) Accrue funding for all open tranches
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

        # 2) Compute active capital
        active_cap = 0.0
        for sl in slots:
            if sl["tranches"]:
                frac = sum(x["fraction"] for x in sl["tranches"])
                active_cap += frac * TOTAL_CAPITAL
        active_capital_per_hour[h] = active_cap

        # Track all-empty hours
        if all(sl["state"] == "empty" for sl in slots):
            hours_all_empty += 1

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
                # Close oldest tranche (FIFO = index 0)
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
                    t = open_tranche(sl, h, coin)
                    sl["tranches"].append(t)
                    total_frac = sum(x["fraction"] for x in sl["tranches"])
                    if total_frac >= 1.0 - 1e-9:
                        sl["state"] = "holding"
                # else: freeze (don't add tranche)

            # --- Process HOLDING slots ---
            for sl in slots:
                if sl["state"] != "holding":
                    continue
                coin = sl["coin"]

                unreal = unrealized_pnl_slot(sl, h)
                is_breakeven = (unreal >= 0.0)

                if is_breakeven:
                    # Check for rotation candidate
                    held_coins  = coins_in_slots()  # includes current coin
                    current_apr = ma12[coin][h] if not np.isnan(ma12[coin][h]) else -1e9
                    not_held    = set(coins) - held_coins

                    candidate = best_candidate(
                        held_coins, h, must_beat_apr=current_apr
                    )

                    if candidate is not None:
                        # Rotate: mark this slot shrinking
                        sl["state"] = "shrinking"
                        n_rotations += 1

                        # Check if we can open new slot for candidate
                        n_gh = n_growing_plus_holding()
                        # after marking this shrinking, n_gh is already updated
                        # (shrinking doesn't count)
                        if n_gh < n_main_cap:
                            empty_sl = find_empty_slot()
                            if empty_sl is not None:
                                start_new_slot(empty_sl, candidate, h)
                    else:
                        # No rotation candidate: check exit threshold
                        c_ma = ma12[coin][h]
                        if not np.isnan(c_ma) and c_ma < exit_usdc_threshold_apr:
                            sl["state"] = "shrinking"

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

        # 4) Equity update
        total_realized = sum(sl["cum_realized_pnl"] for sl in slots)
        unrealized_all = current_unrealized_all(h)
        equity_now = total_realized + unrealized_all + cum_funding_income - cum_fees_global
        pnl_per_hour[h] = equity_now - equity_prev
        equity_prev = equity_now

    # ── Final close of all open positions ─────────────────────────────────────
    # Close everything at last price
    h_final = n_hours - 1
    extra_pnl = 0.0
    for sl in slots:
        if not sl["tranches"]:
            continue
        # Close all tranches
        while sl["tranches"]:
            t = sl["tranches"].pop(0)
            pnl = close_tranche(sl, t, h_final)
            sl["cum_realized_pnl"] += pnl
            extra_pnl += pnl

    # Recalculate funding/fees adjustments into final hour
    # (close_tranche already updated cum_fees_global; funding_collected is baked in pnl)
    # Adjust last hour's pnl
    # The equity_now from the loop already counted unrealized; now we've realized it.
    # The difference: realized_pnl replaces unrealized_pnl (which should be the same
    # accounting at the final price). But close_tranche subtracts exit fees that weren't
    # in the unrealized estimate yet — so we need to adjust.
    # Simplest: recompute final equity.
    total_realized_final = sum(sl["cum_realized_pnl"] for sl in slots)
    equity_final = total_realized_final + cum_funding_income - cum_fees_global
    pnl_per_hour[-1] += equity_final - equity_now

    peak_active_capital = float(active_capital_per_hour.max()) if active_capital_per_hour.max() > 0 else float(TOTAL_CAPITAL * n_main_cap)
    pct_usdc = hours_all_empty / n_hours if n_hours > 0 else 1.0

    info = {
        "cum_funding_income":  cum_funding_income,
        "cum_fees":            cum_fees_global,
        "n_rotations":         n_rotations,
        "n_ramps":             n_ramps,
        "peak_active_capital": peak_active_capital,
        "pct_time_in_usdc":    pct_usdc,
    }
    return pnl_per_hour, active_capital_per_hour, info


def simulate_rebalance_v0(
    coins: list,
    prices: dict,
    rates_data: dict,
    ma12_data: dict,
    sig24min_data: dict,
    n_hours: int,
    *,
    tick_hours: int = 24,
    slice_pct: float = 0.10,
    entry_ma_apr: float = 0.12,
    entry_min_24h_apr: float = 0.10,
    rotation_delta_apr: float = 0.10,
    exit_usdc_threshold_apr: float = 0.10,
    signal_window_hours: int = 12,
    n_slots_total: int = 4,
    n_main_cap: int = 2,
) -> tuple:
    """
    Returns (pnl_per_hour, active_capital_per_hour, info_dict).
    pnl_per_hour: equity[i] - equity[i-1], 1D array.
    active_capital_per_hour: current deployed capital each hour.
    info: dict with cum_funding, cum_fees, n_rotations, n_ramps,
          peak_active_capital, pct_time_in_usdc.
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
        exit_usdc_threshold_apr=exit_usdc_threshold_apr,
        signal_window_hours=signal_window_hours,
        n_slots_total=n_slots_total,
        n_main_cap=n_main_cap,
    )


# ── main() ─────────────────────────────────────────────────────────────────────

def main():
    coins = U11
    print(f"Loading data for {len(coins)} coins: {coins}")

    # Pre-load all data
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

    SIGNAL_WINDOW = 12  # hours for MA12

    for c in available_coins:
        df2  = dfs[c].reindex(common_idx)
        pr   = df2["close"].values.astype(float)
        rt   = df2["fundingRate"].values.astype(float)

        # ma12: rolling 12-hour mean of funding rate, annualized
        ma12_raw = pd.Series(rt).rolling(SIGNAL_WINDOW, min_periods=1).mean().values * HOURS_PER_YEAR

        # signal_24h_min: rolling 24-hour min of ma12
        s24min = pd.Series(ma12_raw).rolling(24, min_periods=1).min().values

        prices_np[c]   = pr
        rates_np[c]    = rt
        ma12_np[c]     = ma12_raw
        sig24min_np[c] = s24min

    # ── Parameter sweep ────────────────────────────────────────────────────────
    LAST_90D_HOURS = 90 * 24  # 2160

    configs = [
        ("baseline",   dict()),
        ("tight",      dict(entry_ma_apr=0.15, entry_min_24h_apr=0.12)),
        ("loose",      dict(entry_ma_apr=0.08, entry_min_24h_apr=0.06)),
        ("fast_tick",  dict(tick_hours=6,  slice_pct=0.10)),
        ("big_slice",  dict(tick_hours=24, slice_pct=0.20)),
    ]

    default_kwargs = dict(
        tick_hours=24,
        slice_pct=0.10,
        entry_ma_apr=0.12,
        entry_min_24h_apr=0.10,
        rotation_delta_apr=0.10,
        exit_usdc_threshold_apr=0.10,
        signal_window_hours=SIGNAL_WINDOW,
        n_slots_total=4,
        n_main_cap=2,
    )

    rows = []

    for cfg_name, overrides in configs:
        kwargs = {**default_kwargs, **overrides}
        print(f"\nRunning config '{cfg_name}': {overrides if overrides else 'defaults'} ...")

        pnl, cap, info = simulate_rebalance_v0(
            coins=available_coins,
            prices=prices_np,
            rates_data=rates_np,
            ma12_data=ma12_np,
            sig24min_data=sig24min_np,
            n_hours=n_hours,
            **kwargs,
        )

        peak_cap = info["peak_active_capital"]
        cum_fund  = info["cum_funding_income"]
        cum_fees  = info["cum_fees"]
        n_ramps   = info["n_ramps"]
        n_rots    = info["n_rotations"]
        pct_usdc  = info["pct_time_in_usdc"]

        # Capital base for metrics: peak_active_capital (same as concurrency_cap.py)
        capital_base = peak_cap if peak_cap > 0 else float(TOTAL_CAPITAL * 2)

        for period_name, start_idx in [("full", 0), ("last_90d", max(0, n_hours - LAST_90D_HOURS))]:
            pnl_slice = pnl[start_idx:]
            n_slice   = len(pnl_slice)

            if n_slice == 0 or capital_base == 0:
                continue

            m = _metrics_on_capital(pnl_slice, capital_base)

            gross_fund_slice = pnl_slice.sum() + cum_fees  # rough; use global for full
            # Better: compute window-specific funding/fees from the arrays isn't
            # available per slice, so we report global for "full" and scale for 90d.
            if period_name == "full":
                gf  = cum_fund
                cf  = cum_fees
            else:
                frac = n_slice / n_hours
                gf   = cum_fund * frac
                cf   = cum_fees * frac

            yrs = n_slice / HOURS_PER_YEAR
            gross_fund_annual_pct = (gf / capital_base / yrs * 100) if yrs > 0 else float("nan")
            fees_annual_pct       = (cf / capital_base / yrs * 100) if yrs > 0 else float("nan")
            fees_ratio            = (cf / gf) if gf > 0 else float("nan")

            rows.append({
                "config":                    cfg_name,
                "tick_hours":                kwargs["tick_hours"],
                "slice_pct":                 kwargs["slice_pct"],
                "entry_ma_apr":              kwargs["entry_ma_apr"],
                "entry_min_24h_apr":         kwargs["entry_min_24h_apr"],
                "rotation_delta_apr":        kwargs["rotation_delta_apr"],
                "exit_usdc_threshold_apr":   kwargs["exit_usdc_threshold_apr"],
                "period":                    period_name,
                "annual_pct":                m["annual_pct"],
                "calmar":                    m["calmar"],
                "max_dd_pct":                m["max_dd_pct"],
                "gross_funding_annual_pct":  round(gross_fund_annual_pct, 2),
                "fees_annual_pct":           round(fees_annual_pct, 2),
                "fees_ratio":                round(fees_ratio, 4) if not np.isnan(fees_ratio) else float("nan"),
                "n_ramps":                   n_ramps,
                "n_rotations":               n_rots,
                "pct_time_in_usdc":          round(pct_usdc, 4),
                "peak_capital":              round(peak_cap, 2),
            })

        print(f"  peak_cap={peak_cap:.0f}, ramps={n_ramps}, rotations={n_rots}, "
              f"pct_usdc={pct_usdc:.2%}, "
              f"annual_full={rows[-2]['annual_pct']:.2f}%, calmar={rows[-2]['calmar']:.2f}")

    # ── Save CSV ───────────────────────────────────────────────────────────────
    out_path = Path(__file__).parent / "rebalance_v0_results.csv"
    df_out = pd.DataFrame(rows, columns=[
        "config", "tick_hours", "slice_pct", "entry_ma_apr", "entry_min_24h_apr",
        "rotation_delta_apr", "exit_usdc_threshold_apr",
        "period", "annual_pct", "calmar", "max_dd_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_rotations", "pct_time_in_usdc", "peak_capital",
    ])
    df_out.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")

    # ── Print comparison table ─────────────────────────────────────────────────
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.float_format", "{:.2f}".format)

    df_full = df_out[df_out["period"] == "full"].copy()
    df_90   = df_out[df_out["period"] == "last_90d"].copy()

    merged = df_full[[
        "config", "annual_pct", "calmar", "max_dd_pct",
        "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
        "n_ramps", "n_rotations", "pct_time_in_usdc", "peak_capital",
    ]].rename(columns={
        "annual_pct":               "ann_full",
        "calmar":                   "cal_full",
        "max_dd_pct":               "dd_full",
        "gross_funding_annual_pct": "gf_full",
        "fees_annual_pct":          "fee_full",
        "fees_ratio":               "f_ratio",
    }).merge(
        df_90[["config", "annual_pct", "calmar", "max_dd_pct"]].rename(columns={
            "annual_pct": "ann_90d",
            "calmar":     "cal_90d",
            "max_dd_pct": "dd_90d",
        }),
        on="config",
    )

    print("\n" + "=" * 130)
    print("REBALANCE V0 — full period vs last 90 days")
    print("=" * 130)
    print(merged.to_string(index=False))
    print("=" * 130)

    # Also print full and 90d separately, sorted by calmar
    print("\n--- FULL PERIOD (sorted by calmar) ---")
    print(df_full[["config", "annual_pct", "calmar", "max_dd_pct",
                   "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
                   "n_ramps", "n_rotations", "pct_time_in_usdc"]
                  ].sort_values("calmar", ascending=False).to_string(index=False))

    print("\n--- LAST 90 DAYS (sorted by calmar) ---")
    print(df_90[["config", "annual_pct", "calmar", "max_dd_pct",
                 "gross_funding_annual_pct", "fees_annual_pct", "fees_ratio",
                 "n_ramps", "n_rotations", "pct_time_in_usdc"]
                ].sort_values("calmar", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
