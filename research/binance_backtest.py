"""
Backtest dynamic_aggressive strategy on Binance funding data vs HL.

Binance funding is paid every 8h (vs 1h on HL).
We convert to hourly-equivalent by dividing rate by 8, then forward-filling
across the 8-hour window. This means funding accumulates 1/8 per hour × 8 hours
= full 8h rate per settlement period — identical economics.

Fees: Binance spot taker 10bps + perp taker 4bps = 28bps/cycle (vs HL 21bps).
"""

import sys
import warnings
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    POSITION_SIZE, TOTAL_CAPITAL, HOURS_PER_YEAR,
    PERP_TAKER, SPOT_TAKER,
)
from concurrency_cap import simulate_multi_capped
from dynamic_min_hold import simulate_dynamic_min_hold, metrics_window

# Binance fee constants
BINANCE_PERP_TAKER = 0.0004   # 4bps
BINANCE_SPOT_TAKER = 0.0010   # 10bps

# Binance break-even constant (28bps / cycle = 0.0028)
# hours_breakeven = 0.0028 * 8760 / annual_rate = 24.528 / annual_rate
BINANCE_BREAKEVEN_CONST = 24.528

DATA_DIR_HL      = Path(__file__).parent / "data"
DATA_DIR_BINANCE = Path(__file__).parent / "data_binance"


def load_binance_data(coin: str) -> pd.DataFrame:
    """
    Loads Binance funding (8h) + HL OHLCV (1h) for a coin.

    Binance funding is reindexed to hourly and divided by 8
    so that sum over 8 hours = original 8h rate.
    Inner-joined with HL close prices. Trimmed to >= 2023-06-08 UTC.
    """
    binance_path = DATA_DIR_BINANCE / f"{coin}.csv"
    if not binance_path.exists():
        warnings.warn(f"Binance data not found for {coin}: {binance_path}")
        return pd.DataFrame()

    # Load Binance funding
    df_b = pd.read_csv(binance_path)
    df_b["time"] = pd.to_datetime(df_b["time"], format="ISO8601", utc=True).dt.floor("h")
    df_b = df_b.set_index("time")[["fundingRate"]].sort_index()
    df_b = df_b[~df_b.index.duplicated(keep="last")]

    # Reindex to full hourly range
    full_idx = pd.date_range(df_b.index.min(), df_b.index.max(), freq="h", tz="UTC")
    df_b = df_b.reindex(full_idx)

    # Forward-fill and divide by 8 to get per-hour equivalent rate
    df_b["fundingRate"] = df_b["fundingRate"].ffill() / 8.0

    # Load HL OHLCV for price data
    hl_ohlcv_path = DATA_DIR_HL / f"{coin}_1h.csv"
    if not hl_ohlcv_path.exists():
        warnings.warn(f"HL OHLCV not found for {coin}: {hl_ohlcv_path}")
        return pd.DataFrame()

    df_hl = pd.read_csv(hl_ohlcv_path)
    df_hl["time"] = pd.to_datetime(df_hl["time"], format="ISO8601", utc=True).dt.floor("h")
    df_hl = df_hl.set_index("time")[["close"]].sort_index()
    df_hl = df_hl[~df_hl.index.duplicated(keep="last")]

    # Inner join
    df = df_b.join(df_hl, how="inner")

    # Trim first week (consistent with HL engine.load_data)
    df = df[df.index >= pd.Timestamp("2023-06-08", tz="UTC")]

    return df


