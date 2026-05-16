"""
Exit-logic с max_hold_cap: принудительный выход через N часов.

Проблема: persistent_6h_-0.30 даёт лучшие метрики, но avg_hold ~3923ч (163 дня) —
слишком долго с операционной точки зрения.

Решение: добавить «крышку» max_hold_cap — принудительный exit если позиция держится
дольше N часов, независимо от exit_threshold.

Sweep: 3 exit режима × 7 значений cap = 21 конфиг.
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    load_data,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
)
from adaptive_entry import metrics_window

BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_exit_capped(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,       # cap для dynamic min_hold (формула)
    exit_mode: str,          # "raw" | "symmetric" | "persistent"
    persistent_hours: int,   # только для exit_mode="persistent"
    max_hold_cap: int,       # НОВЫЙ: принудительный exit через N часов (sentinel 100000 = нет cap)
):
    """
    Симулирует A_cycle с dynamic min_hold, configurable exit logic и max_hold_cap.

    exit_mode:
        "raw"        — мгновенный ar < exit_threshold
        "symmetric"  — smoothed 12h MA < exit_threshold (симметрично с entry)
        "persistent" — exit только если persistent_hours подряд ar < exit_threshold

    max_hold_cap: принудительный exit если hours_since >= max_hold_cap
        (работает НЕЗАВИСИМО от exit_threshold, но всё ещё требует min_hold).

    Возвращает (pnl_per_hour, cap_per_hour, info).
    info включает: exits_by_threshold, exits_by_cap.
    """
    datas = {}
    for c in coins:
        df = load_data(c)
        if df.empty:
            continue
        datas[c] = df

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
            "consec_exit_hours": 0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    closed_hold_hours  = []
    exits_by_threshold = 0
    exits_by_cap       = 0

    for i in range(n):
        # 1) Funding для всех уже-в-позиции
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1

            ar = s["rates"][i] * HOURS_PER_YEAR
            ar_smoothed = s["signal"][i]

            # Определяем should_exit по режиму
            should_exit = False
            triggered_by_cap = False

            if exit_mode == "raw":
                should_exit = (ar < exit_threshold)
            elif exit_mode == "symmetric":
                should_exit = (ar_smoothed < exit_threshold)
            elif exit_mode == "persistent":
                if ar < exit_threshold:
                    s["consec_exit_hours"] += 1
                else:
                    s["consec_exit_hours"] = 0
                should_exit = (s["consec_exit_hours"] >= persistent_hours)

            # max_hold_cap: принудительный exit (override should_exit)
            if s["hours_since"] >= max_hold_cap:
                should_exit = True
                triggered_by_cap = True

            if s["hours_since"] >= s["position_min_hold"] and should_exit:
                P = s["close"][i]
                closed_hold_hours.append(s["hours_since"])

                # Атрибуция exit
                if triggered_by_cap and s["hours_since"] >= max_hold_cap:
                    exits_by_cap += 1
                else:
                    exits_by_threshold += 1

                # Закрыть short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # Продать spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0
                s["consec_exit_hours"] = 0

        # 3) Текущие in-position
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — top-K по signal
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

                # Динамический min_hold
                entry_rate = s["signal"][i]
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE
                s["cash"]             -= POSITION_SIZE * SPOT_TAKER
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= POSITION_SIZE * PERP_TAKER
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["consec_exit_hours"] = 0
                s["trades"]           += 1
                opens_per_hour[i]     += 1

        # 5) MTM equity
        hour_pnl = 0.0
        hour_cap = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            short_pnl = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            equity_now = s["cash"] + s["units_spot"] * P + short_pnl
            hour_pnl += equity_now - s["equity_prev"]
            s["equity_prev"] = equity_now
            if s["in_position"]:
                hour_cap += TOTAL_CAPITAL
        pnl_per_hour[i] = hour_pnl
        cap_per_hour[i] = hour_cap

    # Финал: закрыть всё ещё открытое
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P = valid_close[-1]
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    avg_hold_h = round(float(np.mean(closed_hold_hours)), 1) if closed_hold_hours else 0.0

    info = {
        "total_trades":      sum(s["trades"] for s in state.values()),
        "opens_per_hour":    opens_per_hour,
        "avg_hold_hours":    avg_hold_h,
        "exits_by_threshold": exits_by_threshold,
        "exits_by_cap":       exits_by_cap,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins        = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K            = 3
    capital_base = K * TOTAL_CAPITAL  # $6000

    # Базовая стратегия (фиксированные параметры)
    entry_threshold = 0.08
    safety_mult     = 5.0
    cap_min_hold    = 720   # cap для dynamic min_hold formula
    base_min_hold   = 24
    signal_window   = 12

    # 3 exit режима
    exit_configs = [
        # (exit_mode, exit_threshold, persistent_hours, label)
        ("raw",        -0.15, 1, "raw_-0.15"),
        ("symmetric",  -0.30, 1, "symmetric_-0.30"),
        ("persistent", -0.30, 6, "persistent_6h_-0.30"),
    ]

    # 7 значений max_hold_cap
    # 168=7d, 336=14d, 504=21d, 720=30d, 1440=60d, 2160=90d, 100000=no_cap
    cap_values = [168, 336, 504, 720, 1440, 2160, 100000]
    cap_labels = {168: "7d", 336: "14d", 504: "21d", 720: "30d",
                  1440: "60d", 2160: "90d", 100000: "no_cap"}

    total_configs = len(exit_configs) * len(cap_values)
    rows = []
    idx  = 0

    for exit_mode, exit_thr, pers_hours, mode_label in exit_configs:
        for cap_h in cap_values:
            idx += 1
            label = f"{mode_label}_cap_{cap_labels[cap_h]}"
            print(f"[{idx}/{total_configs}] {label} ...")

            pnl, cap_arr, info = simulate_exit_capped(
                coins,
                max_concurrent=K,
                entry_threshold=entry_threshold,
                exit_threshold=exit_thr,
                base_min_hold=base_min_hold,
                signal_window=signal_window,
                safety_mult=safety_mult,
                cap_min_hold=cap_min_hold,
                exit_mode=exit_mode,
                persistent_hours=pers_hours,
                max_hold_cap=cap_h,
            )
            n = len(pnl)
            opens_ph         = info["opens_per_hour"]
            avg_hold         = info["avg_hold_hours"]
            exits_thr        = info["exits_by_threshold"]
            exits_cap        = info["exits_by_cap"]
            cap_days_display = cap_h // 24 if cap_h < 100000 else "inf"

            # full period
            mf = metrics_window(pnl, cap_arr, opens_ph, capital_base, 0, n)
            rows.append({
                "exit_mode":        mode_label,
                "exit_threshold":   exit_thr,
                "persistent_hours": pers_hours if exit_mode == "persistent" else "",
                "max_hold_cap_h":   cap_h,
                "max_hold_cap_days": cap_days_display,
                "period":           "full",
                "annual":           mf["annual"],
                "max_dd":           mf["max_dd"],
                "calmar":           mf["calmar"],
                "sharpe":           mf["sharpe"],
                "trades":           mf["trades"],
                "exits_by_threshold": exits_thr,
                "exits_by_cap":     exits_cap,
                "avg_hold_h":       avg_hold,
                "tim_pct":          mf["time_in_market_pct"],
            })

            # last 90d
            start_90 = max(0, n - 90 * 24)
            m90 = metrics_window(pnl, cap_arr, opens_ph, capital_base, start_90, n)
            rows.append({
                "exit_mode":        mode_label,
                "exit_threshold":   exit_thr,
                "persistent_hours": pers_hours if exit_mode == "persistent" else "",
                "max_hold_cap_h":   cap_h,
                "max_hold_cap_days": cap_days_display,
                "period":           "last_90d",
                "annual":           m90["annual"],
                "max_dd":           m90["max_dd"],
                "calmar":           m90["calmar"],
                "sharpe":           m90["sharpe"],
                "trades":           m90["trades"],
                "exits_by_threshold": exits_thr,
                "exits_by_cap":     exits_cap,
                "avg_hold_h":       avg_hold,
                "tim_pct":          m90["time_in_market_pct"],
            })

    cols = [
        "exit_mode", "exit_threshold", "persistent_hours",
        "max_hold_cap_h", "max_hold_cap_days", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "exits_by_threshold", "exits_by_cap", "avg_hold_h", "tim_pct",
    ]
    df = pd.DataFrame(rows, columns=cols)

    out = Path(__file__).parent / "exit_modes_capped_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "exit_mode", "max_hold_cap_days", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "exits_by_threshold", "exits_by_cap", "avg_hold_h", "tim_pct",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 200)
    print("FULL PERIOD — все 21 конфиг, sorted by calmar desc")
    print("=" * 200)
    print(df_full[cols_show].to_string(index=False))

    print("\n" + "=" * 200)
    print("LAST 90 DAYS — все 21 конфиг, sorted by calmar desc")
    print("=" * 200)
    print(df_90[cols_show].to_string(index=False))


if __name__ == "__main__":
    main()
