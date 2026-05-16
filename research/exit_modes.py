"""
Exit-logic improvements для funding-harvest стратегии.

Проблема: baseline exit использует мгновенный funding rate × 8760.
Один час с плохим funding вылетает позицию, даже если средний funding отличный.
Это асимметрично с entry, который использует 12h MA.

Три варианта:
  raw        — текущая логика (мгновенный ar < exit_threshold)
  smoothed   — симметричный с entry: smoothed 12h MA < exit_threshold
  persistent — exit только если N часов подряд мгновенный ar < exit_threshold
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


def simulate_with_exit_mode(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
    exit_mode: str,          # "raw" | "smoothed" | "persistent"
    persistent_hours: int,   # только для exit_mode="persistent"
):
    """
    Симулирует A_cycle с динамическим min_hold и configurable exit logic.

    exit_mode:
        "raw"        — текущая логика: мгновенный ar < exit_threshold
        "smoothed"   — использует smoothed 12h MA signal (симметрично с entry)
        "persistent" — exit только если persistent_hours подряд ar < exit_threshold

    Возвращает (pnl_per_hour, cap_per_hour, info).
    info содержит: opens_per_hour, total_trades, avg_hold_hours.
    """
    # Загрузка данных и выравнивание
    datas = {}
    for c in coins:
        df = load_data(c)
        if df.empty:
            continue
        datas[c] = df

    # Общий временной индекс
    common_idx = sorted(set().union(*[set(df.index) for df in datas.values()]))
    common_idx = pd.DatetimeIndex(common_idx)
    n = len(common_idx)

    # Подготовка массивов для каждой монеты
    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        # Сигнал входа (сглаженный 12h MA, annualized)
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
            "consec_exit_hours": 0,   # счётчик подряд часов ниже threshold (persistent)
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    entry_rates_collected = []
    holds_collected       = []
    closed_hold_hours     = []   # hold в часах для каждой закрытой позиции

    for i in range(n):
        # 1) Funding для всех уже-в-позиции
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit для in-position если выполняются условия
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1

            # Мгновенный annualized rate
            ar = s["rates"][i] * HOURS_PER_YEAR
            # Smoothed signal (уже annualized 12h MA)
            ar_smoothed = s["signal"][i]

            # Определяем should_exit по режиму
            should_exit = False
            if exit_mode == "raw":
                should_exit = (ar < exit_threshold)
            elif exit_mode == "smoothed":
                should_exit = (ar_smoothed < exit_threshold)
            elif exit_mode == "persistent":
                if ar < exit_threshold:
                    s["consec_exit_hours"] += 1
                else:
                    s["consec_exit_hours"] = 0
                should_exit = (s["consec_exit_hours"] >= persistent_hours)

            if s["hours_since"] >= s["position_min_hold"] and should_exit:
                P = s["close"][i]
                # Записать hold длительность
                closed_hold_hours.append(s["hours_since"])
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

        # 3) Подсчёт текущих in-position
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — берём top-K по signal
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

                # Динамический min_hold на основе entry rate
                entry_rate = s["signal"][i]  # annualized, сглаженный
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)

                # Купить spot
                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE
                s["cash"]             -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= POSITION_SIZE * PERP_TAKER
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["consec_exit_hours"] = 0
                s["trades"]           += 1
                opens_per_hour[i]     += 1

        # 5) MTM equity per coin
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
        "total_trades":           sum(s["trades"] for s in state.values()),
        "peak_capital":           cap_per_hour.max(),
        "opens_per_hour":         opens_per_hour,
        "entry_rates_collected":  entry_rates_collected,
        "holds_collected":        holds_collected,
        "avg_hold_hours":         avg_hold_h,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins        = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K            = 3
    capital_base = K * TOTAL_CAPITAL   # $6000

    # Базовая стратегия (dynamic_aggressive)
    entry_threshold = 0.08
    safety_mult     = 5.0
    cap_min_hold    = 720
    base_min_hold   = 24
    signal_window   = 12

    # 11 конфигов
    sweep_configs = [
        # (exit_mode,  exit_threshold, persistent_hours, label)
        ("raw",        -0.15,  None, "baseline"),
        ("raw",        -0.30,  None, "tolerance_-0.30"),
        ("raw",        -0.50,  None, "tolerance_-0.50"),
        ("smoothed",   -0.15,  None, "symmetric_-0.15"),
        ("smoothed",   -0.30,  None, "symmetric_-0.30"),
        ("smoothed",   -0.50,  None, "symmetric_-0.50"),
        ("persistent", -0.15,  3,    "persistent_3h_-0.15"),
        ("persistent", -0.15,  6,    "persistent_6h_-0.15"),
        ("persistent", -0.15,  12,   "persistent_12h_-0.15"),
        ("persistent", -0.30,  3,    "persistent_3h_-0.30"),
        ("persistent", -0.30,  6,    "persistent_6h_-0.30"),
    ]

    rows = []
    total = len(sweep_configs)

    for idx, (exit_mode, exit_thr, pers_hours, label) in enumerate(sweep_configs, 1):
        ph = pers_hours if pers_hours is not None else 1
        print(f"[{idx}/{total}] {label} ...")
        pnl, cap, info = simulate_with_exit_mode(
            coins,
            max_concurrent=K,
            entry_threshold=entry_threshold,
            exit_threshold=exit_thr,
            base_min_hold=base_min_hold,
            signal_window=signal_window,
            safety_mult=safety_mult,
            cap_min_hold=cap_min_hold,
            exit_mode=exit_mode,
            persistent_hours=ph,
        )
        n = len(pnl)
        opens_ph  = info["opens_per_hour"]
        avg_hold  = info["avg_hold_hours"]

        # full period
        mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
        rows.append({
            "mode":             label,
            "exit_threshold":   exit_thr,
            "persistent_hours": pers_hours if pers_hours is not None else "",
            "period":           "full",
            "annual":           mf["annual"],
            "max_dd":           mf["max_dd"],
            "calmar":           mf["calmar"],
            "sharpe":           mf["sharpe"],
            "trades":           mf["trades"],
            "avg_hold_h":       avg_hold,
            "tim_pct":          mf["time_in_market_pct"],
        })

        # last 90d
        start_90 = max(0, n - 90 * 24)
        m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
        rows.append({
            "mode":             label,
            "exit_threshold":   exit_thr,
            "persistent_hours": pers_hours if pers_hours is not None else "",
            "period":           "last_90d",
            "annual":           m90["annual"],
            "max_dd":           m90["max_dd"],
            "calmar":           m90["calmar"],
            "sharpe":           m90["sharpe"],
            "trades":           m90["trades"],
            "avg_hold_h":       avg_hold,
            "tim_pct":          m90["time_in_market_pct"],
        })

    df = pd.DataFrame(rows, columns=[
        "mode", "exit_threshold", "persistent_hours", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades", "avg_hold_h", "tim_pct",
    ])

    out = Path(__file__).parent / "exit_modes_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ───────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "mode", "exit_threshold", "persistent_hours", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades", "avg_hold_h", "tim_pct",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 160)
    print("FULL PERIOD — все конфиги, sorted by calmar desc")
    print("=" * 160)
    print(df_full[cols_show].to_string(index=False))

    print("\n" + "=" * 160)
    print("LAST 90 DAYS — все конфиги, sorted by calmar desc")
    print("=" * 160)
    print(df_90[cols_show].to_string(index=False))


if __name__ == "__main__":
    main()