def simulate_dynamic_min_hold_binance(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
):
    """
    Same as simulate_dynamic_min_hold but:
      - Uses load_binance_data instead of load_data
      - Uses BINANCE_PERP_TAKER / BINANCE_SPOT_TAKER fees
      - Uses BINANCE_BREAKEVEN_CONST for dynamic min_hold calculation
    """
    datas = {}
    for c in coins:
        df = load_binance_data(c)
        if df.empty:
            continue
        datas[c] = df

    if not datas:
        return np.zeros(1), np.zeros(1), {}

    # Common hourly index
    common_idx = sorted(set().union(*[set(df.index) for df in datas.values()]))
    common_idx = pd.DatetimeIndex(common_idx)
    n = len(common_idx)

    # Prepare per-coin state arrays
    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        if signal_window > 1:
            sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
        else:
            sig = rates * HOURS_PER_YEAR
        state[c] = {
            "rates":             rates,
            "close":             close,
            "signal":            sig,
            "valid":             ~np.isnan(close) & ~np.isnan(rates),
            "in_position":       False,
            "short_size":        0.0,
            "units_spot":        0.0,
            "entry_price":       0.0,
            "hours_since":       0,
            "position_min_hold": 0,
            "cash":              TOTAL_CAPITAL,
            "equity_prev":       TOTAL_CAPITAL,
            "trades":            0,
            "hours_in":          0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    entry_rates_collected = []
    holds_collected       = []

    for i in range(n):
        # 1) Funding for all in-position
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit check
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            if s["hours_since"] >= s["position_min_hold"] and ar < exit_threshold:
                P = s["close"][i]
                # Close short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * BINANCE_PERP_TAKER
                # Sell spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * BINANCE_SPOT_TAKER
                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0

        # 3) Count active positions
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Entry candidates — top-K by signal
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > entry_threshold:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]

                # Dynamic min_hold based on entry rate (using Binance break-even)
                entry_rate = s["signal"][i]
                if entry_rate > 0:
                    breakeven_h = BINANCE_BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)

                # Buy spot
                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE
                s["cash"]             -= POSITION_SIZE * BINANCE_SPOT_TAKER
                # Open short
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= POSITION_SIZE * BINANCE_PERP_TAKER
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["trades"]           += 1
                opens_per_hour[i]     += 1

        # 5) MTM equity
        hour_pnl = 0.0
        hour_cap = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            short_pnl  = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            equity_now = s["cash"] + s["units_spot"] * P + short_pnl
            hour_pnl  += equity_now - s["equity_prev"]
            s["equity_prev"] = equity_now
            if s["in_position"]:
                hour_cap += TOTAL_CAPITAL
        pnl_per_hour[i] = hour_pnl
        cap_per_hour[i] = hour_cap

    # Final close of remaining open positions
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P = valid_close[-1]
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * BINANCE_PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * BINANCE_SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    info = {
        "total_trades":          sum(s["trades"] for s in state.values()),
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
        "entry_rates_collected": entry_rates_collected,
        "holds_collected":       holds_collected,
    }
    return pnl_per_hour, cap_per_hour, info


def simulate_multi_capped_binance(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    min_hold: int,
    signal_window: int = 1,
):
    """
    Same as simulate_multi_capped but with Binance funding data and fees.
    """
    datas = {}
    for c in coins:
        df = load_binance_data(c)
        if df.empty:
            continue
        datas[c] = df

    if not datas:
        return np.zeros(1), np.zeros(1), {}

    common_idx = sorted(set().union(*[set(df.index) for df in datas.values()]))
    common_idx = pd.DatetimeIndex(common_idx)
    n = len(common_idx)

    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        if signal_window > 1:
            sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
        else:
            sig = rates * HOURS_PER_YEAR
        state[c] = {
            "rates":        rates,
            "close":        close,
            "signal":       sig,
            "valid":        ~np.isnan(close) & ~np.isnan(rates),
            "in_position":  False,
            "short_size":   0.0,
            "units_spot":   0.0,
            "entry_price":  0.0,
            "hours_since":  0,
            "cash":         TOTAL_CAPITAL,
            "equity_prev":  TOTAL_CAPITAL,
            "trades":       0,
            "hours_in":     0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    for i in range(n):
        # 1) Funding
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                s["cash"] += s["short_size"] * s["close"][i] * s["rates"][i]

        # 2) Exit
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            if s["hours_since"] >= min_hold and ar < exit_threshold:
                P = s["close"][i]
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * BINANCE_PERP_TAKER
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * BINANCE_SPOT_TAKER
                s["short_size"]  = 0.0
                s["units_spot"]  = 0.0
                s["entry_price"] = 0.0
                s["in_position"] = False

        # 3) Active count
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Entry candidates
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > entry_threshold:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]
                s["units_spot"]  = POSITION_SIZE / P
                s["cash"]       -= POSITION_SIZE
                s["cash"]       -= POSITION_SIZE * BINANCE_SPOT_TAKER
                s["short_size"]  = POSITION_SIZE / P
                s["entry_price"] = P
                s["cash"]       -= POSITION_SIZE * BINANCE_PERP_TAKER
                s["in_position"] = True
                s["hours_since"] = 0
                s["trades"]     += 1
                opens_per_hour[i] += 1

        # 5) MTM equity
        hour_pnl = 0.0
        hour_cap = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            short_pnl  = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            equity_now = s["cash"] + s["units_spot"] * P + short_pnl
            hour_pnl  += equity_now - s["equity_prev"]
            s["equity_prev"] = equity_now
            if s["in_position"]:
                hour_cap += TOTAL_CAPITAL
        pnl_per_hour[i] = hour_pnl
        cap_per_hour[i] = hour_cap

    # Final close
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P = valid_close[-1]
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * BINANCE_PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * BINANCE_SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    info = {
        "total_trades":    sum(s["trades"] for s in state.values()),
        "peak_capital":    cap_per_hour.max(),
        "opens_per_hour":  opens_per_hour,
    }
    return pnl_per_hour, cap_per_hour, info


def run_and_collect(label, exchange, mode, pnl, cap, opens_ph, capital_base, n):
    """Helper: compute full + last_90d metrics and return list of row dicts."""
    rows = []
    # Full period
    mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
    rows.append({
        "exchange":       exchange,
        "mode":           mode,
        "period":         "full",
        "annual":         mf["annual"],
        "max_dd":         mf["max_dd"],
        "calmar":         mf["calmar"],
        "sharpe":         mf["sharpe"],
        "trades":         mf["trades"],
        "tim_pct":        mf["time_in_market_pct"],
        "median_wait_h":  mf["median_wait_hours"],
    })
    # Last 90d
    start_90 = max(0, n - 2160)
    m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
    rows.append({
        "exchange":       exchange,
        "mode":           mode,
        "period":         "last_90d",
        "annual":         m90["annual"],
        "max_dd":         m90["max_dd"],
        "calmar":         m90["calmar"],
        "sharpe":         m90["sharpe"],
        "trades":         m90["trades"],
        "tim_pct":        m90["time_in_market_pct"],
        "median_wait_h":  m90["median_wait_hours"],
    })
    return rows


def main():
    binance_universe = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "ARB", "OP"]

    K              = 3
    exit_threshold = -0.15
    signal_window  = 12
    base_min_hold  = 24
    cap_min_hold   = 720
    capital_base   = K * TOTAL_CAPITAL   # $6000

    rows = []

    # ── Binance: aggressive ────────────────────────────────────────────────────
    print("Running binance_aggressive (entry=0.08, safety_mult=5.0) ...")
    pnl, cap, info = simulate_dynamic_min_hold_binance(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.08,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=5.0,
        cap_min_hold=cap_min_hold,
    )
    n = len(pnl)
    opens_ph = info.get("opens_per_hour", np.zeros(n, dtype=int))
    rows.extend(run_and_collect("binance_aggressive", "Binance", "dynamic_aggressive", pnl, cap, opens_ph, capital_base, n))

    # ── Binance: balanced ──────────────────────────────────────────────────────
    print("Running binance_balanced (entry=0.15, safety_mult=3.0) ...")
    pnl, cap, info = simulate_dynamic_min_hold_binance(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.15,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=3.0,
        cap_min_hold=cap_min_hold,
    )
    n = len(pnl)
    opens_ph = info.get("opens_per_hour", np.zeros(n, dtype=int))
    rows.extend(run_and_collect("binance_balanced", "Binance", "dynamic_balanced", pnl, cap, opens_ph, capital_base, n))

    # ── Binance: baseline 30/120 ───────────────────────────────────────────────
    print("Running binance_baseline_30_120 (entry=0.30, min_hold=120) ...")
    pnl, cap, info = simulate_multi_capped_binance(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.30,
        exit_threshold=exit_threshold,
        min_hold=120,
        signal_window=signal_window,
    )
    n = len(pnl)
    opens_ph = info.get("opens_per_hour", np.zeros(n, dtype=int))
    rows.extend(run_and_collect("binance_baseline_30_120", "Binance", "baseline_30_120", pnl, cap, opens_ph, capital_base, n))

    # ── HL baselines (same universe) ───────────────────────────────────────────
    print("Running hl_aggressive (entry=0.08, safety_mult=5.0) ...")
    pnl, cap, info = simulate_dynamic_min_hold(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.08,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=5.0,
        cap_min_hold=cap_min_hold,
    )
    n = len(pnl)
    opens_ph = info.get("opens_per_hour", np.zeros(n, dtype=int))
    rows.extend(run_and_collect("hl_aggressive", "HL", "dynamic_aggressive", pnl, cap, opens_ph, capital_base, n))

    print("Running hl_balanced (entry=0.15, safety_mult=3.0) ...")
    pnl, cap, info = simulate_dynamic_min_hold(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.15,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=3.0,
        cap_min_hold=cap_min_hold,
    )
    n = len(pnl)
    opens_ph = info.get("opens_per_hour", np.zeros(n, dtype=int))
    rows.extend(run_and_collect("hl_balanced", "HL", "dynamic_balanced", pnl, cap, opens_ph, capital_base, n))

    print("Running hl_baseline_30_120 (entry=0.30, min_hold=120) ...")
    pnl, cap, info = simulate_multi_capped(
        coins=binance_universe,
        max_concurrent=K,
        entry_threshold=0.30,
        exit_threshold=exit_threshold,
        min_hold=120,
        signal_window=signal_window,
    )
    n = len(pnl)
    # simulate_multi_capped does not return opens_per_hour — reconstruct from cap diff
    cap_diff = np.diff(cap, prepend=0)
    opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)
    rows.extend(run_and_collect("hl_baseline_30_120", "HL", "baseline_30_120", pnl, cap, opens_ph, capital_base, n))

    # ── Build DataFrame ────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "exchange", "mode", "period",
        "annual", "max_dd", "calmar", "sharpe",
        "trades", "tim_pct", "median_wait_h",
    ])

    # Save CSV
    out_csv = Path(__file__).parent / "binance_backtest_results.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nSaved: {out_csv}")

    # ── Pivot table ────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", "{:.2f}".format)

    metrics_cols = ["annual", "max_dd", "calmar", "sharpe", "trades", "tim_pct", "median_wait_h"]

    pivot = df.pivot_table(
        index=["exchange", "mode"],
        columns="period",
        values=metrics_cols,
        aggfunc="first",
    )
    # Flatten column names: metric_period
    pivot.columns = [f"{m}_{p}" for m, p in pivot.columns]

    # Reorder columns: full then 90d for each metric
    ordered_cols = []
    for m in metrics_cols:
        for p in ["full", "last_90d"]:
            col = f"{m}_{p}"
            if col in pivot.columns:
                ordered_cols.append(col)
    pivot = pivot[ordered_cols]

    # Sort by exchange then mode
    pivot = pivot.sort_index()

    print("\n" + "=" * 160)
    print(f"EXCHANGE COMPARISON — universe: {binance_universe}")
    print(f"K={K}, exit={exit_threshold}, sig_window={signal_window}, base_hold={base_min_hold}h, cap_hold={cap_min_hold}h, capital=${capital_base}")
    print("=" * 160)
    print(pivot.to_string())
    print()

    # Also print a compact summary table sorted by calmar_full
    df_full = df[df["period"] == "full"].copy()
    df_90   = df[df["period"] == "last_90d"].copy()
    df_merged = df_full.merge(
        df_90[["exchange", "mode", "annual", "max_dd", "calmar", "trades"]],
        on=["exchange", "mode"],
        suffixes=("_full", "_90d"),
    )
    df_merged = df_merged.sort_values("calmar_full", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 160)
    print("SUMMARY — sorted by calmar (full)")
    print("=" * 160)
    summary_cols = [
        "exchange", "mode",
        "annual_full", "max_dd_full", "calmar_full",
        "annual_90d",  "max_dd_90d",  "calmar_90d",
        "sharpe", "trades_full", "tim_pct", "median_wait_h",
    ]
    print(df_merged[summary_cols].to_string(index=False))


if __name__ == "__main__":
    main()
